from types import SimpleNamespace

import matplotlib.pyplot as plt
import networkx as nx
import pytest
from matplotlib.collections import LineCollection, PathCollection

from rushhour.networks import add_road_pair
from rushhour.visualization import draw_network


def test_congestion_map_uses_neutral_roads_and_colors_only_active_links():
    graph = nx.DiGraph()
    graph.add_node("a", x=0.0, y=0.0)
    graph.add_node("b", x=1.0, y=0.0)
    add_road_pair(graph, "a", "b", length_km=1.0, speed_kph=30, lanes=1)
    result = SimpleNamespace(edge_flows={("a", "b"): 450.0, ("b", "a"): 0.0})

    figure = draw_network(graph, result, color_scale_max=1.0)
    try:
        collections = figure.axes[0].collections
        line_collections = [
            item for item in collections if isinstance(item, LineCollection)
        ]
        active_collection = next(item for item in line_collections if item.get_array() is not None)

        assert active_collection.get_array().tolist() == pytest.approx([0.5])
        assert active_collection.get_edgecolors()[0][:3] != pytest.approx(
            [0.0, 0.0, 0.0]
        )
        assert next(
            item for item in line_collections if item.get_array() is None
        ).get_edgecolors()[0][:3] == pytest.approx([0.835, 0.871, 0.91], abs=0.01)
    finally:
        plt.close(figure)


def test_large_network_map_skips_overlapping_intersection_markers():
    graph = nx.DiGraph()
    graph.add_nodes_from(
        (f"n{index}", {"x": float(index), "y": 0.0}) for index in range(5_001)
    )
    graph.add_edges_from((f"n{index}", f"n{index + 1}") for index in range(5_000))

    figure = draw_network(graph)
    try:
        assert not any(
            isinstance(item, PathCollection) for item in figure.axes[0].collections
        )
    finally:
        plt.close(figure)
