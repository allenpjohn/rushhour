import sys
from types import SimpleNamespace

import networkx as nx
import pytest

from rushhour.networks import (
    _configure_overpass,
    default_demand,
    load_graphml,
    load_graphml_bytes,
    load_osm_features,
    load_osm_network,
    normalize_graph,
)


def test_normalize_graph_handles_graphml_style_attributes():
    graph = nx.DiGraph()
    graph.add_node("west", x=0.0, y=0.0)
    graph.add_node("east", x=1.0, y=0.0)
    graph.add_edge(
        "west",
        "east",
        length="500",
        maxspeed="25 mph",
        lanes="2",
        bridge="False",
    )
    graph.add_edge("east", "west", length="500", maxspeed="25 mph", lanes="2")

    normalized = normalize_graph(graph)

    assert normalized["west"]["east"]["length_km"] == pytest.approx(0.5)
    assert normalized["west"]["east"]["speed_kph"] == pytest.approx(40.2335)
    assert normalized["west"]["east"]["free_flow_min"] == pytest.approx(0.5 / 40.2335 * 60)
    assert not normalized["west"]["east"]["bridge"]
    assert default_demand(normalized)


def test_default_demand_normalizes_non_string_node_ids():
    graph = nx.DiGraph()
    graph.add_node(1, x=0.0, y=0.0)
    graph.add_node(2, x=1.0, y=0.0)
    graph.add_edge(1, 2)
    graph.add_edge(2, 1)

    assert default_demand(graph)


def test_default_demand_can_reduce_od_samples_for_large_networks():
    graph = nx.DiGraph()
    for index in range(20):
        graph.add_node(str(index), x=float(index), y=0.0)
    for index in range(19):
        graph.add_edge(str(index), str(index + 1))
        graph.add_edge(str(index + 1), str(index))

    full = default_demand(graph)
    reduced = default_demand(graph, max_samples=3)

    assert len(reduced) < len(full)
    assert len(reduced) <= 12


def test_load_graphml_imports_and_aggregates_parallel_roads(tmp_path):
    graph = nx.MultiDiGraph()
    graph.add_node(1, x=0.0, y=0.0)
    graph.add_node(2, x=1.0, y=0.0)
    graph.add_edge(1, 2, length=500.0, maxspeed="30 mph", lanes=1)
    graph.add_edge(1, 2, length=600.0, maxspeed="40", lanes=2)
    graph.add_edge(2, 1, length=500.0, maxspeed="30 mph", lanes=1)
    source = tmp_path / "roads.graphml"
    nx.write_graphml(graph, source)

    imported = load_graphml(source)

    assert imported.number_of_edges() == 2
    assert imported["1"]["2"]["capacity_vph"] == 2700
    assert imported["1"]["2"]["lanes"] == 3
    assert imported["1"]["2"]["speed_kph"] == pytest.approx(30 * 1.60934)


def test_load_graphml_bytes_reports_invalid_xml():
    with pytest.raises(ValueError, match="valid UTF-8 GraphML"):
        load_graphml_bytes(b"<unclosed")


