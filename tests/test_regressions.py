import builtins
import json
from io import BytesIO
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request
from zipfile import ZipFile

import networkx as nx
import pymupdf
import pytest
from PIL import Image

from rushhour import DriverClass, build_demo_network, default_demand, simulate
from rushhour.canvas_compat import install_image_to_url_compat
from rushhour.gis import (
    _fetch_arcgis_features,
    _materialize_vector_source,
    _PublicGISRedirectHandler,
    _validate_bbox,
    _validate_public_url,
    discover_local_network_files,
)
from rushhour.networks import add_road_pair, normalize_graph
from rushhour.scenarios import apply_scenario
from rushhour.tracing import extract_canvas_lines, lines_to_graph, render_pdf_page


def test_zero_snap_tolerance_still_connects_exact_crossings():
    graph = lines_to_graph(
        [(10, 50, 90, 50), (50, 10, 50, 90)],
        canvas_width=100,
        scale_km_per_pixel=0.01,
        snap_tolerance_px=0,
    )

    assert graph.number_of_nodes() == 5
    assert graph.number_of_edges() == 8


def test_endpoint_within_snap_tolerance_connects_to_segment_interior():
    graph = lines_to_graph(
        [(10, 50, 90, 50), (50, 53, 50, 80)],
        canvas_width=100,
        scale_km_per_pixel=0.01,
        snap_tolerance_px=4,
    )

    assert graph.number_of_nodes() == 4
    assert graph.number_of_edges() == 6


def test_opposite_one_way_segments_remain_distinct_edges():
    graph = lines_to_graph(
        [(0, 0, 10, 0), (10, 0, 0, 0)],
        canvas_width=100,
        scale_km_per_pixel=0.01,
        bidirectional=False,
        snap_tolerance_px=0,
    )

    assert graph.number_of_edges() == 2


def test_fabric_line_origin_is_applied_when_extracting_coordinates():
    lines = extract_canvas_lines(
        {
            "objects": [
                {
                    "type": "line",
                    "left": 10,
                    "top": 25,
                    "width": 80,
                    "height": 0,
                    "originX": "left",
                    "originY": "top",
                    "x1": -40,
                    "y1": 0,
                    "x2": 40,
                    "y2": 0,
                },
                {
                    "type": "line",
                    "left": 50,
                    "top": 25,
                    "width": 80,
                    "height": 0,
                    "originX": "center",
                    "originY": "center",
                    "x1": -40,
                    "y1": 0,
                    "x2": 40,
                    "y2": 0,
                },
            ]
        }
    )

    assert lines == [(10.0, 25.0, 90.0, 25.0), (10.0, 25.0, 90.0, 25.0)]


def test_lower_speed_limit_keeps_original_free_flow_reference():
    base = build_demo_network()
    edge = next(
        (source, target)
        for source, target, data in base.edges(data=True)
        if data["bridge"]
    )
    original_free_flow = base[edge[0]][edge[1]]["free_flow_min"]
    changed = normalize_graph(apply_scenario(base, "lower_speed_limit"))

    assert changed[edge[0]][edge[1]]["free_flow_min"] > original_free_flow
    assert changed[edge[0]][edge[1]]["reference_free_flow_min"] == pytest.approx(
        original_free_flow
    )


def test_lower_speed_limit_increases_delay_above_fixed_reference():
    base = build_demo_network()
    demand = default_demand(base)
    drivers = [DriverClass("all drivers", 1.0)]
    baseline = simulate(base, demand, drivers, max_iterations=20)
    speed_limit = simulate(
        apply_scenario(base, "lower_speed_limit"),
        demand,
        drivers,
        max_iterations=20,
    )

    assert speed_limit.metrics.total_delay_vehicle_min > baseline.metrics.total_delay_vehicle_min


