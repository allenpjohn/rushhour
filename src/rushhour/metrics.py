"""Network and trip-level outcome metrics."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import pairwise
from typing import Protocol

import networkx as nx
import numpy as np

from rushhour.networks import Edge


class DriverBehavior(Protocol):
    share: float
    risk_aversion: float


@dataclass(frozen=True)
class CongestedEdge:
    origin: str
    destination: str
    flow_vph: float
    capacity_vph: float
    volume_capacity_ratio: float
    travel_time_min: float


@dataclass(frozen=True)
class SimulationMetrics:
    average_travel_time_min: float
    p95_travel_time_min: float
    total_delay_vehicle_min: float
    total_demand_vehicles: float
    congested_edges: tuple[CongestedEdge, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def calculate_metrics(
    graph: nx.DiGraph,
    demand: list[tuple[str, str, float]],
    driver_classes: tuple[DriverBehavior, ...],
    edge_flows: dict[Edge, float],
    edge_times: dict[Edge, float],
    edges: list[Edge],
) -> SimulationMetrics:
    """Aggregate assigned network delay and weighted shortest-route travel times."""
    total_demand = sum(volume for _, _, volume in demand)
    vehicle_minutes = sum(edge_flows[edge] * edge_times[edge] for edge in edges)
    delay = sum(
        edge_flows[edge]
        * max(
            edge_times[edge]
            - graph[edge[0]][edge[1]].get(
                "reference_free_flow_min", graph[edge[0]][edge[1]]["free_flow_min"]
            ),
            0.0,
        )
        for edge in edges
    )

    observed_trip_times: list[float] = []
    observed_weights: list[float] = []
    for driver in driver_classes:
        if driver.share == 0:
            continue
        for origin in {origin for origin, _, _ in demand}:
            try:
                _, paths = nx.single_source_dijkstra(
                    graph,
                    origin,
                    weight=lambda source, target, data, risk=driver.risk_aversion: edge_times[
                        (source, target)
                    ]
                    * (1.0 + risk * edge_flows[(source, target)] / data["capacity_vph"]),
                )
            except nx.NodeNotFound as error:
                raise ValueError(f"Demand references unknown node: {origin!r}.") from error
            for trip_origin, destination, volume in demand:
                if trip_origin != origin:
                    continue
                if destination not in paths:
                    raise ValueError(
                        f"No route connects demand pair {origin!r} to {destination!r}."
                    )
                path = paths[destination]
                trip_time = sum(edge_times[source, target] for source, target in pairwise(path))
                observed_trip_times.append(float(trip_time))
                observed_weights.append(volume * driver.share)

    order = np.argsort(observed_trip_times)
    trip_times = np.asarray(observed_trip_times, dtype=float)[order]
    weights = np.asarray(observed_weights, dtype=float)[order]
    cumulative = np.cumsum(weights)
    p95_index = min(int(np.searchsorted(cumulative, 0.95 * cumulative[-1], side="left")), len(trip_times) - 1)
    congested = []
    for edge in edges:
        source, target = edge
        capacity = graph[source][target]["capacity_vph"]
        congested.append(
            CongestedEdge(
                origin=str(source),
                destination=str(target),
                flow_vph=float(edge_flows[edge]),
                capacity_vph=float(capacity),
                volume_capacity_ratio=float(edge_flows[edge] / capacity),
                travel_time_min=float(edge_times[edge]),
            )
        )
    congested.sort(key=lambda item: item.volume_capacity_ratio, reverse=True)
    return SimulationMetrics(
        average_travel_time_min=float(vehicle_minutes / total_demand),
        p95_travel_time_min=float(trip_times[p95_index]),
        total_delay_vehicle_min=float(delay),
        total_demand_vehicles=float(total_demand),
        congested_edges=tuple(congested[:10]),
    )
