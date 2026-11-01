"""GIS vector import for GeoJSON, zipped Shapefiles, and zipped File Geodatabases."""

from __future__ import annotations

import hashlib
import json
import math
import stat
import tempfile
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from io import BytesIO
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile

import networkx as nx

from rushhour.networks import add_road_pair

MAX_ARCHIVE_BYTES = 200_000_000
MAX_FEATURES = 100_000
ARCGIS_PAGE_SIZE = 2_000
MAX_RESPONSE_BYTES = 50_000_000
ALLOWED_URL_DOMAINS = ("arcgis.com", "nyc.gov")
LOCAL_NETWORK_SUFFIXES = {".geojson", ".json", ".zip", ".graphml", ".xml"}


def discover_local_network_files(directory: Path) -> tuple[Path, ...]:
    """Find supported local network files under a city-organized data directory."""
    if not directory.is_dir():
        return ()
    return tuple(
        sorted(
            path
            for path in directory.rglob("*")
            if path.is_file()
            and path.suffix.lower() in LOCAL_NETWORK_SUFFIXES
            and not any(part.startswith(".") for part in path.relative_to(directory).parts)
        )
    )


def list_vector_layers(content: bytes, filename: str) -> tuple[str, ...]:
    """Return the readable GIS layers in an uploaded vector dataset."""
    geopandas = _geopandas()
    with _materialize_vector_source(content, filename) as source:
        return _list_vector_layers_for_source(source, geopandas)


def load_vector_network(
    content: bytes,
    filename: str,
    *,
    layer: str | None = None,
    bbox_wgs84: tuple[float, float, float, float] | None = None,
    speed_kph: float = 35.0,
    lanes: int = 1,
    capacity_per_lane_vph: float = 900.0,
    bidirectional: bool = True,
    snap_tolerance_m: float = 2.0,
) -> nx.DiGraph:
    """Build a routable graph from linear GIS features in an uploaded dataset."""
    geopandas = _geopandas()
    try:
        with _materialize_vector_source(content, filename) as source:
            layers = _list_vector_layers_for_source(source, geopandas)
            if layer is None:
                if len(layers) != 1:
                    raise ValueError(
                        "This dataset contains multiple layers. Choose the street-centerline layer."
                    )
                layer = layers[0]
            if layer not in layers:
                raise ValueError(f"Layer {layer!r} is not available in this dataset.")
            read_options = {"columns": []}
            if source.suffix.lower() in {".geojson", ".json"}:
                if bbox_wgs84 is not None:
                    read_options["bbox"] = _validate_bbox(bbox_wgs84)
                frame = geopandas.read_file(source, **read_options)
            else:
                frame = geopandas.read_file(source, layer=layer, **read_options)
    except (OSError, RuntimeError, ValueError) as error:
        if isinstance(error, ValueError) and str(error).startswith(
            ("This dataset", "Layer ")
        ):
            raise
        raise ValueError(f"Could not read GIS layer {layer!r}: {error}") from error
    graph = _network_from_frame(
        frame,
        source_name=filename,
        layer=layer,
        bbox_wgs84=bbox_wgs84,
        speed_kph=speed_kph,
        lanes=lanes,
        capacity_per_lane_vph=capacity_per_lane_vph,
        bidirectional=bidirectional,
        snap_tolerance_m=snap_tolerance_m,
    )
    graph.graph["rushhour_source_signature"] = _import_signature(
        hashlib.sha256(content).hexdigest(),
        layer,
        bbox_wgs84,
        speed_kph,
        lanes,
        capacity_per_lane_vph,
        bidirectional,
        snap_tolerance_m,
    )
    return graph


