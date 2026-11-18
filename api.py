"""Public extension API (`natural-proactive.v1`) for astrbot_plugin_natural_proactive."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable
import uuid

from astrbot.api import logger

from .scheduler import ProactivePolicy, ProactiveTarget, ProactiveTurnPlan

PLUGIN_ID = "astrbot_plugin_natural_proactive"
API_VERSION = "natural-proactive.v1"


@dataclass(frozen=True)
class TargetSelectContext:
    now_ts: int
    policy: ProactivePolicy


@dataclass(frozen=True)
class SourcePlanContext:
    target: ProactiveTarget
    now_ts: int
    policy: ProactivePolicy
    recent_global_keys: frozenset[str]


@dataclass
class _Reg:
    token: str
    kind: str
    name: str
    priority: int
    fn: Callable[..., Any]


def discover_proactive_api(context: Any) -> "NaturalProactiveAPI | None":
    """Locate the activated NaturalProactiveAPI instance from an AstrBot Context."""
    get_stars = getattr(context, "get_all_stars", None)
    if not callable(get_stars):
        return None
    try:
        stars = get_stars() or []
    except Exception as exc:
        logger.warning("[NaturalProactive] discover_proactive_api failed: %s", exc)
        return None
    for star in stars:
        name = getattr(star, "name", "") or ""
        if name not in (PLUGIN_ID, "natural_proactive"):
            continue
        if not getattr(star, "activated", False):
            continue
        api = getattr(getattr(star, "star_cls", None), "extension_api", None)
        if getattr(api, "api_version", None) == API_VERSION:
            return api
    return None


class NaturalProactiveAPI:
    """Versioned extension API allowing downstream plugins to plug in custom targets/sources."""

    api_version = API_VERSION
    plugin_id = PLUGIN_ID

    def __init__(self, policy_getter: Callable[[], ProactivePolicy]) -> None:
        self._policy_getter = policy_getter
        self._target_selectors: list[_Reg] = []
        self._source_planners: list[_Reg] = []
        self._delivery_listeners: list[_Reg] = []

    def _add(self, bucket: list[_Reg], kind: str, name: str, priority: int, fn: Callable[..., Any]) -> str:
        if not callable(fn):
            raise TypeError(f"{kind} callback must be callable")
        token = f"{kind}:{name}:{uuid.uuid4().hex[:10]}"
        bucket.append(_Reg(token=token, kind=kind, name=name, priority=int(priority), fn=fn))
        bucket.sort(key=lambda item: item.priority)
        return token

    def register_target_selector(
        self,
        selector_id: str,
        fn: Callable[[TargetSelectContext], list[ProactiveTarget] | None],
        *,
        priority: int = 100,
    ) -> str:
        return self._add(self._target_selectors, "target_selector", selector_id, priority, fn)

    def register_source_planner(
        self,
        planner_id: str,
        fn: Callable[[SourcePlanContext], ProactiveTurnPlan | None],
        *,
        priority: int = 100,
    ) -> str:
        return self._add(self._source_planners, "source_planner", planner_id, priority, fn)

    def register_delivery_listener(
        self,
        listener_id: str,
        fn: Callable[[ProactiveTurnPlan], Any],
        *,
        priority: int = 100,
    ) -> str:
        return self._add(self._delivery_listeners, "delivery_listener", listener_id, priority, fn)

    def unregister(self, token: str) -> bool:
        for bucket in (self._target_selectors, self._source_planners, self._delivery_listeners):
            for idx, item in enumerate(bucket):
                if item.token == token:
                    bucket.pop(idx)
                    return True
        return False

    def clear_registrations(self) -> None:
        self._target_selectors.clear()
        self._source_planners.clear()
        self._delivery_listeners.clear()

    async def collect_targets(self, ctx: TargetSelectContext) -> list[ProactiveTarget]:
        out: list[ProactiveTarget] = []
        for reg in list(self._target_selectors):
            try:
                res = reg.fn(ctx)
                if inspect.isawaitable(res):
                    res = await res
                if isinstance(res, list):
                    out.extend(item for item in res if isinstance(item, ProactiveTarget))
            except Exception as exc:
                logger.warning("[NaturalProactive] target_selector '%s' failed: %s", reg.name, exc)
        return out

    async def plan_from_sources(self, ctx: SourcePlanContext) -> ProactiveTurnPlan | None:
        for reg in list(self._source_planners):
            try:
                res = reg.fn(ctx)
                if inspect.isawaitable(res):
                    res = await res
                if isinstance(res, ProactiveTurnPlan) and res.text.strip():
                    return res
            except Exception as exc:
                logger.warning("[NaturalProactive] source_planner '%s' failed: %s", reg.name, exc)
        return None

    async def notify_delivered(self, plan: ProactiveTurnPlan) -> None:
        for reg in list(self._delivery_listeners):
            try:
                res = reg.fn(plan)
                if inspect.isawaitable(res):
                    await res
            except Exception as exc:
                logger.warning("[NaturalProactive] delivery_listener '%s' failed: %s", reg.name, exc)
