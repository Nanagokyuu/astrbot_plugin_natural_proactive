"""SQLite persistence for proactive deliveries, engagement and activity state.

The store is deliberately synchronous: every operation is a single short
transaction on a WAL database, and callers run the heavier ones through
asyncio.to_thread so the event loop is never blocked by disk work.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2


class ProactiveStateStore:
    """Lightweight SQLite persistence for cooldowns, caps, engagement and jitter."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # -- plumbing ---------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(str(self.db_path), timeout=10.0)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA busy_timeout=10000")
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
                    sent_at INTEGER NOT NULL,
                    answered_at INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            columns = {
                str(row["name"])
                for row in con.execute("PRAGMA table_info(proactive_deliveries)")
            }
            if "answered_at" not in columns:
                con.execute(
                    "ALTER TABLE proactive_deliveries "
                    "ADD COLUMN answered_at INTEGER NOT NULL DEFAULT 0"
                )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_proactive_umo_ts "
                "ON proactive_deliveries (umo, sent_at)"
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS proactive_kv (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS proactive_activity (
                    umo TEXT PRIMARY KEY,
                    last_user_msg_ts INTEGER NOT NULL DEFAULT 0,
                    turns TEXT NOT NULL DEFAULT '[]',
                    updated_at INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS proactive_mutes (
                    umo TEXT PRIMARY KEY,
                    muted_at INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT ''
                )
                """
            )
            con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._enable_wal()

    def _enable_wal(self) -> None:
        try:
            with self._connect() as con:
                con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            # WAL is an optimisation; an unusual filesystem must not stop the plugin.
            pass

    # -- deliveries -------------------------------------------------------

    def count_since(self, umo: str, *, since_ts: int) -> int:
        with self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS c FROM proactive_deliveries "
                "WHERE umo = ? AND sent_at >= ?",
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
            row = con.execute(
                "SELECT v FROM proactive_kv WHERE k = 'next_global_allowed_ts'"
            ).fetchone()
        try:
            return int(row["v"]) if row else 0
        except (TypeError, ValueError):
            return 0

    def seed_global_gate(self, ts: int) -> None:
        """Install an initial jitter baseline only when none exists yet."""
        with self._connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO proactive_kv (k, v) "
                "VALUES ('next_global_allowed_ts', ?)",
                (str(int(ts)),),
            )

    def set_next_global_allowed_ts(self, ts: int) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO proactive_kv (k, v) "
                "VALUES ('next_global_allowed_ts', ?)",
                (str(int(ts)),),
            )

    def recent_texts_for_umo(self, umo: str, *, limit: int = 4) -> list[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT text FROM proactive_deliveries WHERE umo = ? "
                "ORDER BY id DESC LIMIT ?",
                (umo, max(1, int(limit))),
            ).fetchall()
        return [str(r["text"]) for r in rows if str(r["text"] or "").strip()]

    def recent_global_item_keys(self, *, since_ts: int) -> set[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT DISTINCT item_key FROM proactive_deliveries "
                "WHERE sent_at >= ? AND item_key != ''",
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
                "INSERT INTO proactive_deliveries "
                "(umo, kind, item_key, text, sent_at, answered_at) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (umo, kind, item_key, text, ts),
            )
            if next_global_allowed_ts is not None:
                con.execute(
                    "INSERT OR REPLACE INTO proactive_kv (k, v) "
                    "VALUES ('next_global_allowed_ts', ?)",
                    (str(int(next_global_allowed_ts)),),
                )

    # -- engagement -------------------------------------------------------

    def mark_latest_answered(self, umo: str, *, ts: int, window_seconds: int) -> bool:
        """Mark the most recent unanswered proactive as answered by this reply.

        A reply only counts when it arrives inside the engagement window after
        that proactive was sent, so a message sent days later cannot clear a
        long streak of ignored proactives.
        """
        cutoff = int(ts) - max(0, int(window_seconds))
        with self._connect() as con:
            cursor = con.execute(
                "UPDATE proactive_deliveries SET answered_at = ? WHERE id = ("
                "  SELECT id FROM proactive_deliveries"
                "  WHERE umo = ? AND answered_at = 0 AND sent_at >= ?"
                "  ORDER BY id DESC LIMIT 1"
                ")",
                (int(ts), umo, cutoff),
            )
            return cursor.rowcount > 0

    def clear_unanswered(self, umo: str, *, ts: int) -> int:
        """Mark every unanswered proactive as answered for this UMO.

        Used to release a circuit breaker: the user has replied, even if the
        reply arrived outside the normal engagement window.
        """
        with self._connect() as con:
            cursor = con.execute(
                "UPDATE proactive_deliveries SET answered_at = ? "
                "WHERE umo = ? AND answered_at = 0",
                (int(ts), umo),
            )
            return int(cursor.rowcount)

    def unanswered_streak(self, umo: str, *, limit: int = 50) -> int:
        with self._connect() as con:
            rows = con.execute(
                "SELECT answered_at FROM proactive_deliveries WHERE umo = ? "
                "ORDER BY id DESC LIMIT ?",
                (umo, max(1, int(limit))),
            ).fetchall()
        streak = 0
        for row in rows:
            if int(row["answered_at"] or 0) != 0:
                break
            streak += 1
        return streak

    # -- activity ---------------------------------------------------------

    def record_inbound_activity(
        self,
        umo: str,
        *,
        ts: int,
        speaker: str,
        text: str,
        max_items: int = 12,
    ) -> None:
        turns = self._read_turns(umo)
        turns.append(f"{speaker}: {str(text)[:120]}")
        turns = turns[-max(1, int(max_items)) :]
        with self._connect() as con:
            con.execute(
                "INSERT INTO proactive_activity "
                "(umo, last_user_msg_ts, turns, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(umo) DO UPDATE SET "
                "last_user_msg_ts = excluded.last_user_msg_ts, "
                "turns = excluded.turns, "
                "updated_at = excluded.updated_at",
                (umo, int(ts), json.dumps(turns, ensure_ascii=False), int(ts)),
            )

    def touch_activity(self, umo: str, *, ts: int) -> None:
        """Record activity time without storing message text (e.g. a sticker)."""
        with self._connect() as con:
            con.execute(
                "INSERT INTO proactive_activity "
                "(umo, last_user_msg_ts, turns, updated_at) VALUES (?, ?, '[]', ?) "
                "ON CONFLICT(umo) DO UPDATE SET "
                "last_user_msg_ts = excluded.last_user_msg_ts, "
                "updated_at = excluded.updated_at",
                (umo, int(ts), int(ts)),
            )

    def append_turn(
        self,
        umo: str,
        *,
        ts: int,
        speaker: str,
        text: str,
        max_items: int = 12,
    ) -> None:
        """Append one side of the conversation without touching activity time."""
        turns = self._read_turns(umo)
        turns.append(f"{speaker}: {str(text)[:120]}")
        turns = turns[-max(1, int(max_items)) :]
        with self._connect() as con:
            con.execute(
                "INSERT INTO proactive_activity "
                "(umo, last_user_msg_ts, turns, updated_at) VALUES (?, 0, ?, ?) "
                "ON CONFLICT(umo) DO UPDATE SET "
                "turns = excluded.turns, "
                "updated_at = excluded.updated_at",
                (umo, json.dumps(turns, ensure_ascii=False), int(ts)),
            )

    def _read_turns(self, umo: str) -> list[str]:
        with self._connect() as con:
            row = con.execute(
                "SELECT turns FROM proactive_activity WHERE umo = ?", (umo,)
            ).fetchone()
        if not row:
            return []
        try:
            data = json.loads(row["turns"] or "[]")
        except (TypeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [str(item) for item in data if isinstance(item, str)][-12:]

    def load_activity(self, *, since_ts: int) -> dict[str, tuple[int, list[str]]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT umo, last_user_msg_ts, turns FROM proactive_activity "
                "WHERE last_user_msg_ts >= ?",
                (int(since_ts),),
            ).fetchall()
        out: dict[str, tuple[int, list[str]]] = {}
        for row in rows:
            try:
                turns = json.loads(row["turns"] or "[]")
            except (TypeError, ValueError):
                turns = []
            if not isinstance(turns, list):
                turns = []
            out[str(row["umo"])] = (
                int(row["last_user_msg_ts"] or 0),
                [str(item) for item in turns if isinstance(item, str)],
            )
        return out

    # -- mutes ------------------------------------------------------------

    def mute(self, umo: str, *, ts: int | None = None, reason: str = "") -> None:
        with self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO proactive_mutes (umo, muted_at, reason) "
                "VALUES (?, ?, ?)",
                (umo, int(ts or time.time()), reason[:120]),
            )

    def unmute(self, umo: str) -> bool:
        with self._connect() as con:
            cursor = con.execute("DELETE FROM proactive_mutes WHERE umo = ?", (umo,))
            return cursor.rowcount > 0

    def is_muted(self, umo: str) -> bool:
        with self._connect() as con:
            row = con.execute(
                "SELECT 1 AS m FROM proactive_mutes WHERE umo = ?", (umo,)
            ).fetchone()
        return row is not None

    def muted_umos(self) -> list[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT umo FROM proactive_mutes ORDER BY muted_at DESC"
            ).fetchall()
        return [str(r["umo"]) for r in rows]

    # -- housekeeping -----------------------------------------------------

    def prune(
        self,
        *,
        now_ts: int | None = None,
        delivery_retention_days: int = 30,
        activity_retention_days: int = 7,
    ) -> tuple[int, int]:
        now = int(now_ts or time.time())
        delivery_cutoff = now - max(1, int(delivery_retention_days)) * 86400
        activity_cutoff = now - max(1, int(activity_retention_days)) * 86400
        with self._connect() as con:
            deliveries = con.execute(
                "DELETE FROM proactive_deliveries WHERE sent_at < ?",
                (delivery_cutoff,),
            ).rowcount
            activities = con.execute(
                "DELETE FROM proactive_activity WHERE last_user_msg_ts < ?",
                (activity_cutoff,),
            ).rowcount
        return int(deliveries), int(activities)

    def stats(self) -> dict[str, Any]:
        with self._connect() as con:
            total = con.execute(
                "SELECT COUNT(*) AS c FROM proactive_deliveries"
            ).fetchone()["c"]
            last = con.execute(
                "SELECT MAX(sent_at) AS ts FROM proactive_deliveries"
            ).fetchone()["ts"]
            tracked = con.execute(
                "SELECT COUNT(*) AS c FROM proactive_activity"
            ).fetchone()["c"]
            muted = con.execute(
                "SELECT COUNT(*) AS c FROM proactive_mutes"
            ).fetchone()["c"]
        return {
            "total_deliveries": int(total or 0),
            "last_sent_at": int(last or 0),
            "tracked_targets": int(tracked or 0),
            "muted_targets": int(muted or 0),
        }