def load_arcgis_network(
    url: str,
    *,
    bbox_wgs84: tuple[float, float, float, float] | None = None,
    speed_kph: float = 35.0,
    lanes: int = 1,
    capacity_per_lane_vph: float = 900.0,
    bidirectional: bool = True,
    snap_tolerance_m: float = 2.0,
    where_clause: str | None = None,
) -> nx.DiGraph:
    """Load a public ArcGIS FeatureServer layer or GeoJSON URL with pagination."""
    url = url.strip()
    _validate_public_url(url)
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    is_query = parts.path.rstrip("/").lower().endswith("/query")
    format_value = query.get("f", [""])[0].lower()
    is_direct_geojson = parts.path.lower().endswith((".geojson", ".json")) and not is_query
    if is_direct_geojson or (not is_query and format_value in {"geojson", "pgeojson"}):
        payload = _fetch_json(url)
        if payload.get("exceededTransferLimit"):
            raise ValueError(
                "The GeoJSON URL is truncated by its server. Use the FeatureServer layer URL "
                "with a WGS84 bounding box so RushHour can page through its records."
            )
        features = payload.get("features")
        if not isinstance(features, list):
            raise ValueError("The URL did not return a GeoJSON FeatureCollection.")
    else:
        endpoint = url if is_query else url.rstrip("/") + "/query"
        features = _fetch_arcgis_features(
            endpoint,
            query,
            bbox_wgs84,
            where_clause=where_clause,
        )

    if len(features) > MAX_FEATURES:
        raise ValueError(
            f"The service returned more than {MAX_FEATURES:,} features. "
            "Add a WGS84 bounding box or an ArcGIS WHERE filter."
        )
    try:
        frame = _geopandas().GeoDataFrame.from_features(features, crs="EPSG:4326")
    except (TypeError, ValueError, KeyError, ImportError) as error:
        raise ValueError(f"The URL did not contain valid GeoJSON line features: {error}") from error
    graph = _network_from_frame(
        frame,
        source_name=url,
        layer="ArcGIS/GeoJSON",
        bbox_wgs84=bbox_wgs84,
        speed_kph=speed_kph,
        lanes=lanes,
        capacity_per_lane_vph=capacity_per_lane_vph,
        bidirectional=bidirectional,
        snap_tolerance_m=snap_tolerance_m,
    )
    graph.graph["rushhour_source_signature"] = _import_signature(
        url,
        "ArcGIS/GeoJSON",
        bbox_wgs84,
        speed_kph,
        lanes,
        capacity_per_lane_vph,
        bidirectional,
        snap_tolerance_m,
        where_clause,
    )
    return graph


def _list_vector_layers_for_source(source: Path, geopandas: Any) -> tuple[str, ...]:
    if source.suffix.lower() in {".geojson", ".json"}:
        return (source.stem,)
    if source.suffix.lower() == ".gdb":
        try:
            layers = geopandas.list_layers(source)
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError(f"Could not inspect the File Geodatabase: {error}") from error
        return tuple(str(layer) for layer in layers["name"])
    if source.suffix.lower() == ".shp":
        return (source.stem,)
    raise ValueError(
        "Upload a GeoJSON file or a ZIP containing a Shapefile or File Geodatabase."
    )


