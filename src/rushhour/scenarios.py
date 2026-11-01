"""Built-in infrastructure and operations interventions."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from itertools import pairwise

import networkx as nx

from rushhour.networks import Edge

SCENARIOS = ("baseline", "bridge_closure", "add_lane", "lower_speed_limit", "add_signal")


def apply_scenario(
    graph: nx.DiGraph,
    scenario: str,
    *,
    target_edges: Sequence[Edge] | None = None,
    closure_edges: Sequence[Edge] | None = None,
) -> nx.DiGraph:
    """Return an independent network with the selected scenario applied."""
    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown scenario {scenario!r}; choose from {', '.join(SCENARIOS)}.")
    changed = graph.copy()
    scenario_edges = [
        (source, target)
        for source, target, data in changed.edges(data=True)
        if data["bridge"]
    ]
    if not scenario_edges and scenario != "baseline":
        selected_edges = (
            closure_edges
            if scenario == "bridge_closure" and closure_edges is not None
            else target_edges
        )
        scenario_edges = (
            _normalize_target_edges(changed, selected_edges)
            if selected_edges is not None
            else _critical_link_pair(changed)
        )

    if scenario == "bridge_closure":
        changed.remove_edges_from(scenario_edges)
    elif scenario == "add_lane":
        for source, target in scenario_edges:
            data = changed[source][target]
            data["capacity_vph"] += data["capacity_vph"] / data["lanes"]
            data["lanes"] += 1
    elif scenario == "lower_speed_limit":
        for source, target in scenario_edges:
            data = changed[source][target]
            data.setdefault("reference_free_flow_min", data["free_flow_min"])
            data["speed_kph"] *= 0.7
            data["free_flow_min"] = data["length_km"] / data["speed_kph"] * 60.0
    elif scenario == "add_signal":
        center = scenario_edges[0][1] if target_edges is not None else scenario_edges[0][0]
        for _, _, data in changed.in_edges(center, data=True):
            data["signal_delay_min"] += 0.35
    return changed


def demand_critical_corridor(
    graph: nx.DiGraph,
    demand: Sequence[tuple[str, str, float]],
    edge_flows: dict[Edge, float],
    edge_travel_times: dict[Edge, float],
    risk_aversion: float = 0.0,
) -> tuple[Edge, ...]:
    """Choose a complete, heavily used OD route for corridor-wide interventions."""
    demand_by_origin: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for origin, destination, volume in demand:
        demand_by_origin[origin].append((destination, volume))

    best_path: tuple[str, ...] | None = None
    best_score = -1.0
    for origin, trips in demand_by_origin.items():
        try:
            _, paths = nx.single_source_dijkstra(
                graph,
                origin,
                weight=lambda source, target, data: edge_travel_times[(source, target)]
                * (
                    1.0
                    + risk_aversion
                    * edge_flows.get((source, target), 0.0)
                    / data["capacity_vph"]
                ),
            )
        except nx.NodeNotFound as error:
            raise ValueError(f"Demand references unknown node: {origin!r}.") from error
        for destination, volume in trips:
            if destination not in paths:
                raise ValueError(
                    f"No route connects demand pair {origin!r} to {destination!r}."
                )
            path = tuple(paths[destination])
            route_flow = sum(edge_flows.get(edge, 0.0) for edge in pairwise(path))
            score = volume * route_flow
            if score > best_score:
                best_path = path
                best_score = score

    if best_path is None:
        raise ValueError("Cannot choose an intervention corridor without non-empty demand.")
    corridor = []
    for source, target in pairwise(best_path):
        corridor.append((source, target))
        if graph.has_edge(target, source):
            corridor.append((target, source))
    return tuple(corridor)


def demand_critical_edges(
    graph: nx.DiGraph,
    demand: Sequence[tuple[str, str, float]],
    edge_flows: dict[Edge, float] | None = None,
    edge_travel_times: dict[Edge, float] | None = None,
    risk_aversion: float = 0.0,
) -> tuple[Edge, ...]:
    """Choose a busy demand route edge whose removal preserves all demand routes."""
    edge_volumes: dict[Edge, float] = defaultdict(float)
    demand_by_origin: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for origin, destination, volume in demand:
        demand_by_origin[origin].append((destination, volume))

    for origin, trips in demand_by_origin.items():
        try:
            if edge_travel_times is None:
                _, paths = nx.single_source_dijkstra(
                    graph, origin, weight="free_flow_min"
                )
            else:
                _, paths = nx.single_source_dijkstra(
                    graph,
                    origin,
                    weight=lambda source, target, data: edge_travel_times[
                        (source, target)
                    ]
                    * (
                        1.0
                        + risk_aversion
                        * (edge_flows or {}).get((source, target), 0.0)
                        / data["capacity_vph"]
                    ),
                )
        except nx.NodeNotFound as error:
            raise ValueError(f"Demand references unknown node: {origin!r}.") from error
        for destination, volume in trips:
            if destination not in paths:
                raise ValueError(
                    f"No route connects demand pair {origin!r} to {destination!r}."
                )
            for edge in pairwise(paths[destination]):
                edge_volumes[edge] += volume

    if not edge_volumes:
        raise ValueError("Cannot choose an intervention target without non-empty demand.")
    origin_destinations = tuple(
        (origin, destination)
        for origin, trips in demand_by_origin.items()
        for destination, _ in trips
    )
    edge_order = sorted(
        edge_volumes,
        key=lambda edge: (
            edge_flows.get(edge, 0.0) if edge_flows is not None else edge_volumes[edge],
            edge_volumes[edge],
        ),
        reverse=True,
    )
    for edge in edge_order:
        targets = {edge}
        if graph.has_edge(edge[1], edge[0]):
            targets.add((edge[1], edge[0]))
        filtered_graph = nx.subgraph_view(
            graph,
            filter_edge=lambda source, target, closed=targets: (source, target) not in closed,
        )
        if all(nx.has_path(filtered_graph, origin, destination) for origin, destination in origin_destinations):
            return tuple(sorted(targets))
    raise ValueError(
        "Could not choose a safe intervention corridor: closing any demand-used road "
        "would disconnect at least one origin–destination pair."
    )


def _normalize_target_edges(
    graph: nx.DiGraph,
    target_edges: Sequence[Edge],
) -> list[Edge]:
    edges = list(dict.fromkeys(target_edges))
    if not edges or any(not graph.has_edge(source, target) for source, target in edges):
        raise ValueError("Scenario target edges must exist in the selected network.")
    return edges


def _critical_link_pair(graph: nx.DiGraph) -> list[tuple[str, str]]:
    if graph.number_of_edges() == 0:
        raise ValueError("Cannot apply an infrastructure scenario to a network without roads.")
    sample_count = min(50, graph.number_of_nodes() - 1)
    centrality = nx.edge_betweenness_centrality(
        graph,
        k=sample_count if graph.number_of_nodes() > 50 else None,
        seed=42,
    )
    source, target = max(centrality, key=centrality.get)
    edges = [(source, target)]
    if graph.has_edge(target, source):
        edges.append((target, source))
    return edges
