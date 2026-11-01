import networkx as nx
import numpy as np
import pytest

from rushhour import DriverClass, build_demo_network, default_demand, simulate
from rushhour.assignment import bpr_travel_times
from rushhour.networks import add_road_pair


def test_bpr_curve_matches_reference_and_increases_with_flow():
    times = bpr_travel_times(
        np.array([10.0, 10.0, 10.0]),
        np.array([0.0, 1000.0, 2000.0]),
        np.array([1000.0, 1000.0, 1000.0]),
    )
    assert times.tolist() == pytest.approx([10.0, 11.5, 34.0])


def test_assignment_diverts_flow_when_shortest_route_congests():
    graph = nx.DiGraph()
    for node, point in {"o": (0, 0), "a": (1, 1), "b": (1, -1), "d": (2, 0)}.items():
        graph.add_node(node, x=point[0], y=point[1])
    add_road_pair(graph, "o", "a", length_km=1.0, speed_kph=30, lanes=1)
    add_road_pair(graph, "a", "d", length_km=1.0, speed_kph=30, lanes=1)
    add_road_pair(graph, "o", "b", length_km=1.2, speed_kph=30, lanes=2)
    add_road_pair(graph, "b", "d", length_km=1.2, speed_kph=30, lanes=2)

    result = simulate(graph, [("o", "d", 1400.0)], max_iterations=100)

    assert result.edge_flows[("o", "a")] > 0
    assert result.edge_flows[("o", "b")] > 0
    assert result.relative_gap < 0.02
    assert result.metrics.average_travel_time_min > 4.0


def test_driver_heterogeneity_changes_network_assignment():
    graph = build_demo_network()
    demand = default_demand(graph)
    homogeneous = simulate(
        graph,
        demand,
        [DriverClass("commuters", 1.0)],
        max_iterations=40,
    )
    heterogeneous = simulate(
        graph,
        demand,
        [
            DriverClass("steady", 0.5),
            DriverClass("flexible", 0.5, risk_aversion=0.5, detour_tolerance=0.2),
        ],
        max_iterations=40,
    )

    assert heterogeneous.edge_flows != homogeneous.edge_flows
    assert heterogeneous.metrics.average_travel_time_min > 0
    assert heterogeneous.metrics.p95_travel_time_min >= heterogeneous.metrics.average_travel_time_min / 10


def test_invalid_class_shares_and_demand_are_rejected():
    graph = build_demo_network()
    with pytest.raises(ValueError, match="shares must sum"):
        simulate(graph, default_demand(graph), [DriverClass("one", 0.4)])
    with pytest.raises(ValueError, match="positive"):
        simulate(graph, [("r0c0", "r0c1", 0)])


def test_assignment_reports_iteration_progress():
    progress = []
    graph = build_demo_network(rows=3, columns=3)
    result = simulate(
        graph,
        [("r0c0", "r2c2", 100.0)],
        max_iterations=3,
        max_paths=1,
        convergence_threshold=0.0,
        progress_callback=lambda completed, total, gap: progress.append(
            (completed, total, gap)
        ),
    )

    assert result.iterations == 3
    assert [completed for completed, _, _ in progress] == [1, 2, 3]
    assert all(total == 3 for _, total, _ in progress)
