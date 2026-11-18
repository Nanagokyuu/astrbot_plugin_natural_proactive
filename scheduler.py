"""Pure policy, time, text and prompt helpers for natural proactive messaging.

This module intentionally has **no AstrBot imports** so the scheduling policy,
text pipeline and prompt builder stay unit-testable without a host runtime.
"""

from __future__ import annotations

import difflib
import json
import random
import re
import string
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Shanghai"
PLUGIN_VERSION = "0.3.2"

_FENCE_CHAR = chr(96)
_RE_URL = re.compile(r"https?://[^\s\n。，！？；：、）】》」』\"'<>]+")
_RE_FENCE = re.compile(r"^" + _FENCE_CHAR + r"{3,}.*$", re.MULTILINE)
_RE_ROLE_PREFIX = re.compile(r"^(?:我|bot|Bot|BOT|AI|ai|助手|机器人|回复)\s*[:：]\s*")
_RE_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_RE_TOOL_CALL_BLOCK = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL | re.IGNORECASE)
_RE_COMPARE_STRIP = re.compile(
    r"[\s" + re.escape(string.punctuation + "。，！？；：、（）【】「」『』“”‘’…—～·") + r"]+"
)

# Signals that genuinely mean "this sentence was cut off". We deliberately do
# NOT treat a trailing 了/的/着/在... as truncation: in casual Chinese those
# characters end most natural sentences ("我到家了", "挺好看的").
_INCOMPLETE_PUNCT = (
    "，",
    ",",
    "、",
    "：",
    ":",
    "；",
    ";",
    "（",
    "(",
    "「",
    "“",
    "《",
)
_DANGLING_WORDS = (
    "然后",
    "但是",
    "因为",
    "所以",
    "而且",
    "不过",
    "如果",
    "虽然",
    "就是",
    "可是",
    "并且",
    "以及",
    "或者",
)
_QUOTE_PAIRS = (
    ("「", "」"),
    ("“", "”"),
    ("（", "）"),
    ("(", ")"),
    ("《", "》"),
    ("【", "】"),
    ("[", "]"),
)
_WRAPPING_PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’", "「": "」", "『": "』"}


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    return value if isinstance(value, dict) else {}


def _pick(section: dict[str, Any], raw: dict[str, Any], key: str, default: Any) -> Any:
    if key in section:
        return section[key]
    return raw.get(key, default)


def _to_int(
    value: Any, default: int, *, lo: int | None = None, hi: int | None = None
) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if lo is not None:
        number = max(lo, number)
    if hi is not None:
        number = min(hi, number)
    return number


