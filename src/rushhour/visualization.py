"""Network map rendering for RushHour's Streamlit dashboard."""

from __future__ import annotations

import matplotlib.pyplot as plt
import networkx as nx
from matplotlib.collections import LineCollection
from matplotlib.colors import Normalize

LARGE_NETWORK_NODE_LIMIT = 5_000


def draw_network(graph: nx.DiGraph, result=None, *, color_scale_max: float = 1.0):
    """Draw neutral road geometry and highlight links carrying assigned flow."""
    positions = {
        node: (data["x"], data["y"])
        for node, data in graph.nodes(data=True)
    }
    edges = list(graph.edges)
    edge_segments = [(positions[source], positions[target]) for source, target in edges]
    large_network = graph.number_of_nodes() > LARGE_NETWORK_NODE_LIMIT
    figure, axis = plt.subplots(figsize=(10, 6))
    axis.add_collection(
        LineCollection(
            edge_segments,
            colors="#D5DEE8",
            linewidths=0.35 if large_network else 0.8,
            alpha=0.7,
        )
    )
    if not large_network:
        nx.draw_networkx_nodes(
            graph,
            pos=positions,
            node_size=8,
            node_color="#52677D",
            linewidths=0,
            ax=axis,
        )

    if result is not None:
        active_edge_indexes = [
            index
            for index, edge in enumerate(edges)
            if result.edge_flows.get(edge, 0.0) > 0.0
        ]
        if active_edge_indexes:
            ratios = [
                result.edge_flows[edges[index]]
                / graph[edges[index][0]][edges[index][1]]["capacity_vph"]
                for index in active_edge_indexes
            ]
            collection = LineCollection(
                [edge_segments[index] for index in active_edge_indexes],
                cmap=plt.cm.YlOrRd,
                norm=Normalize(vmin=0, vmax=max(0.05, color_scale_max)),
                linewidths=0.9 if large_network else 1.7,
                alpha=0.95,
            )
            collection.set_array(ratios)
            axis.add_collection(collection)
            figure.colorbar(collection, ax=axis, label="Volume / capacity (v/c)")
    axis.set_axis_off()
    axis.autoscale_view()
    axis.set_aspect("equal", adjustable="datalim")
    figure.tight_layout()
    return figure