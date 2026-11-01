"""Print concise examples for the regression fixes in fixes.md."""

from __future__ import annotations

import networkx as nx

from rushhour import DriverClass, build_demo_network, default_demand, simulate
from rushhour.networks import add_road_pair
from rushhour.scenarios import apply_scenario
from rushhour.tracing import extract_canvas_lines, lines_to_graph


def main() -> None:
    crossing = lines_to_graph(
        [(10, 50, 90, 50), (50, 10, 50, 90)],
        canvas_width=100,
        scale_km_per_pixel=0.01,
        snap_tolerance_px=0,
    )
    t_junction = lines_to_graph(
        [(10, 50, 90, 50), (50, 53, 50, 80)],
        canvas_width=100,
        scale_km_per_pixel=0.01,
        snap_tolerance_px=4,
    )
    opposite_one_way = lines_to_graph(
        [(0, 0, 10, 0), (10, 0, 0, 0)],
        canvas_width=100,
        scale_km_per_pixel=0.01,
        bidirectional=False,
        snap_tolerance_px=0,
    )
    fabric_line = extract_canvas_lines(
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
                }
            ]
        }
    )

    graph = nx.DiGraph()
    graph.add_nodes_from(("o", "a", "b", "d"))
    add_road_pair(graph, "o", "a", length_km=0.5, speed_kph=60, lanes=1)
    add_road_pair(graph, "a", "d", length_km=0.5, speed_kph=60, lanes=1)
    add_road_pair(graph, "o", "b", length_km=0.7, speed_kph=60, lanes=20)
    add_road_pair(graph, "b", "d", length_km=0.7, speed_kph=60, lanes=20)
    one_iteration = simulate(graph, [("o", "d", 5000.0)], max_iterations=1)

    demo = build_demo_network()
    demand = default_demand(demo)
    drivers = [DriverClass("all drivers", 1.0)]
    baseline = simulate(demo, demand, drivers, max_iterations=20)
    slower = simulate(
        apply_scenario(demo, "lower_speed_limit"),
        demand,
        drivers,
        max_iterations=20,
    )

    print(f"Exact crossing at zero tolerance: {crossing.number_of_nodes()} nodes")
    print(f"Endpoint near segment interior: {t_junction.number_of_nodes()} nodes")
    print(f"Opposite one-way roads: {opposite_one_way.number_of_edges()} directed edges")
    print(f"Lower-speed delay: {baseline.metrics.total_delay_vehicle_min:.0f} -> "
          f"{slower.metrics.total_delay_vehicle_min:.0f} vehicle-min")
    print(f"Final-flow gap after one iteration: {one_iteration.relative_gap:.3f}")
    print(f"Fabric left-origin line endpoints: {fabric_line[0]}")


if __name__ == "__main__":
    main()
