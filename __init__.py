"""Standalone AstrBot Natural Proactive plugin package (natural-proactive.v1)."""

from .api import API_VERSION, PLUGIN_ID, NaturalProactiveAPI, discover_proactive_api
from .scheduler import (
    ProactivePolicy,
    ProactiveTarget,
    ProactiveTurnPlan,
    clean_proactive_text,
    is_in_awake_window,
)

__all__ = [
    "API_VERSION",
    "PLUGIN_ID",
    "NaturalProactiveAPI",
    "ProactivePolicy",
    "ProactiveTarget",
    "ProactiveTurnPlan",
    "clean_proactive_text",
    "discover_proactive_api",
    "is_in_awake_window",
]
