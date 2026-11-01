"""Profile the assignment kernel after imports and demo-data setup."""

from __future__ import annotations

import cProfile
import pstats

from rushhour import DriverClass, build_demo_network, default_demand, simulate

graph = build_demo_network()
demand = default_demand(graph)
drivers = [
    DriverClass("steady commuters", 0.65, risk_aversion=0.05),
    DriverClass(
        "flexible local drivers",
        0.35,
        risk_aversion=0.35,
        detour_tolerance=0.12,
        reroute_rate=0.65,
    ),
]

profile = cProfile.Profile()
profile.enable()
simulate(graph, demand, drivers, max_iterations=80)
profile.disable()
pstats.Stats(profile).strip_dirs().sort_stats("cumtime").print_stats(15)
