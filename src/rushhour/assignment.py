"""BPR-based, behavior-aware stochastic user-equilibrium assignment."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise

import networkx as nx
import numpy as np

from rushhour.metrics import SimulationMetrics, calculate_metrics
from rushhour.networks import Edge, normalize_graph


@dataclass(frozen=True)
class DriverClass:
    """A driver population with bounded detours and its own route perception."""

    name: str
    share: float
    risk_aversion: float = 0.0
    detour_tolerance: float = 0.0
    reroute_rate: float = 1.0

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Driver class name cannot be empty.")
        if not 0.0 <= self.share <= 1.0:
            raise ValueError("Driver class share must be between 0 and 1.")
        if not 0.0 <= self.risk_aversion <= 2.0:
            raise ValueError("Risk aversion must be between 0 and 2.")
        if not 0.0 <= self.detour_tolerance <= 1.0:
            raise ValueError("Detour tolerance must be between 0 and 1.")
        if not 0.0 < self.reroute_rate <= 1.0:
            raise ValueError("Reroute rate must be greater than 0 and at most 1.")


@dataclass(frozen=True)
class SimulationResult:
    """Serializable simulation outputs and assignment convergence diagnostics."""

    metrics: SimulationMetrics
    edge_flows: dict[Edge, float]
    edge_travel_times: dict[Edge, float]
    iterations: int
    relative_gap: float
    total_demand: float


def bpr_travel_times(
    free_flow_min: np.ndarray,
    flow_vph: np.ndarray,
    capacity_vph: np.ndarray,
    alpha: float = 0.15,
    beta: float = 4.0,
) -> np.ndarray:
    """Calculate vectorized Bureau of Public Roads link travel times."""
    if alpha < 0 or beta <= 0:
        raise ValueError("BPR alpha must be non-negative and beta must be positive.")
    if np.any(capacity_vph <= 0) or np.any(flow_vph < 0):
        raise ValueError("Capacities must be positive and flows must be non-negative.")
    return free_flow_min * (1.0 + alpha * np.power(flow_vph / capacity_vph, beta))


def simulate(
    graph: nx.Graph,
    demand: Sequence[tuple[str, str, float]],
    driver_classes: Sequence[DriverClass] | None = None,
    *,
    max_iterations: int = 100,
    convergence_threshold: float = 0.005,
    bpr_alpha: float = 0.15,
    bpr_beta: float = 4.0,
    max_paths: int = 3,
    progress_callback: Callable[[int, int, float], None] | None = None,
) -> SimulationResult:
    """Assign OD demand using iterative bounded-rational route choice and BPR costs.

    Each class distributes demand across paths within its detour tolerance using a
    logit choice model. Successive averages model the rate at which drivers revise
    routes; the reported relative gap measures the remaining perceived-cost
    improvement available to the current population.
    """
    if max_iterations < 1:
        raise ValueError("max_iterations must be at least 1.")
    if convergence_threshold < 0:
        raise ValueError("convergence_threshold cannot be negative.")
    if max_paths < 1:
        raise ValueError("max_paths must be at least 1.")
    road_graph = normalize_graph(graph)
    normalized_demand = _validate_demand(road_graph, demand)
    classes = tuple(
        (DriverClass("all drivers", 1.0),)
        if driver_classes is None
        else driver_classes
    )
    if not classes or not np.isclose(sum(driver.share for driver in classes), 1.0):
        raise ValueError("Driver class shares must sum to 1.")

    edges = list(road_graph.edges)
    edge_indexes = {edge: index for index, edge in enumerate(edges)}
    free_flow = np.fromiter(
        (road_graph[source][target]["free_flow_min"] for source, target in edges), dtype=float
    )
    capacity = np.fromiter(
        (road_graph[source][target]["capacity_vph"] for source, target in edges), dtype=float
    )
    signal_delay = np.fromiter(
        (road_graph[source][target]["signal_delay_min"] for source, target in edges), dtype=float
    )
    class_flows = np.zeros((len(classes), len(edges)), dtype=float)
    relative_gap = float("inf")
    used_iterations = max_iterations
    demand_by_origin: dict[str, list[tuple[str, str, float]]] = {}
    for origin, destination, volume in normalized_demand:
        demand_by_origin.setdefault(origin, []).append((origin, destination, volume))

    def load_network() -> tuple[np.ndarray, float]:
        total_flow = class_flows.sum(axis=0)
        travel_time = bpr_travel_times(free_flow, total_flow, capacity, bpr_alpha, bpr_beta)
        travel_time += signal_delay
        auxiliary = np.zeros_like(class_flows)
        perceived_costs: dict[int, np.ndarray] = {}
        shortest_path_cost = 0.0
        for class_index, driver in enumerate(classes):
            if driver.share == 0:
                continue
            congestion_ratio = total_flow / capacity
            perceived = travel_time * (1.0 + driver.risk_aversion * congestion_ratio)
            perceived_costs[class_index] = perceived
            for edge, edge_index in edge_indexes.items():
                road_graph[edge[0]][edge[1]]["perceived_time"] = float(perceived[edge_index])
            candidate_limit = max_paths if driver.detour_tolerance > 0 else 1
            for origin, origin_demand in demand_by_origin.items():
                paths = _paths_from_origin(
                    road_graph,
                    origin,
                    {destination for _, destination, _ in origin_demand},
                    max_paths=candidate_limit,
                )
                for _, destination, volume in origin_demand:
                    try:
                        candidate_paths = _bounded_paths(
                            paths,
                            destination,
                            perceived,
                            edge_indexes,
                            driver.detour_tolerance,
                            max_paths if driver.detour_tolerance > 0 else 1,
                        )
                    except KeyError as error:
                        raise ValueError(
                            f"No route connects demand pair {origin!r} to {destination!r}."
                        ) from error
                    _load_paths(
                        auxiliary[class_index],
                        candidate_paths,
                        volume * driver.share,
                        edge_indexes,
                    )
                    shortest_path_cost += candidate_paths[0][1] * volume * driver.share

        denominator = 0.0
        for class_index, perceived in perceived_costs.items():
            denominator += float(np.dot(class_flows[class_index], perceived))
        gap = (
            max(0.0, (denominator - shortest_path_cost) / denominator)
            if denominator > 0
            else 0.0
        )
        return auxiliary, gap

    for iteration in range(max_iterations):
        auxiliary, relative_gap = load_network()
        if iteration > 0 and relative_gap <= convergence_threshold:
            used_iterations = iteration + 1
            if progress_callback is not None:
                progress_callback(used_iterations, max_iterations, relative_gap)
            break

        msa_step = 2.0 / (iteration + 2.0)
        for class_index, driver in enumerate(classes):
            response_step = 1.0 - (1.0 - msa_step) ** driver.reroute_rate
            class_flows[class_index] += response_step * (
                auxiliary[class_index] - class_flows[class_index]
            )
        if progress_callback is not None:
            progress_callback(iteration + 1, max_iterations, relative_gap)
    else:
        _, relative_gap = load_network()

    total_flow = class_flows.sum(axis=0)
    final_travel_time = bpr_travel_times(free_flow, total_flow, capacity, bpr_alpha, bpr_beta)
    final_travel_time += signal_delay
    flow_map = {edge: float(total_flow[index]) for index, edge in enumerate(edges)}
    time_map = {edge: float(final_travel_time[index]) for index, edge in enumerate(edges)}
    metrics = calculate_metrics(
        road_graph,
        normalized_demand,
        classes,
        flow_map,
        time_map,
        edges,
    )
    return SimulationResult(
        metrics=metrics,
        edge_flows=flow_map,
        edge_travel_times=time_map,
        iterations=used_iterations,
        relative_gap=relative_gap,
        total_demand=sum(volume for _, _, volume in normalized_demand),
    )


def _validate_demand(
    graph: nx.DiGraph, demand: Sequence[tuple[str, str, float]]
) -> list[tuple[str, str, float]]:
    normalized = []
    for origin, destination, volume in demand:
        origin, destination = str(origin), str(destination)
        if origin not in graph or destination not in graph:
            raise ValueError(f"Demand references unknown node: {origin!r} -> {destination!r}.")
        if origin == destination or not np.isfinite(volume) or volume <= 0:
            raise ValueError(f"Demand volume must be positive for distinct nodes: {origin} -> {destination}.")
        normalized.append((origin, destination, float(volume)))
    if not normalized:
        raise ValueError("At least one origin-destination demand is required.")
    return normalized


def _paths_from_origin(
    graph: nx.DiGraph,
    origin: str,
    destinations: set[str],
    *,
    max_paths: int,
) -> dict[str, list[list[str]]]:
    distances, shortest_paths = nx.single_source_dijkstra(
        graph, origin, weight="perceived_time"
    )
    return {
        destination: (
            [shortest_paths[destination]]
            if max_paths == 1
            else [
                path
                for _, path in zip(
                    range(max_paths),
                    nx.shortest_simple_paths(graph, origin, destination, weight="perceived_time"),
                )
            ]
        )
        for destination in distances
        if destination != origin and destination in destinations
    }


def _bounded_paths(
    paths_by_destination: dict[str, list[list[str]]],
    destination: str,
    costs: np.ndarray,
    edge_indexes: dict[Edge, int],
    detour_tolerance: float,
    max_paths: int,
) -> list[tuple[list[str], float]]:
    paths = paths_by_destination[destination][:max_paths]
    scored = [
        (path, sum(costs[edge_indexes[(source, target)]] for source, target in pairwise(path)))
        for path in paths
    ]
    shortest = min(cost for _, cost in scored)
    limit = shortest * (1.0 + detour_tolerance) + 1e-9
    return [(path, cost) for path, cost in scored if cost <= limit]


def _load_paths(
    edge_flows: np.ndarray,
    paths: list[tuple[list[str], float]],
    volume: float,
    edge_indexes: dict[Edge, int],
) -> None:
    minimum = min(cost for _, cost in paths)
    spread = max(max(cost for _, cost in paths) - minimum, 0.1)
    weights = np.exp(-np.asarray([cost - minimum for _, cost in paths]) / spread)
    weights /= weights.sum()
    for (path, _), share in zip(paths, weights):
        for source, target in pairwise(path):
            edge_flows[edge_indexes[(source, target)]] += volume * float(share)