def test_load_osm_network_preserves_direction_and_mapped_road_attributes(monkeypatch):
    osm_graph = nx.MultiDiGraph()
    osm_graph.add_node(101, x=-71.1, y=42.3)
    osm_graph.add_node(202, x=-71.09, y=42.31)
    osm_graph.add_edge(
        101,
        202,
        length=2000.0,
        maxspeed=["25 mph", "30 mph"],
        lanes=["2", "3"],
        bridge="yes",
        highway=["primary", "link"],
        geometry=SimpleNamespace(wkt="LINESTRING (-71.1 42.3, -71.09 42.31)"),
    )
    requests = []

    def graph_from_place(place, *, network_type, simplify):
        requests.append((place, network_type, simplify))
        return osm_graph

    monkeypatch.setitem(
        sys.modules,
        "osmnx",
        SimpleNamespace(graph_from_place=graph_from_place),
    )

    graph = load_osm_network("Cambridge, Massachusetts, USA")

    assert requests == [("Cambridge, Massachusetts, USA", "drive", True)]
    assert graph.has_edge("101", "202")
    assert not graph.has_edge("202", "101")
    assert graph["101"]["202"]["length_km"] == pytest.approx(2.0)
    assert graph["101"]["202"]["speed_kph"] == pytest.approx(25 * 1.60934)
    assert graph["101"]["202"]["lanes"] == 2
    assert graph["101"]["202"]["bridge"]
    assert graph["101"]["202"]["road_class"] == "primary;link"
    assert graph["101"]["202"]["osm_geometry_wkt"].startswith("LINESTRING")
    assert graph.nodes["101"]["longitude"] == pytest.approx(-71.1)
    assert graph.nodes["101"]["latitude"] == pytest.approx(42.3)
    assert graph.nodes["101"]["osm_element_type"] == "node"
    assert graph.nodes["101"]["osm_geometry_wkt"].startswith("POINT")
    assert graph["101"]["202"]["osm_element_type"] == "way"
    assert '"highway": ["primary", "link"]' in graph["101"]["202"]["osm_tags"]
    assert graph.graph["source_format"] == "openstreetmap"
    assert graph.graph["attribution"] == "© OpenStreetMap contributors"


def test_osm_overpass_endpoint_can_be_configured(monkeypatch):
    osmnx = SimpleNamespace(settings=SimpleNamespace(overpass_url="https://default.example/api"))
    monkeypatch.setenv(
        "RUSHHOUR_OVERPASS_URL",
        "https://alternate.example/api/",
    )

    _configure_overpass(osmnx)

    assert osmnx.settings.overpass_url == "https://alternate.example/api"


def test_osm_overpass_endpoint_requires_https(monkeypatch):
    osmnx = SimpleNamespace(settings=SimpleNamespace(overpass_url="https://default.example/api"))
    monkeypatch.setenv("RUSHHOUR_OVERPASS_URL", "http://alternate.example/api")

    with pytest.raises(ValueError, match="must be an HTTPS Overpass API base URL"):
        _configure_overpass(osmnx)


def test_osm_network_explains_overpass_capacity_errors(monkeypatch):
    def graph_from_place(place, *, network_type, simplify):
        raise ValueError("overpass-api.de responded: 502 Gateway Timeout")

    monkeypatch.setitem(
        sys.modules,
        "osmnx",
        SimpleNamespace(graph_from_place=graph_from_place),
    )
    monkeypatch.delenv("RUSHHOUR_OVERPASS_URL", raising=False)

    with pytest.raises(ValueError, match="upstream Overpass service") as error:
        load_osm_network("Kendall Square")

    assert "RUSHHOUR_OVERPASS_URL" in str(error.value)


def test_load_osm_features_queries_selected_categories_and_preserves_geometries(monkeypatch):
    class FeatureFrame:
        def __init__(self):
            self.empty = False
            self.attrs = {}

        def __len__(self):
            return 4

    result = FeatureFrame()
    requests = []

    def features_from_place(place, *, tags):
        requests.append((place, tags))
        return result

    monkeypatch.setitem(
        sys.modules,
        "osmnx",
        SimpleNamespace(features_from_place=features_from_place),
    )

    features = load_osm_features(
        "Manhattan, New York",
        ("Amenities and services", "Buildings", "Turn-restriction relations"),
    )

    assert features is result
    assert requests == [
        (
            "Manhattan, New York",
            {"amenity": True, "building": True, "type": "restriction"},
        )
    ]
    assert features.attrs["source_place"] == "Manhattan, New York"
    assert features.attrs["source_categories"] == (
        "Amenities and services",
        "Buildings",
        "Turn-restriction relations",
    )


def test_load_osm_network_requires_a_place_name():
    with pytest.raises(ValueError, match="Enter a place name"):
        load_osm_network("  ")


def test_osm_no_tags_are_not_treated_as_true():
    graph = nx.DiGraph()
    graph.add_node("a", x=0.0, y=0.0)
    graph.add_node("b", x=1.0, y=0.0)
    graph.add_edge("a", "b", bridge="no")

    assert not normalize_graph(graph)["a"]["b"]["bridge"]