def test_iteration_limit_reports_gap_for_final_flows():
    graph = nx.DiGraph()
    for node, coordinates in {
        "o": (0, 0),
        "a": (1, 1),
        "b": (1, -1),
        "d": (2, 0),
    }.items():
        graph.add_node(node, x=coordinates[0], y=coordinates[1])
    add_road_pair(graph, "o", "a", length_km=0.5, speed_kph=60, lanes=1)
    add_road_pair(graph, "a", "d", length_km=0.5, speed_kph=60, lanes=1)
    add_road_pair(graph, "o", "b", length_km=0.7, speed_kph=60, lanes=20)
    add_road_pair(graph, "b", "d", length_km=0.7, speed_kph=60, lanes=20)

    result = simulate(graph, [("o", "d", 5000.0)], max_iterations=1)

    assert result.iterations == 1
    assert result.relative_gap > 0.5


def test_zero_share_driver_class_does_not_change_assignment():
    graph = build_demo_network()
    demand = default_demand(graph)
    drivers = [DriverClass("all drivers", 1.0)]
    active = simulate(graph, demand, drivers, max_iterations=5)
    with_zero_share = simulate(
        graph,
        demand,
        [DriverClass("inactive", 0.0), DriverClass("active", 1.0)],
        max_iterations=5,
    )

    assert with_zero_share.edge_flows == active.edge_flows
    assert with_zero_share.relative_gap == pytest.approx(active.relative_gap)


def test_pdf_dependency_is_loaded_only_for_pdf_rendering(monkeypatch):
    original_import = builtins.__import__

    def without_pymupdf(name, *args, **kwargs):
        if name == "pymupdf":
            raise ImportError("disabled for test")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_pymupdf)
    assert extract_canvas_lines({"objects": []}) == []
    with pytest.raises(RuntimeError, match="requires PyMuPDF"):
        render_pdf_page(b"pdf", 0)


def test_pdf_document_is_closed_after_rendering(monkeypatch):
    source = pymupdf.open()
    source.new_page(width=100, height=100)
    content = source.tobytes()
    source.close()
    opened_documents = []
    original_open = pymupdf.open

    def tracked_open(*args, **kwargs):
        document = original_open(*args, **kwargs)
        opened_documents.append(document)
        return document

    monkeypatch.setattr(pymupdf, "open", tracked_open)

    render_pdf_page(content, 0, width=20, height=20)

    assert opened_documents[0].is_closed


def test_streamlit_image_compat_is_conditional_and_returns_data_url():
    existing_module = ModuleType("streamlit.elements.image")

    def existing_helper():
        return "existing"

    existing_module.image_to_url = existing_helper
    install_image_to_url_compat(existing_module)
    assert existing_module.image_to_url is existing_helper
    assert existing_module.image_to_url() == "existing"

    missing_module = ModuleType("streamlit.elements.image")
    install_image_to_url_compat(missing_module)
    image_url = missing_module.image_to_url(Image.new("RGB", (1, 1), "white"))
    assert image_url.startswith("data:image/png;base64,")


def test_arcgis_query_results_are_paged_and_bounded(monkeypatch):
    from rushhour import gis

    monkeypatch.setattr(gis, "ARCGIS_PAGE_SIZE", 2)
    pages = {
        0: [{}, {}],
        2: [{}, {}],
        4: [{}],
    }
    requested = []

    def fetch_page(url):
        query = parse_qs(urlsplit(url).query)
        if query.get("f") == ["json"]:
            return {"objectIdField": "OBJECTID"}
        offset = int(query["resultOffset"][0])
        requested.append(query)
        return {"features": pages[offset]}

    monkeypatch.setattr(gis, "_fetch_json", fetch_page)
    features = _fetch_arcgis_features(
        "https://services5.arcgis.com/example/FeatureServer/0/query",
        {"outFields": ["*"], "where": ["1=1"]},
        (-74.1, 40.5, -73.7, 40.9),
        where_clause="CLASS_CODE = '1'",
    )

    assert len(features) == 5
    assert [int(query["resultOffset"][0]) for query in requested] == [0, 2, 4]
    assert requested[0]["outFields"] == ["OBJECTID"]
    assert requested[0]["where"] == ["CLASS_CODE = '1'"]
    assert requested[0]["geometryType"] == ["esriGeometryEnvelope"]
    assert requested[0]["outSR"] == ["4326"]


