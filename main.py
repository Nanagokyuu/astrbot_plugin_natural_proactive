"""AstrBot Star plugin for generic persona-grounded, randomized-jitter proactive messaging."""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools

from .api import NaturalProactiveAPI, SourcePlanContext, TargetSelectContext
from .scheduler import (
    PLUGIN_VERSION,
    ProactivePolicy,
    ProactiveTarget,
    ProactiveTurnPlan,
    build_proactive_prompts,
    clean_proactive_text,
    format_local_ts,
    format_turn_clock,
    is_duplicate_text,
    is_group_umo,
    is_in_awake_window,
    local_day_start_ts,
    looks_like_truncated_sentence,
    next_jittered_timestamp,
)
from .store import ProactiveStateStore

MAX_ACTIVITY_TURNS = 12


class NaturalProactivePlugin(Star):
    """Generic AstrBot proactive messaging plugin (natural-proactive.v1)."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context, config)
        self.config: dict[str, Any] = dict(config or {})
        self._enabled: bool = True
        self._lock = asyncio.Lock()
        self._loop_task: asyncio.Task[Any] | None = None
        self._policy_cache: ProactivePolicy | None = None
        self._last_user_msg_ts: dict[str, int] = {}
        self._recent_turns: dict[str, list[str]] = {}
        self._persona_cache: dict[str, tuple[float, str]] = {}
        self._bubble_flow_api: Any = None
        self._bubble_flow_api_ts: float = 0.0
        self._bootstrapped_host: bool = False
        data_dir = self._resolve_data_dir()
        self.store = ProactiveStateStore(data_dir / "proactive.sqlite3")
        self.extension_api = NaturalProactiveAPI(lambda: self.policy)

    # -- lifecycle --------------------------------------------------------

    @staticmethod
    def _resolve_data_dir() -> Path:
        try:
            return Path(StarTools.get_data_dir())
        except Exception:
            return (
                Path.cwd()
                / "data"
                / "plugin_data"
                / "astrbot_plugin_natural_proactive"
            )

    @property
    def policy(self) -> ProactivePolicy:
        if self._policy_cache is None:
            self._policy_cache = ProactivePolicy.from_mapping(self.config)
        return self._policy_cache

    async def initialize(self) -> None:
        await self._restore_state()
        await self._prepare_runtime()
        self._ensure_loop()

    @filter.on_astrbot_loaded()
    async def on_loaded(self) -> None:
        self._ensure_loop()

    async def terminate(self) -> None:
        self._enabled = False
        task = self._loop_task
        self._loop_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._persona_cache.clear()
        # Deliberately keep extension registrations: AstrBot hot-reloads the
        # plugin on every config save, and downstream plugins are not reloaded.
        # The module-level registry is reused by the new instance.

    async def _restore_state(self, *, now_ts: int | None = None) -> None:
        """Rehydrate in-memory activity from SQLite after a restart/reload."""
        now = int(now_ts or time.time())
        cutoff = now - self.policy.active_window_hours * 3600
        rows = await self._to_thread(self.store.load_activity, since_ts=cutoff)
        if not rows:
            return
        for umo, (last_ts, turns) in rows.items():
            self._last_user_msg_ts[umo] = int(last_ts)
            self._recent_turns[umo] = list(turns)[-MAX_ACTIVITY_TURNS:]
        logger.info(
            "[NaturalProactive] Restored %d active target(s) from state.", len(rows)
        )

    async def _prepare_runtime(self) -> None:
        now = int(time.time())
        policy = self.policy
        existing = await self._to_thread(self.store.next_global_allowed_ts) or 0
        if existing <= 0:
            # A fresh install or a wiped database must not fire on the first
            # tick right after startup; seed a full random interval instead.
            seed = next_jittered_timestamp(
                now,
                min_gap_sec=policy.global_min_gap_seconds,
                max_gap_sec=policy.global_max_gap_seconds,
            )
            await self._to_thread(self.store.seed_global_gate, seed)
            logger.info(
                "[NaturalProactive] Seeded initial jitter gate at %s.",
                format_local_ts(seed, tz_name=policy.timezone),
            )
        await self._to_thread(
            self.store.prune,
            now_ts=now,
            delivery_retention_days=policy.retention_days,
            activity_retention_days=max(7, policy.active_window_hours // 24 * 2),
        )

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
                        logger.warning(
                            "[NaturalProactive] Tick error: %s: %s",
                            type(exc).__name__,
                            exc,
                        )
                    await asyncio.sleep(self.policy.tick_interval_seconds)
            except asyncio.CancelledError:
                return

        self._loop_task = asyncio.create_task(_loop())

    # -- helpers ----------------------------------------------------------

    async def _to_thread(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(func, *args, **kwargs)
        except Exception as exc:
            logger.warning(
                "[NaturalProactive] State store call %s failed: %s: %s",
                getattr(func, "__name__", func),
                type(exc).__name__,
                exc,
            )
            return None

    def _skip(self, umo: str, reason: str, **fields: Any) -> None:
        if self.policy.log_skip_reasons:
            logger.info("[NaturalProactive] skip %s: %s %s", umo or "*", reason, fields or "")
        else:
            logger.debug("[NaturalProactive] skip %s: %s %s", umo or "*", reason, fields or "")

    def _allowed_umo(self, umo: str, policy: ProactivePolicy) -> bool:
        if not policy.allow_umo_prefixes:
            return True
        return any(umo.startswith(prefix) for prefix in policy.allow_umo_prefixes)

    @staticmethod
    def _is_private(event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_private_chat", None)
        if not callable(checker):
            return False
        try:
            return bool(checker())
        except Exception:
            return False

    # -- observation ------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-10)
    async def observe_inbound_activity(self, event: AstrMessageEvent) -> None:
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        text = str(getattr(event, "message_str", "") or "").strip()
        if not umo or not text or text.startswith("/"):
            return
        is_private = self._is_private(event)
        if not is_private and not self.policy.enable_group_proactive:
            return
        sender_name = ""
        if not is_private:
            getter = getattr(event, "get_sender_name", None)
            if callable(getter):
                try:
                    sender_name = str(getter() or "").strip()
                except Exception:
                    sender_name = ""
        await self._remember_inbound(
            umo,
            text,
            int(time.time()),
            speaker_name=sender_name if not is_private else "",
        )

    @filter.on_decorating_result(priority=-50)
    async def observe_outbound_reply(self, event: AstrMessageEvent) -> None:
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not umo:
            return
        if not self._is_private(event) and not self.policy.enable_group_proactive:
            return
        getter = getattr(event, "get_result", None)
        result = getter() if callable(getter) else None
        if result is None or not getattr(result, "chain", None):
            return
        plain = " ".join(
            str(getattr(component, "text", "") or "").strip()
            for component in result.chain
            if isinstance(component, Plain)
            and str(getattr(component, "text", "") or "").strip()
        ).strip()
        if not plain:
            return
        now_ts = int(time.time())
        clock = format_turn_clock(now_ts, now_ts=now_ts, tz_name=self.policy.timezone)
        speaker = f"[{clock}] 我" if clock else "我"
        buf = self._recent_turns.setdefault(umo, [])
        buf.append(f"{speaker}: {plain[:120]}")
        del buf[:-MAX_ACTIVITY_TURNS]
        await self._to_thread(
            self.store.append_turn,
            umo,
            ts=now_ts,
            speaker=speaker,
            text=plain,
            max_items=MAX_ACTIVITY_TURNS,
        )

    async def _remember_inbound(
        self,
        umo: str,
        text: str,
        now_ts: int,
        *,
        speaker_name: str = "",
    ) -> None:
        ts = int(now_ts)
        self._last_user_msg_ts[umo] = ts
        clock = format_turn_clock(ts, now_ts=ts, tz_name=self.policy.timezone)
        base_speaker = speaker_name or "对方"
        speaker = f"[{clock}] {base_speaker}" if clock else base_speaker
        buf = self._recent_turns.setdefault(umo, [])
        buf.append(f"{speaker}: {text[:120]}")
        del buf[:-MAX_ACTIVITY_TURNS]
        await self._to_thread(
            self.store.record_inbound_activity,
            umo,
            ts=ts,
            speaker=speaker,
            text=text,
            max_items=MAX_ACTIVITY_TURNS,
        )
        window = self.policy.reply_window_hours * 3600
        await self._to_thread(
            self.store.mark_latest_answered,
            umo,
            ts=ts,
            window_seconds=window,
        )


    # -- host lookups -----------------------------------------------------

    def _find_bubble_flow_api(self) -> Any:
        now = time.monotonic()
        if self._bubble_flow_api is not None and now - self._bubble_flow_api_ts < 60:
            return self._bubble_flow_api
        self._bubble_flow_api = None
        self._bubble_flow_api_ts = now
        get_stars = getattr(self.context, "get_all_stars", None)
        if not callable(get_stars):
            return None
        try:
            for star in get_stars() or ():
                if getattr(star, "name", "") not in (
                    "astrbot_plugin_bubble_flow",
                    "bubble_flow",
                ):
                    continue
                api = getattr(getattr(star, "star_cls", None), "extension_api", None)
                if callable(getattr(api, "get_session_state", None)):
                    self._bubble_flow_api = api
                    break
        except Exception:
            return None
        return self._bubble_flow_api

    def _is_umo_in_flight(self, umo: str, api: Any = None) -> bool:
        api = api or self._find_bubble_flow_api()
        if api is None:
            return False
        try:
            return bool(api.get_session_state(umo).get("in_flight"))
        except Exception:
            return False

    # -- persona & history ------------------------------------------------

    @staticmethod
    def _persona_text(persona: Any) -> str:
        if persona is None:
            return ""
        text = getattr(persona, "system_prompt", None)
        if not text and isinstance(persona, dict):
            text = persona.get("system_prompt") or persona.get("prompt")
        return str(text or "").strip()

    async def _default_persona(self, umo: str) -> str:
        manager = getattr(self.context, "persona_manager", None)
        getter = getattr(manager, "get_default_persona_v3", None)
        if not callable(getter):
            return ""
        try:
            return self._persona_text(await getter(umo))
        except Exception:
            return ""

    async def _resolve_persona(self, umo: str, policy: ProactivePolicy) -> str:
        now = time.monotonic()
        cached = self._persona_cache.get(umo)
        if (
            cached is not None
            and policy.persona_cache_seconds > 0
            and now - cached[0] < policy.persona_cache_seconds
        ):
            return cached[1]

        persona = ""
        try:
            manager = getattr(self.context, "conversation_manager", None)
            get_cid = getattr(manager, "get_curr_conversation_id", None)
            get_conv = getattr(manager, "get_conversation", None)
            if callable(get_cid) and callable(get_conv):
                cid = await get_cid(umo)
                if cid:
                    conv = await get_conv(umo, cid)
                    persona_id = getattr(conv, "persona_id", None) if conv else None
                    persona_manager = getattr(self.context, "persona_manager", None)
                    get_persona = getattr(persona_manager, "get_persona", None)
                    if persona_id and callable(get_persona):
                        persona = self._persona_text(await get_persona(persona_id))
        except Exception as exc:
            logger.debug(
                "[NaturalProactive] persona lookup failed for %s: %s", umo, exc
            )

        if not persona:
            persona = await self._default_persona(umo)
        self._persona_cache[umo] = (now, persona)
        return persona

    @staticmethod
    def _parse_history(history: Any) -> list[dict[str, Any]]:
        # AstrBot exposes Conversation.history as a JSON string; older hosts may
        # hand back a list. Support both instead of silently dropping history.
        if isinstance(history, list):
            return [item for item in history if isinstance(item, dict)]
        if isinstance(history, str) and history.strip():
            try:
                data = json.loads(history)
            except (TypeError, ValueError):
                return []
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
        return []

    @staticmethod
    def _message_text(content: Any) -> str:
        if isinstance(content, list):
            return " ".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
        return str(content or "").strip()

    @staticmethod
    def _coerce_ts(value: Any) -> int:
        if isinstance(value, (int, float)):
            val = int(value)
            return val // 1000 if val > 10**12 else val
        if isinstance(value, datetime):
            return int(value.timestamp())
        if isinstance(value, str) and value.strip():
            raw = value.strip()
            if raw.isdigit():
                val = int(raw)
                return val // 1000 if val > 10**12 else val
            try:
                return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
            except ValueError:
                return 0
        return 0

    def _format_history_messages(
        self,
        messages: list[dict[str, Any]],
        policy: ProactivePolicy,
        *,
        now_ts: int | None = None,
    ) -> tuple[str, ...]:
        turns: list[str] = []
        ref_ts = int(now_ts or time.time())
        for message in messages[-policy.history_turns :]:
            role = "对方" if message.get("role") == "user" else "我"
            text = self._message_text(message.get("content"))
            if not text:
                continue
            msg_ts = self._coerce_ts(
                message.get("timestamp")
                or message.get("created_at")
                or message.get("ts")
            )
            clock = (
                format_turn_clock(msg_ts, now_ts=ref_ts, tz_name=policy.timezone)
                if msg_ts > 0
                else ""
            )
            prefix = f"[{clock}] {role}" if clock else role
            turns.append(f"{prefix}: {text[:120]}")
        return tuple(turns)

    @staticmethod
    def _extract_ltm_content(content: Any) -> str:
        if isinstance(content, dict):
            msg = content.get("message")
            if isinstance(msg, list):
                content = msg
            elif isinstance(content.get("text"), str):
                return str(content["text"]).strip()
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str) and part.strip():
                    parts.append(part.strip())
                    continue
                if not isinstance(part, dict):
                    continue
                ptype = str(part.get("type") or "").lower()
                if ptype in ("plain", "text"):
                    txt = part.get("text")
                    if txt is None and isinstance(part.get("data"), dict):
                        txt = part["data"].get("text")
                    if txt:
                        parts.append(str(txt).strip())
            return " ".join(p for p in parts if p).strip()
        return str(content or "").strip()

    async def _load_group_ltm_turns(
        self, umo: str, policy: ProactivePolicy
    ) -> tuple[str, ...]:
        if policy.history_turns <= 0 or not is_group_umo(umo):
            return ()
        mgr = getattr(self.context, "message_history_manager", None)
        getter = getattr(mgr, "get", None)
        if not callable(getter):
            return ()
        parts = umo.split(":", 2)
        if len(parts) != 3:
            return ()
        platform_id, _, group_id = parts
        try:
            records = await getter(
                platform_id=platform_id,
                user_id=group_id,
                page=1,
                page_size=policy.history_turns,
            )
        except Exception as exc:
            logger.debug(
                "[NaturalProactive] group LTM lookup failed for %s: %s", umo, exc
            )
            return ()
        now_ts = int(time.time())
        turns: list[str] = []
        for rec in list(records or ())[-policy.history_turns :]:
            content = getattr(rec, "content", None)
            if content is None and isinstance(rec, dict):
                content = rec.get("content")
            text = self._extract_ltm_content(content)
            if not text:
                continue
            sender = (
                getattr(rec, "sender_name", None)
                or (rec.get("sender_name") if isinstance(rec, dict) else None)
                or (
                    content.get("sender_name")
                    if isinstance(content, dict)
                    else None
                )
                or "群友"
            )
            rec_ts = self._coerce_ts(
                getattr(rec, "created_at", None)
                or (rec.get("created_at") if isinstance(rec, dict) else None)
            )
            clock = (
                format_turn_clock(rec_ts, now_ts=now_ts, tz_name=policy.timezone)
                if rec_ts > 0
                else ""
            )
            prefix = f"[{clock}] {sender}" if clock else str(sender)
            turns.append(f"{prefix}: {text[:120]}")
        return tuple(turns)

    async def _load_history_turns(
        self, umo: str, policy: ProactivePolicy
    ) -> tuple[str, ...]:
        if policy.history_turns <= 0:
            return ()
        manager = getattr(self.context, "conversation_manager", None)
        get_cid = getattr(manager, "get_curr_conversation_id", None)
        get_conv = getattr(manager, "get_conversation", None)
        messages: list[dict[str, Any]] = []
        if callable(get_cid) and callable(get_conv):
            try:
                cid = await get_cid(umo)
                if cid:
                    conv = await get_conv(umo, cid)
                    messages = self._parse_history(getattr(conv, "history", None))
            except Exception as exc:
                logger.debug(
                    "[NaturalProactive] history lookup failed for %s: %s", umo, exc
                )
        turns = self._format_history_messages(messages, policy)
        if not turns and is_group_umo(umo):
            turns = await self._load_group_ltm_turns(umo, policy)
        return turns

    async def _with_context(
        self, target: ProactiveTarget, policy: ProactivePolicy
    ) -> ProactiveTarget:
        turns = target.recent_turns or tuple(self._recent_turns.get(target.umo) or ())
        if not turns:
            turns = await self._load_history_turns(target.umo, policy)
        persona = target.persona_prompt
        if not persona:
            persona = await self._resolve_persona(target.umo, policy)
        return replace(target, recent_turns=tuple(turns), persona_prompt=persona)

    # -- generation -------------------------------------------------------

    def _select_provider(self, umo: str, policy: ProactivePolicy) -> Any:
        if policy.provider_id:
            getter = getattr(self.context, "get_provider_by_id", None)
            if callable(getter):
                try:
                    provider = getter(policy.provider_id)
                except Exception:
                    provider = None
                if provider is not None:
                    return provider
        getter = getattr(self.context, "get_using_provider", None)
        if not callable(getter):
            return None
        try:
            return getter(umo=umo)
        except TypeError:
            return getter()

    async def _generate_with_provider(
        self,
        *,
        target: ProactiveTarget,
        policy: ProactivePolicy,
        now_ts: int,
    ) -> ProactiveTurnPlan | None:
        provider = self._select_provider(target.umo, policy)
        if provider is None or not callable(getattr(provider, "text_chat", None)):
            self._skip(target.umo, "no_provider")
            return None

        recent_proactives = (
            await self._to_thread(self.store.recent_texts_for_umo, target.umo, limit=4)
        ) or []
        streak = (await self._to_thread(self.store.unanswered_streak, target.umo)) or 0
        start_user_msg_ts = int(self._last_user_msg_ts.get(target.umo, 0) or 0)

        system_prompt, user_prompt = build_proactive_prompts(
            persona_prompt=target.persona_prompt,
            recent_turns=target.recent_turns,
            recent_proactives=recent_proactives,
            max_bubbles=policy.max_bubbles,
            unanswered_streak=int(streak),
            now_ts=now_ts,
            tz_name=policy.timezone,
            channel_type=target.channel_type,
            last_user_msg_ts=target.last_user_msg_ts,
        )
        try:
            response = await provider.text_chat(
                prompt=user_prompt, system_prompt=system_prompt
            )
        except Exception as exc:
            logger.warning(
                "[NaturalProactive] Provider generation failed for %s: %s",
                target.umo,
                exc,
            )
            return None

        raw_text = str(getattr(response, "completion_text", "") or "").strip()
        cleaned = clean_proactive_text(
            raw_text,
            max_bubbles=policy.max_bubbles,
            strip_trailing_period=policy.strip_trailing_period,
        )
        if not cleaned:
            self._skip(target.umo, "empty_generation")
            return None
        if looks_like_truncated_sentence(cleaned):
            self._skip(target.umo, "truncated_generation", text=cleaned[:40])
            return None
        if is_duplicate_text(
            cleaned, recent_proactives, threshold=policy.dedupe_similarity
        ):
            self._skip(target.umo, "duplicate_generation", text=cleaned[:40])
            return None
        return ProactiveTurnPlan(
            umo=target.umo,
            kind="natural_spark",
            text=cleaned,
            created_user_msg_ts=start_user_msg_ts,
        )


    # -- delivery ---------------------------------------------------------

    async def _persist_to_host_conversation(
        self, umo: str, assistant_text: str
    ) -> None:
        buf = self._recent_turns.get(umo)
        if buf is not None:
            now_ts = int(time.time())
            clock = format_turn_clock(now_ts, now_ts=now_ts, tz_name=self.policy.timezone)
            speaker = f"[{clock}] 我" if clock else "我"
            buf.append(f"{speaker}: {assistant_text[:120]}")
            del buf[:-MAX_ACTIVITY_TURNS]

        manager = getattr(self.context, "conversation_manager", None)
        get_cid = getattr(manager, "get_curr_conversation_id", None)
        add_pair = getattr(manager, "add_message_pair", None)
        if not callable(get_cid) or not callable(add_pair):
            return
        try:
            from astrbot.core.agent.message import (
                AssistantMessageSegment,
                TextPart,
                UserMessageSegment,
            )
        except ImportError:
            return
        try:
            cid = await get_cid(umo)
            if not cid:
                return
            await add_pair(
                cid=cid,
                user_message=UserMessageSegment(
                    content=[TextPart(text="[主动发起对话]")]
                ),
                assistant_message=AssistantMessageSegment(
                    content=[TextPart(text=assistant_text)]
                ),
            )
        except Exception as exc:
            logger.debug(
                "[NaturalProactive] Persist to host conversation skipped for %s: %s",
                umo,
                exc,
            )

    async def _deliver_plan(
        self,
        plan: ProactiveTurnPlan,
        *,
        policy: ProactivePolicy,
        now_ts: int,
        record: bool = True,
    ) -> bool:
        lines = [
            line.strip() for line in plan.text.splitlines() if line.strip()
        ][: policy.max_bubbles]
        if not lines and not plan.image_path:
            return False

        bubble_flow = self._find_bubble_flow_api()
        sent_lines: list[str] = []
        sent_image = False

        def _pre_send_abort_reason() -> str:
            if not self._enabled:
                return "terminating_before_send"
            if (
                plan.created_user_msg_ts > 0
                and int(self._last_user_msg_ts.get(plan.umo, 0) or 0)
                > plan.created_user_msg_ts
            ):
                return "new_message_during_generation"
            if self._is_umo_in_flight(plan.umo, bubble_flow):
                return "in_flight_before_send"
            return ""

        async def _try_send(chain: MessageChain) -> bool:
            try:
                return bool(await self.context.send_message(plan.umo, chain))
            except Exception as exc:
                logger.warning(
                    "[NaturalProactive] Send failed for %s: %s: %s",
                    plan.umo,
                    type(exc).__name__,
                    exc,
                )
                return False

        if plan.image_path and (plan.image_first or not lines):
            abort_reason = _pre_send_abort_reason()
            if abort_reason:
                self._skip(plan.umo, abort_reason)
                return False
            img_chain = MessageChain([])
            img_chain.file_image(str(plan.image_path))
            if not await _try_send(img_chain):
                return False
            sent_image = True

        for index, line in enumerate(lines):
            abort_reason = _pre_send_abort_reason()
            if abort_reason:
                self._skip(plan.umo, abort_reason)
                break
            if sent_image or index > 0:
                gap = max(1.2, min(3.8, math.log(max(2, len(line) + 1), 2.1)))
                await asyncio.sleep(gap)
                abort_reason = _pre_send_abort_reason()
                if abort_reason:
                    self._skip(plan.umo, abort_reason)
                    break
            chain = MessageChain([Plain(line)])
            if index == len(lines) - 1 and plan.image_path and not sent_image:
                chain.file_image(str(plan.image_path))
            if not await _try_send(chain):
                if not sent_lines and not sent_image:
                    return False
                break
            sent_lines.append(line)
            if index == len(lines) - 1 and plan.image_path and not sent_image:
                sent_image = True

        if not sent_lines and not sent_image:
            return False

        if record:
            text = "\n".join(sent_lines)
            next_allowed = next_jittered_timestamp(
                now_ts,
                min_gap_sec=policy.global_min_gap_seconds,
                max_gap_sec=policy.global_max_gap_seconds,
            )
            await self._to_thread(
                self.store.record_delivery,
                umo=plan.umo,
                kind=plan.kind,
                text=text or "[图片]",
                item_key=plan.item_key,
                sent_at=now_ts,
                next_global_allowed_ts=next_allowed,
            )
            if policy.persist_to_conversation_history and text:
                await self._persist_to_host_conversation(plan.umo, text)
            await self.extension_api.notify_delivered(replace(plan, text=text))
            logger.info(
                "[NaturalProactive] Sent (%s / %s): %s",
                plan.umo,
                plan.kind,
                " | ".join(sent_lines),
            )
        return True

    # -- tick -------------------------------------------------------------

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
            logger.debug("[NaturalProactive] skip *: outside_awake_window")
            return False

        async with self._lock:
            next_allowed = (
                await self._to_thread(self.store.next_global_allowed_ts)
            ) or 0
            if next_allowed > 0 and ts < next_allowed:
                logger.debug(
                    "[NaturalProactive] skip *: global_gate until %s",
                    format_local_ts(next_allowed, tz_name=policy.timezone),
                )
                return False

            targets = await self._collect_targets(policy, ts)
            if not targets:
                logger.debug("[NaturalProactive] skip *: no_targets")
                return False

            random.shuffle(targets)
            day_start = local_day_start_ts(ts, tz_name=policy.timezone)
            recent_global_keys = frozenset(
                (
                    await self._to_thread(
                        self.store.recent_global_item_keys, since_ts=ts - 86400
                    )
                )
                or ()
            )

            for target in targets:
                reason = await self._eligibility_reason(
                    target, policy, ts, day_start
                )
                if reason:
                    self._skip(target.umo, reason)
                    continue

                target = await self._with_context(target, policy)
                plan = await self.extension_api.plan_from_sources(
                    SourcePlanContext(
                        target=target,
                        now_ts=ts,
                        policy=policy,
                        recent_global_keys=recent_global_keys,
                    )
                )
                if plan is None:
                    plan = await self._generate_with_provider(
                        target=target, policy=policy, now_ts=ts
                    )
                if plan is not None and await self._deliver_plan(
                    plan, policy=policy, now_ts=ts
                ):
                    return True
            return False

    async def _bootstrap_host_targets(
        self, policy: ProactivePolicy, now_ts: int
    ) -> None:
        if self._bootstrapped_host or not policy.bootstrap_from_host:
            return
        self._bootstrapped_host = True
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return
        convs: list[Any] = []
        get_filtered = getattr(manager, "get_filtered_conversations", None)
        get_all = getattr(manager, "get_conversations", None)
        try:
            if callable(get_filtered):
                res = await get_filtered(page=1, page_size=30, include_history=True)
                if isinstance(res, tuple) and res:
                    convs = list(res[0] or ())
                elif isinstance(res, list):
                    convs = res
            elif callable(get_all):
                convs = list(await get_all() or ())
        except Exception as exc:
            logger.debug("[NaturalProactive] host bootstrap failed: %s", exc)
            return

        cutoff = now_ts - policy.active_window_hours * 3600
        discovered = 0
        for conv in convs:
            umo = str(
                getattr(conv, "user_id", None)
                or getattr(conv, "unified_msg_origin", None)
                or ""
            ).strip()
            if not umo:
                continue
            if is_group_umo(umo) and not policy.enable_group_proactive:
                continue
            if not self._allowed_umo(umo, policy):
                continue
            updated_ts = self._coerce_ts(
                getattr(conv, "updated_at", None) or getattr(conv, "created_at", None)
            )
            if updated_ts <= 0 or updated_ts < cutoff:
                continue
            if self._last_user_msg_ts.get(umo, 0) >= updated_ts:
                continue
            self._last_user_msg_ts[umo] = updated_ts
            if umo not in self._recent_turns:
                messages = self._parse_history(getattr(conv, "history", None))
                turns = self._format_history_messages(messages, policy, now_ts=now_ts)
                if turns:
                    self._recent_turns[umo] = list(turns)[-MAX_ACTIVITY_TURNS:]
            discovered += 1
        if discovered:
            logger.info(
                "[NaturalProactive] Bootstrapped %d active target(s) from host history.",
                discovered,
            )

    async def _collect_targets(
        self, policy: ProactivePolicy, now_ts: int
    ) -> list[ProactiveTarget]:
        await self._bootstrap_host_targets(policy, now_ts)
        collected: dict[str, ProactiveTarget] = {}
        extension_targets = await self.extension_api.collect_targets(
            TargetSelectContext(now_ts=now_ts, policy=policy)
        )
        for target in extension_targets:
            if not target.umo:
                continue
            if not self._allowed_umo(target.umo, policy):
                self._skip(target.umo, "umo_not_allowed")
                continue
            ch_type = (
                "group"
                if (target.channel_type == "group" or is_group_umo(target.umo))
                else target.channel_type
            )
            collected[target.umo] = replace(target, channel_type=ch_type)

        if not collected:
            cutoff = now_ts - policy.active_window_hours * 3600
            for umo, last_ts in list(self._last_user_msg_ts.items()):
                if last_ts < cutoff:
                    continue
                if not self._allowed_umo(umo, policy):
                    continue
                ch_type = "group" if is_group_umo(umo) else "private"
                collected[umo] = ProactiveTarget(
                    umo=umo,
                    channel_type=ch_type,
                    last_user_msg_ts=last_ts,
                    recent_turns=tuple(self._recent_turns.get(umo) or ()),
                )
        return list(collected.values())

    async def _eligibility_reason(
        self,
        target: ProactiveTarget,
        policy: ProactivePolicy,
        now_ts: int,
        day_start: int,
    ) -> str:
        umo = target.umo
        is_group = target.channel_type == "group" or is_group_umo(umo)
        if is_group and not policy.enable_group_proactive:
            return "group_disabled"
        if not self._allowed_umo(umo, policy):
            return "umo_not_allowed"
        if await self._to_thread(self.store.is_muted, umo):
            return "muted"
        if self._is_umo_in_flight(umo):
            return "in_flight"
        silence_req = (
            policy.group_min_silence_seconds
            if is_group
            else policy.min_silence_after_chat_seconds
        )
        if (
            target.last_user_msg_ts > 0
            and (now_ts - target.last_user_msg_ts) < silence_req
        ):
            return "post_chat_silence"

        sent_today = (await self._to_thread(
            self.store.count_since, umo, since_ts=day_start
        )) or 0
        if sent_today >= policy.daily_cap:
            return "daily_cap"

        streak = (await self._to_thread(self.store.unanswered_streak, umo)) or 0
        if streak >= policy.max_unanswered:
            return "unanswered_limit"

        last_sent = (await self._to_thread(self.store.latest_for_umo, umo)) or 0
        if last_sent > 0:
            if is_group:
                cooldown = policy.group_cooldown_seconds
            else:
                cooldown = policy.effective_cooldown_seconds(int(streak))
            if now_ts - last_sent < cooldown:
                return "cooldown"
        return ""

    # -- commands ---------------------------------------------------------

    @filter.command("冒泡状态", alias={"np-status"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_status(self, event: AstrMessageEvent):
        """查看主动冒泡的调度状态。"""
        policy = self.policy
        now = int(time.time())
        next_allowed = (await self._to_thread(self.store.next_global_allowed_ts)) or 0
        stats = (await self._to_thread(self.store.stats)) or {}
        extensions = self.extension_api.describe()
        muted = len(await self._to_thread(self.store.muted_umos) or [])

        if next_allowed > now:
            gate = (
                format_local_ts(next_allowed, tz_name=policy.timezone)
                + f"（还有 {max(1, (next_allowed - now) // 60)} 分钟）"
            )
        else:
            gate = "已放行"

        lines = [
            f"AstrBot 自然主动冒泡 v{PLUGIN_VERSION}",
            f"开关：{'开启' if policy.enabled else '关闭'} / 清醒时段："
            f"{policy.awake_start_hour:02d}:00-{policy.awake_end_hour:02d}:00 "
            f"{policy.timezone}",
            f"下次全局允许：{gate}",
            f"累计发送：{stats.get('total_deliveries', 0)} 条；"
            f"上次发送：{format_local_ts(int(stats.get('last_sent_at', 0)), tz_name=policy.timezone)}",
            f"跟踪对象：{stats.get('tracked_targets', 0)} 个；静音：{muted} 个",
            f"扩展回调：{extensions['target_selectors']} 个 selector / "
            f"{extensions['source_planners']} 个 planner / "
            f"{extensions['delivery_listeners']} 个 listener",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("冒泡测试", alias={"np-test"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_test(self, event: AstrMessageEvent, umo: str = ""):
        """立即生成并发送一条主动消息（不计入配额）。"""
        policy = self.policy
        target_umo = (umo or str(getattr(event, "unified_msg_origin", ""))).strip()
        if not target_umo:
            yield event.plain_result("找不到当前会话的 UMO，无法测试。")
            return
        now = int(time.time())
        target = await self._with_context(
            ProactiveTarget(umo=target_umo, last_user_msg_ts=now), policy
        )
        plan = await self.extension_api.plan_from_sources(
            SourcePlanContext(
                target=target,
                now_ts=now,
                policy=policy,
                recent_global_keys=frozenset(),
            )
        )
        if plan is None:
            plan = await self._generate_with_provider(
                target=target, policy=policy, now_ts=now
            )
        if plan is None:
            yield event.plain_result(
                "这次没有生成可用内容（空输出/截断/与最近消息过于相似），"
                "具体原因见插件日志。"
            )
            return
        ok = await self._deliver_plan(plan, policy=policy, now_ts=now, record=False)
        yield event.plain_result(
            ("已发送（未计入配额）：\n" if ok else "发送失败：\n") + plan.text
        )

    @filter.command("别主动找我", alias={"np-mute"})
    async def cmd_mute(self, event: AstrMessageEvent):
        """让 bot 不要再主动给当前会话发消息。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not umo:
            return
        await self._to_thread(
            self.store.mute, umo, ts=int(time.time()), reason="user_command"
        )
        yield event.plain_result(
            "好，之后我不会再主动找你。想恢复的话发「/恢复冒泡」就行。"
        )

    @filter.command("恢复冒泡", alias={"np-unmute"})
    async def cmd_unmute(self, event: AstrMessageEvent):
        """恢复当前会话的主动消息。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not umo:
            return
        removed = await self._to_thread(self.store.unmute, umo)
        if removed:
            yield event.plain_result("好，我回来了。以后还会偶尔来打扰你。")
        else:
            yield event.plain_result("本来就没有静音你，放心。")
