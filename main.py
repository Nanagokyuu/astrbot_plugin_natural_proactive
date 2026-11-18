"""AstrBot Star plugin for generic persona-grounded, randomized-jitter proactive messaging."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
import random
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools

from .api import NaturalProactiveAPI, SourcePlanContext, TargetSelectContext
from .scheduler import (
    ProactivePolicy,
    ProactiveStateStore,
    ProactiveTarget,
    ProactiveTurnPlan,
    build_proactive_prompts,
    clean_proactive_text,
    is_in_awake_window,
    local_day_start_ts,
    looks_like_truncated_sentence,
    next_jittered_timestamp,
)


class NaturalProactivePlugin(Star):
    """Generic AstrBot proactive messaging plugin (`natural-proactive.v1`)."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context, config)
        self.config: dict[str, Any] = dict(config or {})
        try:
            data_dir = Path(StarTools.get_data_dir())
        except Exception:
            data_dir = Path.cwd() / "data" / "plugin_data" / "astrbot_plugin_natural_proactive"
        self.store = ProactiveStateStore(data_dir / "proactive.sqlite3")
        self.extension_api = NaturalProactiveAPI(lambda: self.policy)
        self._enabled: bool = True
        self._lock = asyncio.Lock()
        self._loop_task: asyncio.Task[Any] | None = None
        self._last_user_msg_ts: dict[str, int] = {}
        self._recent_turns: dict[str, list[str]] = {}

    @property
    def policy(self) -> ProactivePolicy:
        return ProactivePolicy.from_mapping(self.config)

    async def initialize(self) -> None:
        self._ensure_loop()

    @filter.on_astrbot_loaded()
    async def on_loaded(self) -> None:
        self._ensure_loop()

    async def terminate(self) -> None:
        self._enabled = False
        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        self._loop_task = None
        self.extension_api.clear_registrations()

    def _ensure_loop(self) -> None:
        if not self._enabled or not self.policy.enabled:
            return
        if self._loop_task is not None and not self._loop_task.done():
            return

        async def _loop() -> None:
            try:
                await asyncio.sleep(30)
                while self._enabled:
                    try:
                        await self.run_tick()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning("[NaturalProactive] Tick error: %s: %s", type(exc).__name__, exc)
                    await asyncio.sleep(self.policy.tick_interval_seconds)
            except asyncio.CancelledError:
                return

        self._loop_task = asyncio.create_task(_loop())

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=-10)
    async def observe_inbound_activity(self, event: AstrMessageEvent) -> None:
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        text = str(getattr(event, "message_str", "") or "").strip()
        if not umo or not text or text.startswith("/"):
            return
        self._last_user_msg_ts[umo] = int(time.time())
        buf = self._recent_turns.setdefault(umo, [])
        buf.append(f"对方: {text[:120]}")
        if len(buf) > 12:
            del buf[:-12]

    @filter.on_decorating_result(priority=-50)
    async def observe_outbound_reply(self, event: AstrMessageEvent) -> None:
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not umo or not bool(getattr(event, "is_private_chat", lambda: False)()):
            return
        result = getattr(event, "get_result", lambda: None)()
        if result is None or not getattr(result, "chain", None):
            return
        plain = " ".join(
            str(getattr(c, "text", "") or "").strip()
            for c in result.chain
            if isinstance(c, Plain) and str(getattr(c, "text", "") or "").strip()
        ).strip()
        if plain:
            buf = self._recent_turns.setdefault(umo, [])
            buf.append(f"我: {plain[:120]}")
            if len(buf) > 12:
                del buf[:-12]

    def _is_umo_in_flight(self, umo: str) -> bool:
        get_stars = getattr(self.context, "get_all_stars", None)
        if not callable(get_stars):
            return False
        try:
            for star in get_stars() or ():
                if getattr(star, "name", "") in ("astrbot_plugin_bubble_flow", "bubble_flow"):
                    api = getattr(getattr(star, "star_cls", None), "extension_api", None)
                    if api is not None and callable(getattr(api, "get_session_state", None)):
                        return bool(api.get_session_state(umo).get("in_flight"))
        except Exception:
            pass
        return False

    async def _resolve_persona_and_turns(self, umo: str, policy: ProactivePolicy) -> tuple[str, tuple[str, ...]]:
        persona_prompt = ""
        turns = list(self._recent_turns.get(umo, ()))
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if conv_mgr is not None and callable(getattr(conv_mgr, "get_curr_conversation_id", None)):
            try:
                cid = await conv_mgr.get_curr_conversation_id(umo)
                if cid and callable(getattr(conv_mgr, "get_conversation", None)):
                    conv = await conv_mgr.get_conversation(umo, cid)
                    pid = getattr(conv, "persona_id", None) if conv else None
                    p_mgr = getattr(self.context, "persona_manager", None)
                    if pid and p_mgr is not None and callable(getattr(p_mgr, "get_persona", None)):
                        persona = await p_mgr.get_persona(pid)
                        if persona and getattr(persona, "system_prompt", None):
                            persona_prompt = str(persona.system_prompt)
                    if not turns and conv and getattr(conv, "history", None) and policy.history_turns > 0:
                        hist = conv.history
                        if isinstance(hist, list):
                            for msg in hist[-policy.history_turns :]:
                                if not isinstance(msg, dict):
                                    continue
                                role = "对方" if msg.get("role") == "user" else "我"
                                content = msg.get("content")
                                if isinstance(content, list):
                                    txt = " ".join(
                                        str(p.get("text") or "")
                                        for p in content
                                        if isinstance(p, dict) and p.get("type") == "text"
                                    ).strip()
                                else:
                                    txt = str(content or "").strip()
                                if txt:
                                    turns.append(f"{role}: {txt[:120]}")
            except Exception:
                pass
        return persona_prompt, tuple(turns[-policy.history_turns :] if policy.history_turns > 0 else ())

    async def _generate_with_provider(
        self,
        *,
        target: ProactiveTarget,
        policy: ProactivePolicy,
        now_ts: int,
    ) -> ProactiveTurnPlan | None:
        provider = None
        if policy.provider_id and callable(getattr(self.context, "get_provider_by_id", None)):
            provider = self.context.get_provider_by_id(policy.provider_id)
        if provider is None and callable(getattr(self.context, "get_using_provider", None)):
            provider = self.context.get_using_provider(umo=target.umo)
        if provider is None or not callable(getattr(provider, "text_chat", None)):
            return None

        recent_proactives = self.store.recent_texts_for_umo(target.umo, limit=4)
        sys_p, usr_p = build_proactive_prompts(
            persona_prompt=target.persona_prompt,
            recent_turns=target.recent_turns,
            recent_proactives=recent_proactives,
            now_ts=now_ts,
            tz_name=policy.timezone,
        )
        try:
            resp = await provider.text_chat(prompt=usr_p, system_prompt=sys_p)
        except Exception as exc:
            logger.warning("[NaturalProactive] Provider generation failed for %s: %s", target.umo, exc)
            return None

        raw_text = str(getattr(resp, "completion_text", "") or "").strip()
        cleaned = clean_proactive_text(
            raw_text,
            max_bubbles=policy.max_bubbles,
            strip_trailing_period=policy.strip_trailing_period,
        )
        if not cleaned or looks_like_truncated_sentence(cleaned):
            return None
        return ProactiveTurnPlan(umo=target.umo, kind="natural_spark", text=cleaned)

    async def _deliver_plan(self, plan: ProactiveTurnPlan, *, policy: ProactivePolicy, now_ts: int) -> bool:
        lines = [ln.strip() for ln in plan.text.splitlines() if ln.strip()][: policy.max_bubbles]
        if not lines and not plan.image_path:
            return False

        async def _try_send(chain: MessageChain) -> bool:
            try:
                return bool(await self.context.send_message(plan.umo, chain))
            except Exception as exc:
                logger.warning("[NaturalProactive] Send failed for %s: %s: %s", plan.umo, type(exc).__name__, exc)
                return False

        sent = False
        for idx, line in enumerate(lines):
            if idx > 0:
                gap = max(1.4, min(3.8, math.log(max(2, len(line) + 1), 2.1)))
                await asyncio.sleep(gap)
            chain = MessageChain([Plain(line)])
            if idx == len(lines) - 1 and plan.image_path:
                chain.file_image(str(plan.image_path))
            ok = await _try_send(chain)
            if not ok:
                if not sent:
                    return False
                break
            sent = True

        if not sent:
            return False

        next_allowed = next_jittered_timestamp(
            now_ts,
            min_gap_sec=policy.global_min_gap_seconds,
            max_gap_sec=policy.global_max_gap_seconds,
        )
        self.store.record_delivery(
            umo=plan.umo,
            kind=plan.kind,
            text=plan.text,
            item_key=plan.item_key,
            sent_at=now_ts,
            next_global_allowed_ts=next_allowed,
        )
        await self.extension_api.notify_delivered(plan)
        logger.info("[NaturalProactive] Sent (%s / %s): %s", plan.umo, plan.kind, " | ".join(lines))
        return True

    async def run_tick(self, *, now_ts: int | None = None) -> bool:
        policy = self.policy
        if not self._enabled or not policy.enabled:
            return False
        ts = int(now_ts or time.time())
        if not is_in_awake_window(
            ts,
            start_hour=policy.awake_start_hour,
            end_hour=policy.awake_end_hour,
            tz_name=policy.timezone,
        ):
            return False

        async with self._lock:
            next_allowed = self.store.next_global_allowed_ts()
            if next_allowed > 0 and ts < next_allowed:
                return False

            ext_targets = await self.extension_api.collect_targets(TargetSelectContext(now_ts=ts, policy=policy))
            targets: list[ProactiveTarget] = list(ext_targets)
            if not targets:
                active_cutoff = ts - policy.active_window_hours * 3600
                for umo, last_ts in self._last_user_msg_ts.items():
                    if last_ts < active_cutoff:
                        continue
                    if policy.allow_umo_prefixes and not any(umo.startswith(p) for p in policy.allow_umo_prefixes):
                        continue
                    persona_p, turns = await self._resolve_persona_and_turns(umo, policy)
                    targets.append(
                        ProactiveTarget(
                            umo=umo,
                            last_user_msg_ts=last_ts,
                            recent_turns=turns,
                            persona_prompt=persona_p,
                        )
                    )

            if not targets:
                return False

            random.shuffle(targets)
            day_start = local_day_start_ts(ts, tz_name=policy.timezone)
            recent_global_keys = frozenset(self.store.recent_global_item_keys(since_ts=ts - 24 * 3600))

            for tgt in targets:
                if self._is_umo_in_flight(tgt.umo):
                    continue
                if tgt.last_user_msg_ts > 0 and (ts - tgt.last_user_msg_ts) < policy.min_silence_after_chat_seconds:
                    continue
                if self.store.count_since(tgt.umo, since_ts=day_start) >= policy.daily_cap:
                    continue
                last_sent = self.store.latest_for_umo(tgt.umo)
                if last_sent > 0 and (ts - last_sent) < policy.per_target_cooldown_seconds:
                    continue

                plan = await self.extension_api.plan_from_sources(
                    SourcePlanContext(
                        target=tgt,
                        now_ts=ts,
                        policy=policy,
                        recent_global_keys=recent_global_keys,
                    )
                )
                if plan is None:
                    plan = await self._generate_with_provider(target=tgt, policy=policy, now_ts=ts)
                if plan is not None and await self._deliver_plan(plan, policy=policy, now_ts=ts):
                    return True
            return False
