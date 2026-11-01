"""Command-line entry points for simulation, comparison, and OSM import."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from rushhour import DriverClass, build_demo_network, default_demand, simulate
from rushhour.networks import download_osm_network, load_graphml
from rushhour.scenarios import (
    SCENARIOS,
    apply_scenario,
    demand_critical_corridor,
    demand_critical_edges,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rushhour",
        description="Run congestion-aware, BPR-based urban traffic assignment.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="simulate one scenario")
    run.add_argument("--scenario", choices=SCENARIOS, default="baseline")
    run.add_argument("--network", type=Path, help="input road network in GraphML format")
    run.add_argument("--demand-scale", type=float, default=1.0)
    run.add_argument("--iterations", type=int, default=80)
    run.add_argument("--output", type=Path, help="write results as JSON")

    compare = commands.add_parser("compare", help="compare every intervention with baseline")
    compare.add_argument("--network", type=Path, help="input road network in GraphML format")
    compare.add_argument("--demand-scale", type=float, default=1.0)
    compare.add_argument("--iterations", type=int, default=80)
    compare.add_argument("--output", type=Path, help="write results as JSON")
    compare.add_argument("--plot", type=Path, help="write a scenario-comparison chart as PNG")

    download = commands.add_parser("download-osm", help="download a drivable OpenStreetMap area")
    download.add_argument("--place", required=True, help='place name, e.g. "Cambridge, Massachusetts, USA"')
    download.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "download-osm":
        output = download_osm_network(arguments.place, arguments.output)
        print(f"Saved OpenStreetMap road network to {output}")
        return 0

    graph = load_graphml(arguments.network) if arguments.network else build_demo_network()
    demand = default_demand(graph, scale=arguments.demand_scale)
    drivers = (
        DriverClass("steady commuters", 0.65, risk_aversion=0.05, detour_tolerance=0.0),
        DriverClass(
            "flexible local drivers",
            0.35,
            risk_aversion=0.35,
            detour_tolerance=0.12,
            reroute_rate=0.65,
        ),
    )
    scenarios = SCENARIOS if arguments.command == "compare" else (arguments.scenario,)
    has_tagged_bridges = any(data["bridge"] for _, _, data in graph.edges(data=True))
    if arguments.command == "compare":
        baseline = _run_one(graph, demand, drivers, "baseline", arguments.iterations)
        closure_targets = (
            demand_critical_edges(
                graph,
                demand,
                baseline.edge_flows,
                baseline.edge_travel_times,
                sum(driver.share * driver.risk_aversion for driver in drivers),
            )
            if not has_tagged_bridges
            else None
        )
        targets = (
            demand_critical_corridor(
                graph,
                demand,
                baseline.edge_flows,
                baseline.edge_travel_times,
                sum(driver.share * driver.risk_aversion for driver in drivers),
            )
            if not has_tagged_bridges
            else None
        )
        scenario_results = {"baseline": baseline}
        scenario_results.update(
            {
                scenario: _run_one(
                    graph,
                    demand,
                    drivers,
                    scenario,
                    arguments.iterations,
                    target_edges=targets,
                    closure_edges=closure_targets,
                )
                for scenario in scenarios[1:]
            }
        )
    elif arguments.scenario != "baseline" and not has_tagged_bridges:
        baseline = _run_one(graph, demand, drivers, "baseline", arguments.iterations)
        closure_targets = demand_critical_edges(
            graph,
            demand,
            baseline.edge_flows,
            baseline.edge_travel_times,
            sum(driver.share * driver.risk_aversion for driver in drivers),
        )
        targets = demand_critical_corridor(
            graph,
            demand,
            baseline.edge_flows,
            baseline.edge_travel_times,
            sum(driver.share * driver.risk_aversion for driver in drivers),
        )
        scenario_results = {
            arguments.scenario: _run_one(
                graph,
                demand,
                drivers,
                arguments.scenario,
                arguments.iterations,
                target_edges=targets,
                closure_edges=closure_targets,
            )
        }
    else:
        scenario_results = {
            arguments.scenario: _run_one(
                graph, demand, drivers, arguments.scenario, arguments.iterations
            )
        }
    results = {
        scenario: _serialize_result(scenario, result)
        for scenario, result in scenario_results.items()
    }
    if arguments.command == "compare":
        baseline = results["baseline"]["metrics"]
        for value in results.values():
            value["average_travel_time_change_pct"] = _percentage_change(
                value["metrics"]["average_travel_time_min"],
                baseline["average_travel_time_min"],
            )
            value["delay_change_pct"] = _percentage_change(
                value["metrics"]["total_delay_vehicle_min"],
                baseline["total_delay_vehicle_min"],
            )

    serialized = json.dumps(results, indent=2)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(serialized + "\n", encoding="utf-8")
        print(f"Wrote results to {arguments.output}")
    else:
        print(serialized)
    if arguments.command == "compare" and arguments.plot:
        _plot_comparison(results, arguments.plot)
        print(f"Wrote scenario chart to {arguments.plot}")
    return 0


def _run_one(
    graph,
    demand,
    drivers,
    scenario: str,
    iterations: int,
    *,
    target_edges=None,
    closure_edges=None,
):
    return simulate(
        apply_scenario(
            graph,
            scenario,
            target_edges=target_edges,
            closure_edges=closure_edges,
        ),
        demand,
        drivers,
        max_iterations=iterations,
    )


def _serialize_result(scenario: str, result) -> dict:
    return {
        "scenario": scenario,
        "iterations": result.iterations,
        "relative_gap": result.relative_gap,
        "total_demand_vehicles": result.total_demand,
        "metrics": result.metrics.to_dict(),
    }


def _percentage_change(value: float, baseline: float) -> float:
    return 0.0 if baseline == 0 else (value / baseline - 1.0) * 100.0


def _plot_comparison(results: dict, destination: Path) -> None:
    labels = [scenario.replace("_", "\n") for scenario in results]
    positions = np.arange(len(labels))
    average_times = [value["metrics"]["average_travel_time_min"] for value in results.values()]
    p95_times = [value["metrics"]["p95_travel_time_min"] for value in results.values()]
    delays = [value["metrics"]["total_delay_vehicle_min"] for value in results.values()]
    figure, axes = plt.subplots(1, 2, figsize=(11, 4.5), constrained_layout=True)
    width = 0.36
    axes[0].bar(positions - width / 2, average_times, width, label="Mean", color="#2878B5")
    axes[0].bar(positions + width / 2, p95_times, width, label="P95 OD", color="#F28E2B")
    axes[0].set_ylabel("Minutes")
    axes[0].set_title("Modeled trip travel time")
    axes[0].legend(frameon=False)
    axes[1].bar(positions, delays, color="#59A14F")
    axes[1].set_ylabel("Vehicle-minutes")
    axes[1].set_title("Total delay above free flow")
    for axis in axes:
        axis.set_xticks(positions, labels)
        axis.tick_params(axis="x", labelsize=8)
        axis.grid(axis="y", alpha=0.2)
        axis.set_axisbelow(True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(destination, dpi=180)
    plt.close(figure)


if __name__ == "__main__":
    raise SystemExit(main())
