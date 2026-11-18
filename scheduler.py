"""Pure policy, time, text and prompt helpers for natural proactive messaging.

This module intentionally has **no AstrBot imports** so the scheduling policy,
text pipeline and prompt builder stay unit-testable without a host runtime.
"""

from __future__ import annotations

import difflib
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
PLUGIN_VERSION = "0.2.0"

_FENCE_CHAR = chr(96)
_RE_URL = re.compile(r"https?://[^\s\n。，！？；：、）】》」』\"'<>]+")
_RE_FENCE = re.compile(r"^" + _FENCE_CHAR + r"{3,}.*$", re.MULTILINE)
_RE_ROLE_PREFIX = re.compile(r"^(?:我|bot|Bot|BOT|AI|ai|助手|机器人|回复)\s*[:：]\s*")
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
    "……",
    "——",
    "…",
    "-",
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
    per_target_cooldown_seconds: int = 5400
    min_silence_after_chat_seconds: int = 600
    active_window_hours: int = 48
    allow_umo_prefixes: tuple[str, ...] = ()
    max_unanswered: int = 3

    reply_window_hours: int = 24
    unanswered_backoff: float = 2.0
    unanswered_backoff_cap: float = 8.0

    provider_id: str = ""
    history_turns: int = 6
    max_bubbles: int = 2
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
            provider_id=_to_str(_pick(generation, raw, "provider_id", "")),
            history_turns=_to_int(
                _pick(generation, raw, "history_turns", 6), 6, lo=0, hi=30
            ),
            max_bubbles=_to_int(
                _pick(generation, raw, "max_bubbles", 2), 2, lo=1, hi=6
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


def clean_proactive_text(
    text: str,
    *,
    max_bubbles: int = 2,
    strip_trailing_period: bool = True,
) -> str:
    """Normalise model output into 1..max_bubbles clean, short bubbles.

    Handles role prefixes, wrapping quotes, code fences, trailing Chinese
    periods and URLs (which must survive the period stripping).
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
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
        item = line.rstrip("。").rstrip() if strip_trailing_period else line
        for idx, url in enumerate(urls):
            item = item.replace(f"\x00U{idx}\x00", url)
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
    proactive_rules = (
        "【主动冒泡规则（必须严格保持你上面原本的说话人设与口吻）】\n"
        "1. 像真人闲着顺手给熟人发一条微信/QQ小窗，优先写 1 条极短自然口语（8~28字），"
        f"{bubble_rule}；禁止小作文与汇报腔。\n"
        "2. 不要每次都以问号结尾查户口！大部分时候直接发一句感叹、自言自语、吐槽或顺着刚才聊天联想到的陈述句即可。\n"
        "3. 严禁在句首叫对方网名/昵称（如「某某，…」），严禁「在吗」「最近怎么样」等客套开场，也别机械报时（如「中午好」「晚上在干嘛」）。\n"
        f"4. 必须把一句话说完整，绝不写到一半截断；直接输出你要发送的话本身，不要加引号、前缀或JSON包装，也不要超过 {bubbles} 行。\n"
        "5. 不要复述你最近主动说过的话，换个话题或换个说法，别显得像定时复读。"
    )
    system = f"{base_persona}\n\n{proactive_rules}" if base_persona else proactive_rules

    user_parts: list[str] = [f"当前本地时间：{dt.strftime('%Y-%m-%d %H:%M')}"]
    if recent_turns:
        user_parts.append(
            "你们最近几轮真实对话（顺着这个相处氛围和语感自然接，别生硬重提）：\n"
            + "\n".join(list(recent_turns)[-6:])
        )
    if unanswered_streak > 0:
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
