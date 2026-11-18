"""Generic proactive scheduler, SQLite state store, and anti-robotic prompt builder."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import random
import re
import sqlite3
import time
from typing import Any, Iterator
from zoneinfo import ZoneInfo

_RE_URL = re.compile(r"https?://[^\s\n。，！？；：、）】》」』\"'<>]+")
_RE_TRAILING_CN_PERIOD = re.compile(r"。+(?=\s*(?:\n|$))")
_DANGLING_TAILS = ("，", "、", "：", "；", "的", "了", "着", "和", "跟", "把", "被", "给", "在", "从", "对", "但", "可")


@dataclass(frozen=True)
class ProactivePolicy:
    """Configuration policy for generic natural proactive messaging."""

    enabled: bool = True
    tick_interval_seconds: int = 300
    awake_start_hour: int = 8
    awake_end_hour: int = 23
    timezone: str = "Asia/Shanghai"
    global_min_gap_seconds: int = 1500
    global_max_gap_seconds: int = 3300
    daily_cap: int = 3
    per_target_cooldown_seconds: int = 5400
    min_silence_after_chat_seconds: int = 600
    active_window_hours: int = 48
    allow_umo_prefixes: tuple[str, ...] = ()
    provider_id: str = ""
    history_turns: int = 6
    max_bubbles: int = 2
    strip_trailing_period: bool = True

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "ProactivePolicy":
        if not isinstance(raw, dict):
            return cls()
        sched = raw.get("schedule") if isinstance(raw.get("schedule"), dict) else {}
        tgts = raw.get("targets") if isinstance(raw.get("targets"), dict) else {}
        gen = raw.get("generation") if isinstance(raw.get("generation"), dict) else {}

        prefixes_raw = str(tgts.get("allow_umo_prefixes", raw.get("allow_umo_prefixes", "")) or "")
        prefixes = tuple(p.strip() for p in prefixes_raw.split(",") if p.strip())
        g_min = max(60, int(sched.get("global_min_gap_seconds", raw.get("global_min_gap_seconds", 1500))))
        g_max = max(g_min, int(sched.get("global_max_gap_seconds", raw.get("global_max_gap_seconds", 3300))))

        return cls(
            enabled=bool(raw.get("enabled", True)),
            tick_interval_seconds=max(30, int(sched.get("tick_interval_seconds", raw.get("tick_interval_seconds", 300)))),
            awake_start_hour=max(0, min(23, int(sched.get("awake_start_hour", raw.get("awake_start_hour", 8))))),
            awake_end_hour=max(1, min(24, int(sched.get("awake_end_hour", raw.get("awake_end_hour", 23))))),
            timezone=str(sched.get("timezone", raw.get("timezone", "Asia/Shanghai")) or "Asia/Shanghai"),
            global_min_gap_seconds=g_min,
            global_max_gap_seconds=g_max,
            daily_cap=max(1, int(tgts.get("daily_cap", raw.get("daily_cap", 3)))),
            per_target_cooldown_seconds=max(60, int(tgts.get("per_target_cooldown_seconds", raw.get("per_target_cooldown_seconds", 5400)))),
            min_silence_after_chat_seconds=max(0, int(tgts.get("min_silence_after_chat_seconds", raw.get("min_silence_after_chat_seconds", 600)))),
            active_window_hours=max(1, int(tgts.get("active_window_hours", raw.get("active_window_hours", 48)))),
            allow_umo_prefixes=prefixes,
            provider_id=str(gen.get("provider_id", raw.get("provider_id", "")) or "").strip(),
            history_turns=max(0, int(gen.get("history_turns", raw.get("history_turns", 6)))),
            max_bubbles=max(1, int(gen.get("max_bubbles", raw.get("max_bubbles", 2)))),
            strip_trailing_period=bool(gen.get("strip_trailing_period", raw.get("strip_trailing_period", True))),
        )


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


def is_in_awake_window(
    now_ts: int,
    *,
    start_hour: int = 8,
    end_hour: int = 23,
    tz_name: str = "Asia/Shanghai",
) -> bool:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    hour = datetime.fromtimestamp(int(now_ts), tz).hour
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    if start_hour > end_hour:
        return hour >= start_hour or hour < end_hour
    return True


def local_day_start_ts(now_ts: int, *, tz_name: str = "Asia/Shanghai") -> int:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    dt = datetime.fromtimestamp(int(now_ts), tz)
    return int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def clean_proactive_text(
    text: str,
    *,
    max_bubbles: int = 2,
    strip_trailing_period: bool = True,
) -> str:
    """Split lines into clean short bubbles while preserving URLs."""
    raw = str(text or "").replace("\r\n", "\n").strip()
    if not raw:
        return ""
    urls: list[str] = []

    def _mask(m: re.Match[str]) -> str:
        urls.append(m.group(0))
        return f"\x00URL{len(urls) - 1}\x00"

    masked = _RE_URL.sub(_mask, raw)
    lines = [ln.strip() for ln in masked.splitlines() if ln.strip()][: max(1, int(max_bubbles))]
    out: list[str] = []
    for ln in lines:
        item = _RE_TRAILING_CN_PERIOD.sub("", ln).strip() if strip_trailing_period else ln
        for idx, u in enumerate(urls):
            item = item.replace(f"\x00URL{idx}\x00", u)
        if item:
            out.append(item)
    return "\n".join(out)


def looks_like_truncated_sentence(text: str) -> bool:
    """Detect obvious mid-sentence cutoff so incomplete LLM outputs are rejected."""
    s = str(text or "").strip()
    if not s:
        return True
    last_line = [ln.strip() for ln in s.splitlines() if ln.strip()][-1]
    if last_line.endswith(_DANGLING_TAILS):
        return True
    return False


def build_proactive_prompts(
    *,
    persona_prompt: str,
    recent_turns: tuple[str, ...] | list[str] = (),
    recent_proactives: list[str] | tuple[str, ...] = (),
    now_ts: int | None = None,
    tz_name: str = "Asia/Shanghai",
    source_hint: str = "",
) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) inheriting the bot's real persona & recent chat context."""
    ts = int(now_ts or time.time())
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    dt = datetime.fromtimestamp(ts, tz)

    base_persona = str(persona_prompt or "").strip()
    proactive_rules = (
        "【主动冒泡规则（必须严格保持你上面原本的说话人设与口吻）】\n"
        "1. 像真人闲着顺手给熟人发一条微信/QQ小窗，优先写 1 条极短自然口语（8~28字；真有补充最多分 2 行），禁止小作文与汇报腔。\n"
        "2. 不要每次都以问号结尾查户口！大部分时候直接发一句感叹、自言自语、吐槽或顺着刚才聊天联想到的陈述句即可。\n"
        "3. 严禁在句首叫对方网名/昵称（如「某某，…」），严禁「在吗」「最近怎么样」等客套开场，也别机械报时（如「中午好」「晚上在干嘛」）。\n"
        "4. 必须把一句话说完整，绝不写到一半截断；直接输出你要发送的话本身，不要加引号、前缀或JSON包装。"
    )
    system = f"{base_persona}\n\n{proactive_rules}" if base_persona else proactive_rules

    user_parts: list[str] = [f"当前本地时间：{dt.strftime('%Y-%m-%d %H:%M')}"]
    if recent_turns:
        user_parts.append("你们最近几轮真实对话（顺着这个相处氛围和语感自然接，别生硬重提）：\n" + "\n".join(recent_turns[-6:]))
    if source_hint:
        user_parts.append(f"本次冒泡灵感/素材：\n{source_hint}")
    if recent_proactives:
        samples = " | ".join(x.replace("\n", " ")[:32] for x in recent_proactives[:4])
        user_parts.append(f"避重复（你最近已经主动发过）：{samples}")
    user_parts.append("请直接写出这一条主动消息：")
    return system, "\n\n".join(user_parts)


