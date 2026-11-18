"""Standalone AstrBot Natural Proactive plugin package (natural-proactive.v1)."""

from .api import (
    API_VERSION,
    PLUGIN_ID,
    NaturalProactiveAPI,
    discover_proactive_api,
)
from .scheduler import (
    PLUGIN_VERSION,
    ProactivePolicy,
    ProactiveTarget,
    ProactiveTurnPlan,
    build_proactive_prompts,
    clamp_bubble_text,
    clean_proactive_text,
    is_duplicate_text,
    is_in_awake_window,
    looks_like_truncated_sentence,
    next_jittered_timestamp,
    score_topic_relevance,
    text_similarity,
)
from .store import ProactiveStateStore

__all__ = [
    "API_VERSION",
    "PLUGIN_ID",
    "PLUGIN_VERSION",
    "NaturalProactiveAPI",
    "ProactivePolicy",
    "ProactiveStateStore",
    "ProactiveTarget",
    "ProactiveTurnPlan",
    "build_proactive_prompts",
    "clamp_bubble_text",
    "clean_proactive_text",
    "discover_proactive_api",
    "is_duplicate_text",
    "is_in_awake_window",
    "looks_like_truncated_sentence",
    "next_jittered_timestamp",
    "score_topic_relevance",
    "text_similarity",
]
