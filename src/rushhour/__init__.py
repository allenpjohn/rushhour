"""Congestion-aware traffic assignment for urban road networks."""

from rushhour.assignment import DriverClass, SimulationResult, simulate
from rushhour.networks import build_demo_network, default_demand

__all__ = [
    "DriverClass",
    "SimulationResult",
    "build_demo_network",
    "default_demand",
    "simulate",
]

__version__ = "1.0.0"