class ProactiveStateStore:
    """Lightweight SQLite persistence for proactive cooldowns, caps, and global jitter."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(str(self.db_path), timeout=5.0)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _init_schema(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS proactive_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    umo TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    item_key TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL,
                    sent_at INTEGER NOT NULL
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_proactive_umo_ts ON proactive_deliveries (umo, sent_at)"
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS proactive_kv (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL
                )
                """
            )

    def count_since(self, umo: str, *, since_ts: int) -> int:
        with self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS c FROM proactive_deliveries WHERE umo = ? AND sent_at >= ?",
                (umo, int(since_ts)),
            ).fetchone()
        return int(row["c"] or 0) if row else 0

    def latest_for_umo(self, umo: str) -> int:
        with self._connect() as con:
            row = con.execute(
                "SELECT MAX(sent_at) AS ts FROM proactive_deliveries WHERE umo = ?",
                (umo,),
            ).fetchone()
        return int(row["ts"] or 0) if row else 0

    def latest_global_ts(self) -> int:
        with self._connect() as con:
            row = con.execute("SELECT MAX(sent_at) AS ts FROM proactive_deliveries").fetchone()
        return int(row["ts"] or 0) if row else 0

    def next_global_allowed_ts(self) -> int:
        with self._connect() as con:
            row = con.execute("SELECT v FROM proactive_kv WHERE k = 'next_global_allowed_ts'").fetchone()
        try:
            return int(row["v"]) if row else 0
        except Exception:
            return 0

    def recent_texts_for_umo(self, umo: str, *, limit: int = 4) -> list[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT text FROM proactive_deliveries WHERE umo = ? ORDER BY id DESC LIMIT ?",
                (umo, max(1, int(limit))),
            ).fetchall()
        return [str(r["text"]) for r in rows if str(r["text"] or "").strip()]

    def recent_global_item_keys(self, *, since_ts: int) -> set[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT DISTINCT item_key FROM proactive_deliveries WHERE sent_at >= ? AND item_key != ''",
                (int(since_ts),),
            ).fetchall()
        return {str(r["item_key"]) for r in rows}

    def record_delivery(
        self,
        *,
        umo: str,
        kind: str,
        text: str,
        item_key: str = "",
        sent_at: int | None = None,
        next_global_allowed_ts: int | None = None,
    ) -> None:
        ts = int(sent_at or time.time())
        with self._connect() as con:
            con.execute(
                "INSERT INTO proactive_deliveries (umo, kind, item_key, text, sent_at) VALUES (?, ?, ?, ?, ?)",
                (umo, kind, item_key, text, ts),
            )
            if next_global_allowed_ts is not None:
                con.execute(
                    "INSERT OR REPLACE INTO proactive_kv (k, v) VALUES ('next_global_allowed_ts', ?)",
                    (str(int(next_global_allowed_ts)),),
                )


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