def test_gis_bounding_box_and_remote_host_validation():
    assert _validate_bbox((-74.1, 40.5, -73.7, 40.9)) == (
        -74.1,
        40.5,
        -73.7,
        40.9,
    )
    with pytest.raises(ValueError, match="Bounding box"):
        _validate_bbox((-74.1, 40.9, -73.7, 40.5))
    _validate_public_url("https://services5.arcgis.com/example/FeatureServer/0")
    with pytest.raises(ValueError, match="ArcGIS Online or NYC.gov"):
        _validate_public_url("https://example.com/roads.geojson")
    with pytest.raises(ValueError, match="standard HTTPS port"):
        _validate_public_url("https://services5.arcgis.com:8443/example/FeatureServer/0")


def test_gis_redirects_cannot_leave_approved_hosts():
    request = Request("https://services5.arcgis.com/example/FeatureServer/0")
    with pytest.raises(ValueError, match="ArcGIS Online or NYC.gov"):
        _PublicGISRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://127.0.0.1/private",
        )


def test_zip_import_rejects_paths_outside_archive_root():
    archive_content = BytesIO()
    with ZipFile(archive_content, "w") as archive:
        archive.writestr("../unsafe.shp", b"not a shapefile")

    with (
        pytest.raises(ValueError, match="unsafe file path"),
        _materialize_vector_source(archive_content.getvalue(), "roads.zip"),
    ):
        pass


def test_local_network_discovery_is_recursive_and_ignores_hidden_files(tmp_path):
    city_dir = tmp_path / "tallahassee"
    city_dir.mkdir()
    (city_dir / "roads.geojson").write_text("{}", encoding="utf-8")
    (city_dir / "streets.graphml").write_text("<graphml/>", encoding="utf-8")
    (city_dir / "notes.txt").write_text("not a network", encoding="utf-8")
    hidden_dir = city_dir / ".cache"
    hidden_dir.mkdir()
    (hidden_dir / "ignored.geojson").write_text("{}", encoding="utf-8")

    assert discover_local_network_files(tmp_path) == (
        city_dir / "roads.geojson",
        city_dir / "streets.graphml",
    )


def test_geojson_network_import_nodes_crossing_roads_when_gis_extra_is_installed():
    pytest.importorskip("geopandas")
    from rushhour.gis import load_vector_network

    content = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [-74.0, 40.7],
                            [-73.999, 40.7],
                        ],
                    },
                },
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [-73.9995, 40.6995],
                            [-73.9995, 40.7005],
                        ],
                    },
                },
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [-75.0, 41.0],
                            [-74.999, 41.0],
                        ],
                    },
                },
            ],
        }
    ).encode("utf-8")

    graph = load_vector_network(
        content,
        "roads.geojson",
        bbox_wgs84=(-74.1, 40.6, -73.9, 40.8),
        lanes=2,
        capacity_per_lane_vph=1200,
    )

    assert graph.number_of_nodes() == 5
    assert graph.number_of_edges() == 8
    assert next(iter(graph.edges(data=True)))[2]["capacity_vph"] == 2400
    assert graph.graph["rushhour_source_signature"]


def test_zipped_shapefile_import_builds_a_routable_graph(tmp_path):
    geopandas = pytest.importorskip("geopandas")
    from shapely.geometry import LineString

    shapefile = tmp_path / "roads.shp"
    geopandas.GeoDataFrame(
        {"road": ["one"]},
        geometry=[LineString([(-74.0, 40.7), (-73.999, 40.7)])],
        crs="EPSG:4326",
    ).to_file(shapefile)
    archive_content = BytesIO()
    with ZipFile(archive_content, "w") as archive:
        for source in tmp_path.iterdir():
            archive.write(source, Path("roads") / source.name)

    from rushhour.gis import load_vector_network

    graph = load_vector_network(
        archive_content.getvalue(),
        "roads.zip",
        bbox_wgs84=(-74.1, 40.6, -73.9, 40.8),
    )

    assert graph.number_of_nodes() == 2
    assert graph.number_of_edges() == 2
