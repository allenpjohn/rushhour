"""Road network construction, normalization, and optional OpenStreetMap import."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Iterable
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree

import networkx as nx

Edge = tuple[str, str]
OSM_FEATURE_CATEGORIES = {
    "Amenities and services": {"amenity": True},
    "Buildings": {"building": True},
    "Roads and paths": {"highway": True},
    "Land use and leisure": {"landuse": True, "leisure": True},
    "Natural features and water": {
        "natural": True,
        "water": True,
    },
    "Administrative boundaries": {"boundary": True},
    "Transit route relations": {
        "route": ["bus", "tram", "train", "subway", "trolleybus"]
    },
    "Turn-restriction relations": {"type": "restriction"},
}
OVERPASS_URL_ENV = "RUSHHOUR_OVERPASS_URL"


def build_demo_network(rows: int = 5, columns: int = 5) -> nx.DiGraph:
    """Build a deterministic, compact urban grid with a marked central arterial."""
    if rows < 3 or columns < 3:
        raise ValueError("The demo network requires at least three rows and columns.")

    graph = nx.DiGraph(name="RushHour demo urban grid")
    for row in range(rows):
        for column in range(columns):
            node = f"r{row}c{column}"
            graph.add_node(node, x=float(column), y=float(rows - row - 1))

    for row in range(rows):
        for column in range(columns):
            source = f"r{row}c{column}"
            neighbors = []
            if column + 1 < columns:
                neighbors.append((row, column + 1))
            if row + 1 < rows:
                neighbors.append((row + 1, column))
            for next_row, next_column in neighbors:
                target = f"r{next_row}c{next_column}"
                is_arterial = row == rows // 2 and next_row == row
                add_road_pair(
                    graph,
                    source,
                    target,
                    length_km=0.55,
                    speed_kph=50.0 if is_arterial else 35.0,
                    lanes=2 if is_arterial else 1,
                    bridge=is_arterial and columns // 2 - 1 <= column < columns // 2 + 1,
                )

    return graph


def add_road_pair(
    graph: nx.DiGraph,
    first: str,
    second: str,
    *,
    length_km: float,
    speed_kph: float,
    lanes: int,
    capacity_per_lane_vph: float = 900.0,
    bridge: bool = False,
    signal_delay_min: float = 0.0,
) -> None:
    """Add both travel directions using consistent simulation edge attributes."""
    for source, target in ((first, second), (second, first)):
        graph.add_edge(
            source,
            target,
            length_km=float(length_km),
            speed_kph=float(speed_kph),
            lanes=int(lanes),
            capacity_vph=float(lanes * capacity_per_lane_vph),
            free_flow_min=float(length_km / speed_kph * 60.0),
            signal_delay_min=float(signal_delay_min),
            bridge=bridge,
        )


def default_demand(
    graph: nx.DiGraph,
    scale: float = 1.0,
    *,
    max_samples: int = 5,
) -> list[tuple[str, str, float]]:
    """Create a deterministic, directional peak-hour commute demand matrix."""
    if scale <= 0:
        raise ValueError("Demand scale must be greater than zero.")
    if max_samples < 2:
        raise ValueError("Demand sampling requires at least two edge samples.")
    if graph.number_of_nodes() == 0:
        raise ValueError("Cannot create demand for an empty road network.")
    graph = normalize_graph(graph)
    component = max(nx.strongly_connected_components(graph), key=len)
    usable_nodes = list(component)
    if len(usable_nodes) < 2:
        raise ValueError("Network needs a strongly connected component with at least two nodes.")
    x = {node: float(graph.nodes[node].get("x", 0.0)) for node in usable_nodes}
    y = {node: float(graph.nodes[node].get("y", 0.0)) for node in usable_nodes}
    min_x, max_x = min(x.values()), max(x.values())
    min_y, max_y = min(y.values()), max(y.values())
    sample_count = min(max_samples, max(2, int(len(usable_nodes) ** 0.5)))

    def edge_samples(candidates: list, coordinate: dict, count: int) -> list:
        ordered = sorted(candidates, key=lambda node: (coordinate[node], str(node)))
        if count == 1:
            return [ordered[0]]
        return [
            ordered[index * (len(ordered) - 1) // (count - 1)]
            for index in range(count)
        ]

    west_nodes = [node for node in usable_nodes if x[node] <= min_x + (max_x - min_x) * 0.15]
    east_nodes = [node for node in usable_nodes if x[node] >= max_x - (max_x - min_x) * 0.15]
    south_nodes = [node for node in usable_nodes if y[node] <= min_y + (max_y - min_y) * 0.15]
    north_nodes = [node for node in usable_nodes if y[node] >= max_y - (max_y - min_y) * 0.15]
    west_count = min(sample_count, len(west_nodes))
    east_count = min(sample_count, len(east_nodes))
    south_count = min(sample_count, len(south_nodes))
    north_count = min(sample_count, len(north_nodes))
    west = edge_samples(west_nodes, y, west_count)
    east = edge_samples(east_nodes, y, east_count)
    south = edge_samples(south_nodes, x, south_count)
    north = edge_samples(north_nodes, x, north_count)
    pairs: dict[tuple[str, str], float] = {}
    for index, origin in enumerate(west):
        for destination_index, destination in enumerate(east):
            if origin != destination:
                pairs[(str(origin), str(destination))] = 125.0 + 20.0 * (index + destination_index)
    for index, origin in enumerate(south):
        destination = north[(index + 1) % len(north)]
        if origin != destination:
            key = (str(origin), str(destination))
            pairs[key] = pairs.get(key, 0.0) + 90.0
    return [(origin, destination, volume * scale) for (origin, destination), volume in pairs.items()]


def normalize_graph(graph: nx.Graph) -> nx.DiGraph:
    """Convert a directed or undirected graph into a validated simple digraph."""
    if graph.is_directed():
        edges: Iterable[tuple[str, str, dict]] = graph.edges(data=True)
    else:
        edges = (
            directed_edge
            for source, target, data in graph.edges(data=True)
            for directed_edge in ((source, target, data), (target, source, data))
        )

    normalized = nx.DiGraph(**graph.graph)
    normalized.graph.setdefault("name", "Imported road network")
    normalized.add_nodes_from(
        (str(node), {**data, "x": float(data.get("x", index)), "y": float(data.get("y", 0.0))})
        for index, (node, data) in enumerate(graph.nodes(data=True))
    )
    for source, target, attributes in edges:
        if "length_km" in attributes:
            length_km = _number(attributes["length_km"], 0.5)
        else:
            length_km = _number(attributes.get("length", 500.0), 500.0) / 1000.0
        speed_kph = max(_speed_kph(attributes), 5.0)
        lanes = max(1, int(_number(attributes.get("lanes", 1), 1)))
        capacity = max(
            _number(attributes.get("capacity_vph", lanes * 900), lanes * 900), 1.0
        )
        edge_data = {
            "length_km": max(length_km, 0.01),
            "speed_kph": speed_kph,
            "lanes": lanes,
            "capacity_vph": capacity,
            "free_flow_min": max(
                _number(attributes.get("free_flow_min", length_km / speed_kph * 60.0), 1.0),
                0.01,
            ),
            "reference_free_flow_min": max(
                _number(
                    attributes.get(
                        "reference_free_flow_min",
                        attributes.get("free_flow_min", length_km / speed_kph * 60.0),
                    ),
                    1.0,
                ),
                0.01,
            ),
            "signal_delay_min": _number(attributes.get("signal_delay_min", 0.0), 0.0),
            "bridge": _boolean(attributes.get("bridge", False)),
        }
        for key in (
            "access",
            "junction",
            "maxspeed",
            "oneway",
            "osmid",
            "osm_element_type",
            "osm_geometry_wkt",
            "osm_tags",
            "road_class",
        ):
            if key in attributes:
                edge_data[key] = _tag_string(attributes[key])
        if "highway" in attributes:
            edge_data["road_class"] = _tag_string(attributes["highway"])
        edge_source, edge_target = str(source), str(target)
        if normalized.has_edge(edge_source, edge_target):
            previous = normalized[edge_source][edge_target]
            combined_capacity = previous["capacity_vph"] + capacity
            previous["free_flow_min"] = (
                previous["free_flow_min"] * previous["capacity_vph"]
                + edge_data["free_flow_min"] * capacity
            ) / combined_capacity
            previous["reference_free_flow_min"] = (
                previous["reference_free_flow_min"] * previous["capacity_vph"]
                + edge_data["reference_free_flow_min"] * capacity
            ) / combined_capacity
            previous["capacity_vph"] = combined_capacity
            previous["lanes"] += lanes
            previous["bridge"] = previous["bridge"] or edge_data["bridge"]
        else:
            normalized.add_edge(edge_source, edge_target, **edge_data)
    if normalized.number_of_nodes() == 0 or normalized.number_of_edges() == 0:
        raise ValueError("The road network must contain at least one node and one edge.")
    return normalized


def load_graphml(path: str | Path) -> nx.DiGraph:
    """Load a GraphML road network and normalize its edge attributes."""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"Network file does not exist: {source}")
    return normalize_graph(nx.read_graphml(source))


def load_graphml_bytes(content: bytes) -> nx.DiGraph:
    """Load an uploaded GraphML document without staging it in a temporary file."""
    try:
        graph = nx.parse_graphml(content.decode("utf-8"))
    except (UnicodeDecodeError, nx.NetworkXError, ElementTree.ParseError, ValueError) as error:
        raise ValueError("The uploaded file is not valid UTF-8 GraphML.") from error
    return normalize_graph(graph)


def download_osm_network(place: str, output: str | Path, network_type: str = "drive") -> Path:
    """Download a drivable OpenStreetMap graph and save it as GraphML."""
    graph = load_osm_network(place, network_type=network_type)
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    nx.write_graphml(graph, destination)
    return destination


def load_osm_network(place: str, network_type: str = "drive") -> nx.DiGraph:
    """Download and normalize a drivable OpenStreetMap network for a named place."""
    place = place.strip()
    if not place:
        raise ValueError("Enter a place name or address to load an OpenStreetMap network.")
    try:
        ox: Any = import_module("osmnx")
    except ImportError as error:
        raise RuntimeError(
            "OpenStreetMap import requires the optional dependency; install rushhour[osm]."
        ) from error

    _configure_overpass(ox)
    try:
        graph = ox.graph_from_place(place, network_type=network_type, simplify=True)
    except (ValueError, nx.NetworkXError) as error:
        raise ValueError(
            f"Could not download an OpenStreetMap network for {place!r}: "
            f"{error}{_overpass_capacity_hint(error)}"
        ) from error
    for _, attributes in graph.nodes(data=True):
        longitude = _number(attributes.get("x"), 0.0)
        latitude = _number(attributes.get("y"), 0.0)
        attributes["longitude"] = longitude
        attributes["latitude"] = latitude
        attributes["osm_element_type"] = "node"
        attributes["osm_geometry_wkt"] = f"POINT ({longitude} {latitude})"
        attributes["osm_tags"] = _json_tag_data(attributes)
    for source, target, attributes in graph.edges(data=True):
        geometry = attributes.get("geometry")
        if geometry is not None and hasattr(geometry, "wkt"):
            attributes["osm_geometry_wkt"] = geometry.wkt
        else:
            start = graph.nodes[source]
            end = graph.nodes[target]
            attributes["osm_geometry_wkt"] = (
                f"LINESTRING ({start['x']} {start['y']}, {end['x']} {end['y']})"
            )
        attributes["osm_element_type"] = "way"
        attributes["osm_tags"] = _json_tag_data(attributes)
    normalized = normalize_graph(graph)
    normalized.graph.update(
        source_format="openstreetmap",
        source_name=place,
        network_type=network_type,
        attribution="© OpenStreetMap contributors",
        rushhour_source_signature=f"osm:{network_type}:{place.casefold()}",
    )
    return normalized


def load_osm_features(place: str, categories: Iterable[str]) -> Any:
    """Fetch selected OSM feature tags as geometries, without treating them as roads."""
    place = place.strip()
    selected_categories = tuple(dict.fromkeys(categories))
    if not place:
        raise ValueError("Enter a place name or address to load OpenStreetMap features.")
    if not selected_categories:
        raise ValueError("Select at least one OpenStreetMap feature category.")
    unknown_categories = set(selected_categories) - OSM_FEATURE_CATEGORIES.keys()
    if unknown_categories:
        unknown = ", ".join(sorted(unknown_categories))
        raise ValueError(f"Unknown OpenStreetMap feature category: {unknown}.")
    try:
        ox: Any = import_module("osmnx")
    except ImportError as error:
        raise RuntimeError(
            "OpenStreetMap import requires OSMnx in the active Python environment."
        ) from error

    _configure_overpass(ox)
    tags: dict[str, bool | str | list[str]] = {}
    for category in selected_categories:
        tags.update(OSM_FEATURE_CATEGORIES[category])
    try:
        features = ox.features_from_place(place, tags=tags)
    except (ValueError, nx.NetworkXError) as error:
        raise ValueError(
            f"Could not download OpenStreetMap features for {place!r}: "
            f"{error}{_overpass_capacity_hint(error)}"
        ) from error
    if features.empty:
        raise ValueError(
            f"No OpenStreetMap features for {', '.join(selected_categories)} were found in {place!r}."
        )
    if len(features) > 100_000:
        raise ValueError(
            "The selected OpenStreetMap query returned more than 100,000 features. "
            "Choose a smaller place or fewer feature categories."
        )
    features.attrs["source_place"] = place
    features.attrs["source_categories"] = selected_categories
    return features


def _configure_overpass(osmnx: Any) -> None:
    """Apply an operator-configured Overpass API endpoint, if provided."""
    endpoint = os.getenv(OVERPASS_URL_ENV, "").strip()
    if not endpoint:
        return
    parts = urlsplit(endpoint)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError(
            f"{OVERPASS_URL_ENV} must be an HTTPS Overpass API base URL without credentials."
        )
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError(f"{OVERPASS_URL_ENV} has an invalid port.") from error
    if port not in (None, 443) or parts.query or parts.fragment:
        raise ValueError(
            f"{OVERPASS_URL_ENV} must use standard HTTPS and cannot include a query or fragment."
        )
    osmnx.settings.overpass_url = endpoint.rstrip("/")


def _overpass_capacity_hint(error: Exception) -> str:
    message = str(error)
    if "overpass" in message.casefold() and re.search(r"\b(?:429|502|503|504)\b", message):
        return (
            " This response comes from the upstream Overpass service (capacity or rate limit), "
            "not RushHour's route-assignment model. Retry later, or set "
            f"{OVERPASS_URL_ENV} to another compatible HTTPS Overpass API base URL and restart "
            "Streamlit. Keep the requested area small."
        )
    return ""


def _json_tag_data(attributes: dict) -> str:
    return json.dumps(
        {
            key: value
            for key, value in attributes.items()
            if key
            not in {
                "x",
                "y",
                "latitude",
                "longitude",
                "geometry",
                "osm_element_type",
                "osm_geometry_wkt",
                "osm_tags",
            }
        },
        sort_keys=True,
        default=str,
    )


def _number(value: object, fallback: float) -> float:
    if isinstance(value, (list, tuple)):
        return _number(value[0], fallback) if value else fallback
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else fallback
    if isinstance(value, str):
        try:
            number = float(value.split(";")[0])
            return number if math.isfinite(number) else fallback
        except ValueError:
            return fallback
    return fallback


def _boolean(value: object) -> bool:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes"}:
            return True
        if normalized in {"0", "false", "no", "none", ""}:
            return False
    return bool(value)


def _tag_string(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return ";".join(str(item) for item in value)
    return str(value)


def _speed_kph(attributes: dict) -> float:
    value = attributes.get("speed_kph", attributes.get("speed_limit", attributes.get("maxspeed", 35.0)))
    if isinstance(value, (list, tuple)):
        return _speed_kph({"maxspeed": value[0]}) if value else 35.0
    if isinstance(value, str):
        match = re.match(r"\s*(\d+(?:\.\d+)?)\s*(mph|km/?h)?", value.split(";")[0], re.IGNORECASE)
        if match:
            speed = float(match.group(1))
            return speed * 1.60934 if match.group(2) and match.group(2).lower() == "mph" else speed
        return 35.0
    return _number(value, 35.0)
