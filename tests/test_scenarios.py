import networkx as nx
import pytest

from rushhour import DriverClass, build_demo_network, default_demand, simulate
from rushhour.networks import add_road_pair
from rushhour.scenarios import (
    apply_scenario,
    demand_critical_corridor,
    demand_critical_edges,
)


def test_lane_addition_increases_bridge_capacity_without_mutating_base():
    graph = build_demo_network()
    bridge_edge = next((u, v) for u, v, data in graph.edges(data=True) if data["bridge"])

    upgraded = apply_scenario(graph, "add_lane")

    assert upgraded[bridge_edge[0]][bridge_edge[1]]["capacity_vph"] == (
        graph[bridge_edge[0]][bridge_edge[1]]["capacity_vph"] + 900
    )
    assert graph[bridge_edge[0]][bridge_edge[1]]["lanes"] == 2


def test_all_scenarios_preserve_expected_graph_changes():
    base = build_demo_network()
    assert apply_scenario(base, "bridge_closure").number_of_edges() < base.number_of_edges()
    assert apply_scenario(base, "add_lane").number_of_edges() == base.number_of_edges()
    assert apply_scenario(base, "lower_speed_limit").number_of_edges() == base.number_of_edges()
    assert apply_scenario(base, "add_signal").number_of_edges() == base.number_of_edges()
    with pytest.raises(ValueError, match="Unknown scenario"):
        apply_scenario(base, "invented")


def test_closing_arterial_increases_demo_average_travel_time():
    base = build_demo_network()
    demand = default_demand(base)
    drivers = [DriverClass("all drivers", 1.0)]
    baseline = simulate(base, demand, drivers, max_iterations=80)
    closure = simulate(
        apply_scenario(base, "bridge_closure"),
        demand,
        drivers,
        max_iterations=80,
    )
    lane_addition = simulate(
        apply_scenario(base, "add_lane"),
        demand,
        drivers,
        max_iterations=80,
    )

    assert closure.metrics.average_travel_time_min > baseline.metrics.average_travel_time_min
    assert lane_addition.metrics.average_travel_time_min < baseline.metrics.average_travel_time_min


def test_scenarios_fall_back_to_a_critical_link_without_bridge_tags():
    graph = build_demo_network()
    for _, _, data in graph.edges(data=True):
        data["bridge"] = False

    closed = apply_scenario(graph, "bridge_closure")
    upgraded = apply_scenario(graph, "add_lane")
    speed_reduced = apply_scenario(graph, "lower_speed_limit")
    signalized = apply_scenario(graph, "add_signal")

    assert closed.number_of_edges() < graph.number_of_edges()
    assert upgraded.number_of_edges() == graph.number_of_edges()
    assert any(
        speed_reduced[u][v]["speed_kph"] < graph[u][v]["speed_kph"]
        for u, v, data in graph.edges(data=True)
        if (u, v) in speed_reduced.edges and data["speed_kph"] > 5
    )
    assert any(
        signalized[u][v]["signal_delay_min"] > graph[u][v]["signal_delay_min"]
        for u, v in graph.edges
    )


def test_demand_targeted_interventions_use_busy_connected_corridor():
    graph = nx.DiGraph()
    graph.add_nodes_from(
        (node, {"x": float(index), "y": float(index % 2)})
        for index, node in enumerate(("o", "a", "b", "d"))
    )
    add_road_pair(graph, "o", "a", length_km=1.0, speed_kph=40, lanes=1)
    add_road_pair(graph, "a", "d", length_km=1.0, speed_kph=40, lanes=1)
    add_road_pair(graph, "o", "b", length_km=1.2, speed_kph=40, lanes=2)
    add_road_pair(graph, "b", "d", length_km=1.2, speed_kph=40, lanes=2)
    demand = [("o", "d", 100.0)]
    edge_flows = {
        ("o", "a"): 0.0,
        ("a", "o"): 0.0,
        ("a", "d"): 0.0,
        ("d", "a"): 0.0,
        ("o", "b"): 100.0,
        ("b", "o"): 0.0,
        ("b", "d"): 100.0,
        ("d", "b"): 0.0,
    }

    edge_times = {
        edge: (1.0 if edge[0] in {"o", "b"} and edge[1] in {"b", "d"} else 3.0)
        for edge in graph.edges
    }
    closure_targets = demand_critical_edges(
        graph, demand, edge_flows, edge_times, risk_aversion=0.35
    )
    corridor_targets = demand_critical_corridor(
        graph, demand, edge_flows, edge_times, risk_aversion=0.35
    )
    closed = apply_scenario(
        graph,
        "bridge_closure",
        target_edges=corridor_targets,
        closure_edges=closure_targets,
    )
    lane_added = apply_scenario(graph, "add_lane", target_edges=corridor_targets)
    speed_reduced = apply_scenario(
        graph, "lower_speed_limit", target_edges=corridor_targets
    )

    assert set(closure_targets) == {("o", "b"), ("b", "o")}
    assert set(corridor_targets) == {
        ("o", "b"),
        ("b", "o"),
        ("b", "d"),
        ("d", "b"),
    }
    assert nx.has_path(closed, "o", "d")
    assert lane_added["o"]["b"]["capacity_vph"] == pytest.approx(2700)
    assert lane_added["o"]["b"]["lanes"] == 3
    assert lane_added["b"]["d"]["lanes"] == 3
    assert lane_added["d"]["b"]["lanes"] == 3
    assert speed_reduced["o"]["b"]["free_flow_min"] > graph["o"]["b"]["free_flow_min"]
    assert speed_reduced["b"]["d"]["free_flow_min"] > graph["b"]["d"]["free_flow_min"]