def _to_float(
    value: Any, default: float, *, lo: float | None = None, hi: float | None = None
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if lo is not None:
        number = max(lo, number)
    if hi is not None:
        number = min(hi, number)
    return number


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on", "是"):
            return True
        if lowered in ("false", "0", "no", "off", "否"):
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _to_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


@dataclass(frozen=True)
class ProactivePolicy:
    """Configuration policy for generic natural proactive messaging."""

    enabled: bool = True
    tick_interval_seconds: int = 300
    awake_start_hour: int = 8
    awake_end_hour: int = 23
    timezone: str = DEFAULT_TZ
    global_min_gap_seconds: int = 1500
    global_max_gap_seconds: int = 3300

    daily_cap: int = 3
    global_daily_cap: int = 12
    per_target_cooldown_seconds: int = 5400
    min_silence_after_chat_seconds: int = 600
    active_window_hours: int = 48
    allow_umo_prefixes: tuple[str, ...] = ()
    max_unanswered: int = 3
    enable_group_proactive: bool = False
    group_min_silence_seconds: int = 3600
    group_cooldown_seconds: int = 14400
    bootstrap_from_host: bool = True

    reply_window_hours: int = 24
    unanswered_backoff: float = 2.0
    unanswered_backoff_cap: float = 8.0
    persist_to_conversation_history: bool = True

    provider_id: str = ""
    history_turns: int = 6
    max_bubbles: int = 2
    max_chars_per_bubble: int = 80
    generation_timeout_seconds: int = 60
    strip_trailing_period: bool = True
    dedupe_similarity: float = 0.85
    persona_cache_seconds: int = 600

    log_skip_reasons: bool = True
    retention_days: int = 30

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> ProactivePolicy:
        if not isinstance(raw, dict):
            return cls()
        sched = _section(raw, "schedule")
        targets = _section(raw, "targets")
        engagement = _section(raw, "engagement")
        generation = _section(raw, "generation")
        diagnostics = _section(raw, "diagnostics")

        prefixes_raw = str(_pick(targets, raw, "allow_umo_prefixes", "") or "")
        prefixes = tuple(p.strip() for p in prefixes_raw.split(",") if p.strip())

        g_min = _to_int(_pick(sched, raw, "global_min_gap_seconds", 1500), 1500, lo=60)
        g_max = _to_int(
            _pick(sched, raw, "global_max_gap_seconds", 3300),
            3300,
            lo=g_min,
            hi=24 * 3600,
        )
        awake_end = _to_int(_pick(sched, raw, "awake_end_hour", 23), 23, lo=1, hi=24)

        return cls(
            enabled=_to_bool(raw.get("enabled", True), True),
            tick_interval_seconds=_to_int(
                _pick(sched, raw, "tick_interval_seconds", 300), 300, lo=30, hi=3600
            ),
            awake_start_hour=_to_int(
                _pick(sched, raw, "awake_start_hour", 8), 8, lo=0, hi=23
            ),
            awake_end_hour=awake_end,
            timezone=_to_str(_pick(sched, raw, "timezone", DEFAULT_TZ), DEFAULT_TZ),
            global_min_gap_seconds=g_min,
            global_max_gap_seconds=g_max,
            daily_cap=_to_int(_pick(targets, raw, "daily_cap", 3), 3, lo=1, hi=20),
            global_daily_cap=_to_int(
                _pick(targets, raw, "global_daily_cap", 12), 12, lo=0, hi=100
            ),
            per_target_cooldown_seconds=_to_int(
                _pick(targets, raw, "per_target_cooldown_seconds", 5400),
                5400,
                lo=60,
                hi=7 * 24 * 3600,
            ),
            min_silence_after_chat_seconds=_to_int(
                _pick(targets, raw, "min_silence_after_chat_seconds", 600),
                600,
                lo=0,
                hi=24 * 3600,
            ),
            active_window_hours=_to_int(
                _pick(targets, raw, "active_window_hours", 48), 48, lo=1, hi=30 * 24
            ),
            allow_umo_prefixes=prefixes,
            max_unanswered=_to_int(
                _pick(targets, raw, "max_unanswered", 3), 3, lo=1, hi=50
            ),
            enable_group_proactive=_to_bool(
                _pick(targets, raw, "enable_group_proactive", False), False
            ),
            group_min_silence_seconds=_to_int(
                _pick(targets, raw, "group_min_silence_seconds", 3600),
                3600,
                lo=60,
                hi=7 * 24 * 3600,
            ),
            group_cooldown_seconds=_to_int(
                _pick(targets, raw, "group_cooldown_seconds", 14400),
                14400,
                lo=300,
                hi=14 * 24 * 3600,
            ),
            bootstrap_from_host=_to_bool(
                _pick(targets, raw, "bootstrap_from_host", True), True
            ),
            reply_window_hours=_to_int(
                _pick(engagement, raw, "reply_window_hours", 24),
                24,
                lo=1,
                hi=30 * 24,
            ),
            unanswered_backoff=_to_float(
                _pick(engagement, raw, "unanswered_backoff", 2.0), 2.0, lo=1.0, hi=5.0
            ),
            unanswered_backoff_cap=_to_float(
                _pick(engagement, raw, "unanswered_backoff_cap", 8.0),
                8.0,
                lo=1.0,
                hi=32.0,
            ),
            persist_to_conversation_history=_to_bool(
                _pick(engagement, raw, "persist_to_conversation_history", True), True
            ),
            provider_id=_to_str(_pick(generation, raw, "provider_id", "")),
            history_turns=_to_int(
                _pick(generation, raw, "history_turns", 6), 6, lo=0, hi=30
            ),
            max_bubbles=_to_int(
                _pick(generation, raw, "max_bubbles", 2), 2, lo=1, hi=6
            ),
            max_chars_per_bubble=_to_int(
                _pick(generation, raw, "max_chars_per_bubble", 80),
                80,
                lo=0,
                hi=500,
            ),
            generation_timeout_seconds=_to_int(
                _pick(generation, raw, "generation_timeout_seconds", 60),
                60,
                lo=5,
                hi=600,
            ),
            strip_trailing_period=_to_bool(
                _pick(generation, raw, "strip_trailing_period", True), True
            ),
            dedupe_similarity=_to_float(
                _pick(generation, raw, "dedupe_similarity", 0.85),
                0.85,
                lo=0.3,
                hi=1.0,
            ),
            persona_cache_seconds=_to_int(
                _pick(generation, raw, "persona_cache_seconds", 600),
                600,
                lo=0,
                hi=24 * 3600,
            ),
            log_skip_reasons=_to_bool(
                _pick(diagnostics, raw, "log_skip_reasons", True), True
            ),
            retention_days=_to_int(
                _pick(diagnostics, raw, "retention_days", 30), 30, lo=1, hi=365
            ),
        )

    def effective_cooldown_seconds(self, unanswered_streak: int) -> int:
        """Per-target cooldown, stretched when previous proactives were ignored."""
        streak = max(0, int(unanswered_streak))
        if streak <= 0 or self.unanswered_backoff <= 1.0:
            return self.per_target_cooldown_seconds
        factor = min(self.unanswered_backoff_cap, self.unanswered_backoff**streak)
        return int(self.per_target_cooldown_seconds * factor)


@dataclass(frozen=True)
class ProactiveTarget:
    """Candidate recipient discovered from AstrBot history or an extension target selector."""

    umo: str
    user_id: str = ""
    channel_type: str = "private"
    tier: str = "active_peer"
    last_user_msg_ts: int = 0
    recent_turns: tuple[str, ...] = ()
    persona_prompt: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProactiveTurnPlan:
    """Planned proactive message ready for multi-bubble delivery."""

    umo: str
    kind: str
    text: str
    item_key: str = ""
    image_path: str | None = None
    image_first: bool = False
    created_user_msg_ts: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _timezone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def is_in_awake_window(
    now_ts: int,
    *,
    start_hour: int = 8,
    end_hour: int = 23,
    tz_name: str = DEFAULT_TZ,
) -> bool:
    hour = datetime.fromtimestamp(int(now_ts), _timezone(tz_name)).hour
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    if start_hour > end_hour:
        return hour >= start_hour or hour < end_hour
    return True


def local_day_start_ts(now_ts: int, *, tz_name: str = DEFAULT_TZ) -> int:
    dt = datetime.fromtimestamp(int(now_ts), _timezone(tz_name))
    return int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def format_local_ts(ts: int, *, tz_name: str = DEFAULT_TZ) -> str:
    if ts <= 0:
        return "无"
    return datetime.fromtimestamp(int(ts), _timezone(tz_name)).strftime("%m-%d %H:%M")


def is_group_umo(umo: str) -> bool:
    raw = str(umo or "")
    return ":GroupMessage:" in raw or ":group:" in raw.lower()


def format_relative_age(delta_sec: int) -> str:
    sec = max(0, int(delta_sec))
    if sec < 60:
        return "刚刚"
    minutes = sec // 60
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小时前"
    days = hours // 24
    return f"{days} 天前"


def format_turn_clock(
    ts: int,
    *,
    now_ts: int | None = None,
    tz_name: str = DEFAULT_TZ,
) -> str:
    if ts <= 0:
        return ""
    tz = _timezone(tz_name)
    dt = datetime.fromtimestamp(int(ts), tz)
    ref_dt = datetime.fromtimestamp(int(now_ts or time.time()), tz)
    if dt.date() == ref_dt.date():
        return dt.strftime("%H:%M")
    return dt.strftime("%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Text pipeline
# ---------------------------------------------------------------------------


def _strip_wrapping_quotes(text: str) -> str:
    item = text.strip()
    for _ in range(2):
        if len(item) < 2:
            break
        if _WRAPPING_PAIRS.get(item[0]) == item[-1]:
            item = item[1:-1].strip()
        else:
            break
    return item


def _truncate_masked_bubble(text: str, limit: int) -> str:
    """Clamp a URL-masked bubble, preferring a natural punctuation boundary."""
    if limit <= 0 or len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind(ch) for ch in "。！？!?；;，,")
    if cut >= max(1, limit // 2):
        head = window[: cut + 1]
        if head and head[-1] in "，,、：:；;":
            head = head[:-1]
        return head.rstrip()
    # Do not split a URL placeholder such as "\x00U0\x00": drop the whole
    # URL rather than sending a broken link.
    if window.count("\x00") % 2:
        last = window.rfind("\x00")
        return window[:last].rstrip() if last > 0 else ""
    return window.rstrip()


def clamp_bubble_text(text: str, *, max_chars: int = 80) -> str:
    """Clamp one outgoing bubble while keeping URLs intact.

    max_chars <= 0 disables the limit. A URL longer than the limit is kept
    whole, because a partial link is worse than a slightly long bubble.
    """
    raw = str(text or "").strip()
    if max_chars <= 0 or len(raw) <= max_chars:
        return raw

    urls: list[str] = []

    def _mask(match: re.Match[str]) -> str:
        urls.append(match.group(0))
        return f"\x00U{len(urls) - 1}\x00"

    masked = _RE_URL.sub(_mask, raw)
    if len(masked) > max_chars:
        masked = _truncate_masked_bubble(masked, max_chars)
    for idx, url in enumerate(urls):
        masked = masked.replace(f"\x00U{idx}\x00", url)
    return masked.strip()


def clean_proactive_text(
    text: str,
    *,
    max_bubbles: int = 2,
    max_chars_per_bubble: int = 80,
    strip_trailing_period: bool = True,
) -> str:
    """Normalise model output into 1..max_bubbles clean, short bubbles.

    Handles role prefixes, wrapping quotes, code fences, trailing Chinese
    periods and URLs (which must survive the period stripping).
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return ""
    raw = _RE_THINK_BLOCK.sub("", raw)
    raw = _RE_TOOL_CALL_BLOCK.sub("", raw).strip()
    if raw.startswith("{") and raw.endswith("}"):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("text"), str):
            raw = payload["text"].strip()
    if not raw:
        return ""

    urls: list[str] = []

    def _mask(match: re.Match[str]) -> str:
        urls.append(match.group(0))
        return f"\x00U{len(urls) - 1}\x00"

    masked = _RE_URL.sub(_mask, raw)
    masked = _RE_FENCE.sub("", masked)

    lines: list[str] = []
    for line in masked.splitlines():
        item = line.strip()
        if not item:
            continue
        item = _RE_ROLE_PREFIX.sub("", item).strip()
        item = _strip_wrapping_quotes(item)
        if item:
            lines.append(item)

    if not lines:
        return ""
    lines = lines[: max(1, int(max_bubbles))]

    out: list[str] = []
    for line in lines:
        item = line
        for idx, url in enumerate(urls):
            item = item.replace(f"\x00U{idx}\x00", url)
        item = clamp_bubble_text(item, max_chars=max_chars_per_bubble)
        if strip_trailing_period:
            item = item.rstrip("。").rstrip()
        item = item.strip()
        if item:
            out.append(item)
    return "\n".join(out)


def looks_like_truncated_sentence(text: str) -> bool:
    """Detect an obviously cut-off generation.

    Only hard signals count: a sentence that stops on mid-sentence punctuation,
    on a dangling conjunction, or with unbalanced quotes/brackets. Ending on a
    normal function character such as 了/的/着 is *not* a truncation signal.
    """
    s = str(text or "").strip()
    if not s:
        return True
    lines = [line.strip() for line in s.splitlines() if line.strip()]
    if not lines:
        return True
    last = lines[-1]
    if last.endswith(_INCOMPLETE_PUNCT):
        return True
    if last.endswith(_DANGLING_WORDS):
        return True
    for opener, closer in _QUOTE_PAIRS:
        if last.count(opener) > last.count(closer):
            return True
    return False


def _normalise_for_compare(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    return _RE_COMPARE_STRIP.sub("", text)


def text_similarity(left: str, right: str) -> float:
    a = _normalise_for_compare(left)
    b = _normalise_for_compare(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def is_duplicate_text(
    text: str,
    recent_texts: list[str] | tuple[str, ...],
    *,
    threshold: float = 0.85,
) -> bool:
    """Whether text is close enough to any recently sent proactive message."""
    return any(
        text_similarity(text, previous) >= float(threshold) for previous in recent_texts
    )


def _char_bigrams(text: str) -> set[str]:
    norm = _normalise_for_compare(text)
    if len(norm) < 2:
        return {norm} if norm else set()
    return {norm[i : i + 2] for i in range(len(norm) - 1)}


def score_topic_relevance(
    candidate_text: str,
    recent_turns: tuple[str, ...] | list[str] = (),
    *,
    hints: tuple[str, ...] | list[str] = (),
    synonym_groups: tuple[tuple[str, ...], ...] = (),
) -> float:
    """Score how well ``candidate_text`` silently aligns with recent turns & hints (0.0..1.0)."""
    cand_low = str(candidate_text or "").strip().lower()
    if not cand_low:
        return 0.0
    seeds = [str(h).strip() for h in hints if str(h).strip()]
    for turn in recent_turns:
        txt = str(turn or "").strip()
        if ":" in txt:
            txt = txt.split(":", 1)[1].strip()
        elif "：" in txt:
            txt = txt.split("：", 1)[1].strip()
        if txt:
            seeds.append(txt)
    if not seeds:
        return 0.0

    cand_grams = _char_bigrams(cand_low)
    best = 0.0
    for seed in seeds:
        s_low = seed.lower()
        if len(s_low) >= 2 and s_low in cand_low:
            best = max(best, 1.0)
            continue
        for group in synonym_groups:
            g_low = tuple(tok.lower() for tok in group if tok)
            if any(tok in s_low for tok in g_low) and any(tok in cand_low for tok in g_low):
                best = max(best, 0.85)
        s_grams = _char_bigrams(s_low)
        if cand_grams and s_grams:
            inter = len(cand_grams & s_grams)
            if inter:
                sim = inter / ((len(cand_grams) * len(s_grams)) ** 0.5)
                best = max(best, min(0.8, sim))
    return round(best, 4)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def build_proactive_prompts(
    *,
    persona_prompt: str,
    recent_turns: tuple[str, ...] | list[str] = (),
    recent_proactives: list[str] | tuple[str, ...] = (),
    max_bubbles: int = 2,
    unanswered_streak: int = 0,
    now_ts: int | None = None,
    tz_name: str = DEFAULT_TZ,
    source_hint: str = "",
    channel_type: str = "private",
    last_user_msg_ts: int = 0,
    max_context_turns: int = 6,
) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) inheriting the bot's real persona & recent chat context."""
    ts = int(now_ts or time.time())
    dt = datetime.fromtimestamp(ts, _timezone(tz_name))
    bubbles = max(1, int(max_bubbles))
    if bubbles > 1:
        bubble_rule = f"真有补充最多分 {bubbles} 行，每行一句"
    else:
        bubble_rule = "只发一条，不要换行"

    base_persona = str(persona_prompt or "").strip()
    is_group = str(channel_type or "").lower() == "group"
    if is_group:
        proactive_rules = (
            "【群聊主动破冰】\n"
            f"保持你原本的说话口吻，像群友闲着随口冒泡（1~2 句短口语，{bubble_rule}）。\n"
            "别解释动机（别写“好久没聊/突然想起”），别用“大家好/在吗”或新闻播报腔，直接抛出想法、吐槽或顺着群聊接茬，不加引号和句尾句号。"
        )
    else:
        proactive_rules = (
            "【主动冒泡】\n"
            f"保持你原本的说话口吻，像熟人小窗随口说一句（优先 1 句 6~24 字，{bubble_rule}）。\n"
            "别解释动机（严禁“突然想起你喜欢X / 好久没聊了”），少用问号查户口，别客套或叫昵称；直接说想法、吐槽或异步接茬，把话完整说完，不加引号和句尾句号。"
        )
    system = f"{base_persona}\n\n{proactive_rules}" if base_persona else proactive_rules

    user_parts: list[str] = [f"当前本地时间：{dt.strftime('%Y-%m-%d %H:%M')}"]
    if last_user_msg_ts > 0:
        age = format_relative_age(ts - int(last_user_msg_ts))
        last_str = format_local_ts(int(last_user_msg_ts), tz_name=tz_name)
        user_parts.append(f"距离上次互动：{age}（{last_str}）")
    context_limit = max(0, int(max_context_turns))
    context_turns = list(recent_turns)[-context_limit:] if context_limit else []
    if context_turns:
        heading = (
            "群里最近几轮真实发言（只作语境参考，别硬复述）：\n"
            if is_group
            else "你们最近几轮真实对话（顺着这个相处氛围和语感自然接，别生硬重提；注意每条的时间先后）：\n"
        )
        user_parts.append(heading + "\n".join(context_turns))
    if unanswered_streak > 0:
        if is_group:
            user_parts.append(
                f"提醒：你最近在群里主动冒泡过 {int(unanswered_streak)} 次暂时没人接话。"
                "语气更随意克制，别刷屏或追问大家为什么不说话。"
            )
        else:
            user_parts.append(
                f"提醒：你最近主动找过对方 {int(unanswered_streak)} 次，对方都没接话。"
                "别追问、别抱怨、别说「你怎么不理我」；可以更短更随意，只分享一件小事就好。"
            )
    if source_hint:
        user_parts.append(f"本次冒泡灵感/素材：\n{source_hint}")
    if recent_proactives:
        samples = " | ".join(
            x.replace("\n", " ")[:32] for x in list(recent_proactives)[:4]
        )
        user_parts.append(f"避重复（你最近已经主动发过）：{samples}")
    user_parts.append("请直接写出这一条主动消息：")
    return system, "\n\n".join(user_parts)


def next_jittered_timestamp(
    now_ts: int,
    *,
    min_gap_sec: int,
    max_gap_sec: int,
    rng: random.Random | None = None,
) -> int:
    lo = max(60, int(min_gap_sec))
    hi = max(lo, int(max_gap_sec))
    gen = rng or random
    return int(now_ts) + gen.randint(lo, hi)