def _import_signature(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()


def _network_from_frame(
    frame: Any,
    *,
    source_name: str,
    layer: str,
    bbox_wgs84: tuple[float, float, float, float] | None,
    speed_kph: float,
    lanes: int,
    capacity_per_lane_vph: float,
    bidirectional: bool,
    snap_tolerance_m: float,
) -> nx.DiGraph:
    if (
        not math.isfinite(speed_kph)
        or speed_kph <= 0
        or not math.isfinite(capacity_per_lane_vph)
        or capacity_per_lane_vph <= 0
        or lanes < 1
        or int(lanes) != lanes
    ):
        raise ValueError("Default speed, lane count, and per-lane capacity must be positive.")
    if not math.isfinite(snap_tolerance_m) or snap_tolerance_m < 0:
        raise ValueError("GIS snap tolerance cannot be negative.")
    if len(frame) > MAX_FEATURES:
        raise ValueError(
            f"The dataset contains more than {MAX_FEATURES:,} features. "
            "Use a smaller WGS84 study-area box before importing."
        )
    if frame.crs is None:
        raise ValueError(
            "The GIS dataset has no coordinate reference system. Assign its correct CRS before import."
        )
    source_crs = str(frame.crs)
    if bbox_wgs84 is not None:
        west, south, east, north = _validate_bbox(bbox_wgs84)
        frame = frame.to_crs("EPSG:4326")
        from shapely.geometry import box

        extent = box(west, south, east, north)
        frame = frame.loc[frame.geometry.notna()].copy()
        frame.geometry = frame.geometry.intersection(extent)
    frame = frame.loc[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
    if frame.empty:
        raise ValueError("No GIS features intersect the selected study area.")
    if not any(
        part.length > 0
        for geometry in frame.geometry
        for part in _line_parts(geometry)
    ):
        raise ValueError("The selected GIS layer contains no usable street centerlines.")
    try:
        target_crs = frame.estimate_utm_crs()
        if target_crs is None:
            raise ValueError("Could not determine a local UTM projection.")
        projected = frame.to_crs(target_crs)
    except (ValueError, RuntimeError) as error:
        raise ValueError("Could not project this GIS layer to a local metric coordinate system.") from error
    lines = [
        part
        for geometry in projected.geometry
        for part in _line_parts(geometry)
        if not part.is_empty and part.length > 0
    ]
    if not lines:
        raise ValueError("No line features remain after projection.")
    lines = _snap_line_endpoints(lines, snap_tolerance_m)
    from shapely.ops import unary_union

    noded = unary_union(lines)
    segments = []
    for line in _line_parts(noded):
        coordinates = list(line.coords)
        segments.extend(
            (start, end)
            for start, end in pairwise(coordinates)
            if math.dist(start[:2], end[:2]) > 0
        )
    if not segments:
        raise ValueError("The GIS layer did not produce any usable street segments.")
    if len(segments) > MAX_FEATURES:
        raise ValueError(
            f"The selected area produced more than {MAX_FEATURES:,} road segments. "
            "Use a smaller WGS84 study-area box or reduce the ArcGIS WHERE filter."
        )

    graph = nx.DiGraph(name=f"{source_name} · {layer}")
    graph.graph.update(
        source_format="gis_vector",
        source_name=source_name,
        source_layer=layer,
        source_crs=source_crs,
        projected_crs=str(projected.crs),
        bbox_wgs84=",".join(map(str, bbox_wgs84)) if bbox_wgs84 else "",
    )
    node_ids: dict[tuple[float, float], str] = {}
    for start, end in segments:
        start_xy = (round(float(start[0]), 3), round(float(start[1]), 3))
        end_xy = (round(float(end[0]), 3), round(float(end[1]), 3))
        source = _gis_node(graph, node_ids, start_xy)
        target = _gis_node(graph, node_ids, end_xy)
        if source == target:
            continue
        length_km = math.dist(start_xy, end_xy) / 1000.0
        if graph.has_edge(source, target):
            continue
        if bidirectional:
            add_road_pair(
                graph,
                source,
                target,
                length_km=length_km,
                speed_kph=speed_kph,
                lanes=lanes,
                capacity_per_lane_vph=capacity_per_lane_vph,
            )
        else:
            graph.add_edge(
                source,
                target,
                length_km=length_km,
                speed_kph=speed_kph,
                lanes=lanes,
                capacity_vph=float(lanes * capacity_per_lane_vph),
                free_flow_min=length_km / speed_kph * 60.0,
                signal_delay_min=0.0,
                bridge=False,
            )
    if graph.number_of_edges() == 0:
        raise ValueError("The GIS layer did not produce any routable roads.")
    graph.graph["coordinate_units"] = "kilometers"
    return graph


def _gis_node(
    graph: nx.DiGraph,
    node_ids: dict[tuple[float, float], str],
    point: tuple[float, float],
) -> str:
    node = node_ids.get(point)
    if node is None:
        node = f"gis{len(node_ids)}"
        node_ids[point] = node
        graph.add_node(node, x=point[0] / 1000.0, y=point[1] / 1000.0)
    return node


def _line_parts(geometry: Any) -> Iterator[Any]:
    if geometry.geom_type == "LineString":
        yield geometry
    elif geometry.geom_type in {"MultiLineString", "GeometryCollection"}:
        for part in geometry.geoms:
            yield from _line_parts(part)


def _snap_line_endpoints(lines: list[Any], tolerance_m: float) -> list[Any]:
    if tolerance_m <= 0 or len(lines) < 2:
        return lines
    from shapely.geometry import LineString, Point
    from shapely.strtree import STRtree

    tree = STRtree(lines)
    adjusted = []
    for line_index, line in enumerate(lines):
        coordinates = list(line.coords)
        for endpoint_index in (0, -1):
            point = Point(coordinates[endpoint_index][:2])
            best: tuple[float, tuple[float, float]] | None = None
            for candidate_index in tree.query(point.buffer(tolerance_m)):
                candidate_index = int(candidate_index)
                if candidate_index == line_index:
                    continue
                candidate = lines[candidate_index]
                projection = candidate.interpolate(candidate.project(point))
                distance = point.distance(projection)
                if distance <= tolerance_m and (best is None or distance < best[0]):
                    best = (distance, (projection.x, projection.y))
            if best is not None:
                coordinates[endpoint_index] = best[1]
        adjusted.append(LineString(coordinates))
    return adjusted


def _validate_bbox(
    bbox: Sequence[float],
) -> tuple[float, float, float, float]:
    if len(bbox) != 4:
        raise ValueError("Bounding box must be west,south,east,north in WGS84.")
    west, south, east, north = (float(value) for value in bbox)
    if not all(math.isfinite(value) for value in (west, south, east, north)):
        raise ValueError("Bounding-box coordinates must be finite numbers.")
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError(
            "Bounding box must satisfy -180 <= west < east <= 180 "
            "and -90 <= south < north <= 90."
        )
    return west, south, east, north


@contextmanager
def _materialize_vector_source(content: bytes, filename: str) -> Generator[Path, None, None]:
    suffix = Path(filename).suffix.lower()
    with tempfile.TemporaryDirectory(prefix="rushhour-gis-") as temporary_directory:
        root = Path(temporary_directory)
        if suffix == ".zip":
            try:
                with ZipFile(BytesIO(content)) as archive:
                    entries = archive.infolist()
                    if sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES:
                        raise ValueError("The GIS archive expands beyond the 200 MB import limit.")
                    for entry in entries:
                        if stat.S_ISLNK(entry.external_attr >> 16):
                            raise ValueError("GIS ZIP archives cannot contain symbolic links.")
                        entry_path = PurePosixPath(entry.filename.replace("\\", "/"))
                        destination = (root / Path(*entry_path.parts)).resolve()
                        if not destination.is_relative_to(root.resolve()):
                            raise ValueError("The GIS ZIP archive contains an unsafe file path.")
                    archive.extractall(root)
            except BadZipFile as error:
                raise ValueError("The GIS upload is not a valid ZIP archive.") from error
            geodatabases = sorted(
                path for path in root.rglob("*.gdb") if path.is_dir()
            )
            shapefiles = sorted(root.rglob("*.shp"))
            if len(geodatabases) == 1 and not shapefiles:
                yield geodatabases[0]
                return
            if len(shapefiles) == 1 and not geodatabases:
                yield shapefiles[0]
                return
            if geodatabases or shapefiles:
                raise ValueError(
                    "The ZIP contains multiple GIS datasets. Upload one Shapefile or File Geodatabase at a time."
                )
            raise ValueError("The ZIP contains no .shp file or .gdb folder.")
        if suffix in {".geojson", ".json"}:
            source = root / Path(filename).name
            source.write_bytes(content)
            yield source
            return
        raise ValueError(
            "Upload GeoJSON directly, or upload a ZIP containing the complete Shapefile or File Geodatabase."
        )


def _geopandas() -> Any:
    try:
        import geopandas
    except ImportError as error:
        raise RuntimeError(
            "GIS vector import requires the optional dependencies. Install them with "
            "`python -m pip install -e '.[gis]'`."
        ) from error
    return geopandas


def _validate_public_url(url: str) -> None:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host or parts.username or parts.password:
        raise ValueError("GIS service URLs must use HTTPS.")
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("GIS service URL has an invalid port.") from error
    if port not in (None, 443):
        raise ValueError("GIS service URLs must use the standard HTTPS port.")
    if not any(host == domain or host.endswith(f".{domain}") for domain in ALLOWED_URL_DOMAINS):
        raise ValueError("Use a public HTTPS ArcGIS Online or NYC.gov GIS URL.")


def _fetch_arcgis_features(
    endpoint: str,
    original_query: dict[str, list[str]],
    bbox_wgs84: tuple[float, float, float, float] | None,
    *,
    where_clause: str | None = None,
) -> list[dict[str, Any]]:
    requested_fields = original_query.get("outFields", [""])[0].strip()
    if not requested_fields or requested_fields == "*":
        requested_fields = _arcgis_object_id_field(endpoint)
    params = {
        "where": where_clause or original_query.get("where", ["1=1"])[0],
        "outFields": requested_fields,
        "outSR": "4326",
        "f": "geojson",
    }
    if bbox_wgs84 is not None:
        west, south, east, north = _validate_bbox(bbox_wgs84)
        params.update(
            {
                "geometry": json.dumps(
                    {"xmin": west, "ymin": south, "xmax": east, "ymax": north}
                ),
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
            }
        )
    features: list[dict[str, Any]] = []
    offset = 0
    while True:
        page_params = {**params, "resultOffset": str(offset), "resultRecordCount": str(ARCGIS_PAGE_SIZE)}
        page_url = _url_with_query(endpoint, page_params)
        payload = _fetch_json(page_url)
        if "error" in payload:
            raise ValueError(f"ArcGIS returned an error: {payload['error']}")
        page = payload.get("features")
        if not isinstance(page, list):
            raise TypeError("The ArcGIS URL did not return GeoJSON features.")
        features.extend(page)
        if len(features) > MAX_FEATURES:
            raise ValueError(
                f"The service returned more than {MAX_FEATURES:,} features. "
                "Add a WGS84 bounding box or an ArcGIS WHERE filter."
            )
        has_more = payload.get("exceededTransferLimit", len(page) == ARCGIS_PAGE_SIZE)
        if not has_more:
            break
        if not page:
            raise ValueError("ArcGIS reported more results but returned an empty page.")
        offset += len(page)
    return features


def _arcgis_object_id_field(endpoint: str) -> str:
    layer_url = endpoint.removesuffix("/query").rstrip("/")
    metadata_url = _url_with_query(layer_url, {"f": "json"})
    metadata = _fetch_json(metadata_url)
    object_id_field = metadata.get("objectIdField")
    if isinstance(object_id_field, str) and object_id_field:
        return object_id_field
    for field in metadata.get("fields", []):
        if field.get("type") == "esriFieldTypeOID":
            name = field.get("name")
            if isinstance(name, str) and name:
                return name
    raise ValueError(
        "Could not identify the ArcGIS object ID field. Use an explicit outFields value in the URL."
    )


def _url_with_query(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def _fetch_json(url: str) -> dict[str, Any]:
    _validate_public_url(url)
    request = Request(url, headers={"User-Agent": "RushHour/1.0 GIS importer"})
    try:
        opener = build_opener(_PublicGISRedirectHandler())
        with opener.open(request, timeout=45) as response:
            content = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise ValueError(f"Could not retrieve the GIS service URL: {error}") from error
    if len(content) > MAX_RESPONSE_BYTES:
        raise ValueError("A GIS service response exceeded the 50 MB page limit.")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("The GIS service did not return valid JSON.") from error
    if not isinstance(payload, dict):
        raise TypeError("The GIS service response must be a JSON object.")
    return payload


class _PublicGISRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)
