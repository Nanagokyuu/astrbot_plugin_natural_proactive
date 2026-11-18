from __future__ import annotations

import asyncio
import json
import random
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_natural_proactive import (
    PLUGIN_VERSION,
    NaturalProactiveAPI,
    ProactivePolicy,
    ProactiveStateStore,
    ProactiveTarget,
    ProactiveTurnPlan,
    build_proactive_prompts,
    clean_proactive_text,
    is_duplicate_text,
    is_in_awake_window,
    looks_like_truncated_sentence,
    next_jittered_timestamp,
    text_similarity,
)
from astrbot_plugin_natural_proactive.main import NaturalProactivePlugin


class _FakeProvider:
    def __init__(self, text: str) -> None:
        self.text = text
        self.last_prompt = ""
        self.last_system = ""

    async def text_chat(self, prompt: str, system_prompt: str = ""):
        self.last_prompt = prompt
        self.last_system = system_prompt
        return SimpleNamespace(completion_text=self.text)


class _FakeContext:
    def __init__(
        self,
        *,
        reply_text: str = "刚想到昨天聊那事，笑死",
        fail_umos: set[str] | None = None,
        fail_on_call: int | None = None,
        conversation_manager=None,
        persona_manager=None,
    ) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_umos = set(fail_umos or ())
        self.fail_on_call = fail_on_call
        self.calls = 0
        self.conversation_manager = conversation_manager
        self.persona_manager = persona_manager
        self.provider = _FakeProvider(reply_text)

    def get_all_stars(self):
        return []

    def get_using_provider(self, umo: str = ""):
        return self.provider

    def get_provider_by_id(self, provider_id: str):
        return self.provider

    async def send_message(self, umo: str, chain) -> bool:
        self.calls += 1
        if umo in self.fail_umos:
            raise RuntimeError("ApiNotAvailable")
        if self.fail_on_call is not None and self.calls == self.fail_on_call:
            raise RuntimeError("SendFailed")
        text = " | ".join(
            getattr(component, "text", "")
            for component in getattr(chain, "chain", ())
            if getattr(component, "text", "")
        )
        self.sent.append((umo, text))
        return True


class _FakeEvent:
    def __init__(
        self, umo: str = "default:FriendMessage:1", text: str = "", chain=None
    ) -> None:
        self.unified_msg_origin = umo
        self.message_str = text
        self._chain = chain
        self.results: list[str] = []

    def is_private_chat(self) -> bool:
        return True

    def get_result(self):
        return SimpleNamespace(chain=self._chain) if self._chain is not None else None

    def plain_result(self, text: str):
        self.results.append(text)
        return SimpleNamespace(text=text)


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


BASE_CONFIG = {
    "enabled": True,
    "schedule": {
        "awake_start_hour": 0,
        "awake_end_hour": 24,
        "timezone": "Asia/Shanghai",
        "global_min_gap_seconds": 60,
        "global_max_gap_seconds": 60,
        "tick_interval_seconds": 30,
    },
    "targets": {
        "daily_cap": 5,
        "per_target_cooldown_seconds": 60,
        "min_silence_after_chat_seconds": 0,
        "active_window_hours": 48,
        "max_unanswered": 5,
    },
    "generation": {"max_bubbles": 1, "history_turns": 6},
}


def _build_plugin(tmp_path: Path, ctx=None, **overrides) -> NaturalProactivePlugin:
    plugin = NaturalProactivePlugin(ctx or _FakeContext(), _merge(BASE_CONFIG, overrides))
    plugin.store = ProactiveStateStore(tmp_path / "state.sqlite3")
    return plugin


@pytest.fixture(autouse=True)
def _clean_registry():
    NaturalProactiveAPI.reset_registry()
    yield
    NaturalProactiveAPI.reset_registry()


def _register_target_and_source(
    plugin: NaturalProactivePlugin, umo: str, text: str = "刚收工，瘫了"
):
    plugin.extension_api.register_target_selector(
        "test.targets",
        lambda _ctx: [ProactiveTarget(umo=umo, last_user_msg_ts=1000)],
    )
    plugin.extension_api.register_source_planner(
        "test.sources",
        lambda sctx: ProactiveTurnPlan(umo=sctx.target.umo, kind="spark", text=text),
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_truncation_heuristic_keeps_natural_endings() -> None:
    for sentence in ("我到家了", "我先睡了", "刚才那个图真的绝了", "笑死了", "挺好看的"):
        assert looks_like_truncated_sentence(sentence) is False, sentence

    assert looks_like_truncated_sentence("花九千多买了个，") is True
    assert looks_like_truncated_sentence("然后") is True
    assert looks_like_truncated_sentence("他说「你好") is True
    assert looks_like_truncated_sentence("") is True


def test_clean_text_strips_prefix_quotes_fences_and_keeps_urls() -> None:
    fence = chr(96) * 3
    assert clean_proactive_text("我：你好呀。", max_bubbles=1) == "你好呀"
    assert (
        clean_proactive_text('"第一句。"\n「第二句。」', max_bubbles=2)
        == "第一句\n第二句"
    )
    assert (
        clean_proactive_text("看这个 https://example.com/a。", max_bubbles=1)
        == "看这个 https://example.com/a"
    )
    assert clean_proactive_text(fence + "\n你好\n" + fence, max_bubbles=1) == "你好"
    assert clean_proactive_text("一\n二\n三", max_bubbles=2) == "一\n二"
    assert clean_proactive_text("   ", max_bubbles=2) == ""


def test_similarity_and_duplicate_detection() -> None:
    assert text_similarity("今天吃了火锅", "今天吃了火锅") == 1.0
    assert text_similarity("今天吃了火锅", "明天要去爬山") < 0.5
    assert is_duplicate_text("今天吃了火锅", ["今天吃了火锅呀"], threshold=0.8) is True
    assert is_duplicate_text("明天要去爬山", ["今天吃了火锅呀"], threshold=0.8) is False


def test_policy_mapping_defaults_and_clamps() -> None:
    default = ProactivePolicy.from_mapping(None)
    assert default.max_bubbles == 2
    assert default.dedupe_similarity == 0.85

    policy = ProactivePolicy.from_mapping(
        {
            "enabled": "false",
            "schedule": {"global_min_gap_seconds": 5000, "global_max_gap_seconds": 100},
            "targets": {"allow_umo_prefixes": "a, b ,", "max_unanswered": 0},
            "generation": {"max_bubbles": 99, "dedupe_similarity": 5},
        }
    )
    assert policy.enabled is False
    assert policy.global_min_gap_seconds == 5000
    assert policy.global_max_gap_seconds == 5000
    assert policy.allow_umo_prefixes == ("a", "b")
    assert policy.max_unanswered == 1
    assert policy.max_bubbles == 6
    assert policy.dedupe_similarity == 1.0

    assert ProactivePolicy.from_mapping({"daily_cap": 7}).daily_cap == 7

    backoff = ProactivePolicy(
        per_target_cooldown_seconds=100,
        unanswered_backoff=2.0,
        unanswered_backoff_cap=8.0,
    )
    assert backoff.effective_cooldown_seconds(0) == 100
    assert backoff.effective_cooldown_seconds(1) == 200
    assert backoff.effective_cooldown_seconds(3) == 800
    assert backoff.effective_cooldown_seconds(9) == 800


def test_prompt_reflects_bubble_count_and_unanswered_streak() -> None:
    sys_p, usr_p = build_proactive_prompts(
        persona_prompt="你是艾莉诺尔，平时说话松弛。",
        recent_turns=["对方: 今天好困", "我: 早点摸鱼"],
        recent_proactives=["刚才那个图绝了"],
        max_bubbles=3,
        unanswered_streak=2,
        now_ts=1_760_000_000,
    )
    assert "你是艾莉诺尔" in sys_p
    assert "不要每次都以问号结尾查户口" in sys_p
    assert "最多分 3 行" in sys_p
    assert "今天好困" in usr_p
    assert "刚才那个图绝了" in usr_p
    assert "2 次" in usr_p


def test_jitter_and_awake_window_bounds() -> None:
    t_next = next_jittered_timestamp(
        1000, min_gap_sec=1500, max_gap_sec=3300, rng=random.Random(42)
    )
    assert 2500 <= t_next <= 4300
    assert is_in_awake_window(0, start_hour=8, end_hour=23, tz_name="Asia/Shanghai")
    assert not is_in_awake_window(0, start_hour=0, end_hour=1, tz_name="Asia/Shanghai")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_store_activity_engagement_mutes_and_migration(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.sqlite3"
    con = sqlite3.connect(legacy)
    con.execute(
        "CREATE TABLE proactive_deliveries ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, umo TEXT NOT NULL, "
        "kind TEXT NOT NULL, item_key TEXT NOT NULL DEFAULT '', "
        "text TEXT NOT NULL, sent_at INTEGER NOT NULL)"
    )
    con.execute(
        "INSERT INTO proactive_deliveries (umo, kind, item_key, text, sent_at) "
        "VALUES ('u', 'k', '', 'hi', 1000)"
    )
    con.commit()
    con.close()

    store = ProactiveStateStore(legacy)
    with sqlite3.connect(legacy) as check:
        columns = {row[1] for row in check.execute("PRAGMA table_info(proactive_deliveries)")}
    assert "answered_at" in columns

    assert store.unanswered_streak("u") == 1
    assert store.mark_latest_answered("u", ts=1500, window_seconds=3600) is True
    assert store.unanswered_streak("u") == 0

    store.record_delivery(umo="u", kind="k", text="later", sent_at=10_000)
    assert store.unanswered_streak("u") == 1
    assert store.mark_latest_answered("u", ts=17_200, window_seconds=3600) is False
    assert store.unanswered_streak("u") == 1

    store.record_inbound_activity("u", ts=20_000, speaker="对方", text="在吗")
    activity = store.load_activity(since_ts=0)
    assert activity["u"][0] == 20_000
    assert "对方: 在吗" in activity["u"][1]

    store.append_turn("u", ts=20_100, speaker="我", text="在")
    assert store.load_activity(since_ts=0)["u"][1][-1] == "我: 在"

    store.mute("u", ts=1, reason="test")
    assert store.is_muted("u") is True
    assert store.muted_umos() == ["u"]
    assert store.unmute("u") is True
    assert store.is_muted("u") is False

    removed, _ = store.prune(
        now_ts=40 * 86400,
        delivery_retention_days=30,
        activity_retention_days=7,
    )
    assert removed == 2


# ---------------------------------------------------------------------------
# Scheduling behaviour
# ---------------------------------------------------------------------------


def test_tick_respects_global_gate_cooldown_and_daily_cap(tmp_path: Path) -> None:
    ctx = _FakeContext()
    plugin = _build_plugin(
        tmp_path, ctx, targets={"daily_cap": 2}, engagement={"unanswered_backoff": 1.0}
    )
    umo = "default:FriendMessage:alice"
    _register_target_and_source(plugin, umo)

    assert asyncio.run(plugin.run_tick(now_ts=1000)) is True
    assert ctx.sent == [(umo, "刚收工，瘫了")]
    assert plugin.store.count_since(umo, since_ts=0) == 1

    assert asyncio.run(plugin.run_tick(now_ts=1030)) is False
    assert len(ctx.sent) == 1

    assert asyncio.run(plugin.run_tick(now_ts=1060)) is True
    assert len(ctx.sent) == 2

    assert asyncio.run(plugin.run_tick(now_ts=1200)) is False
    assert len(ctx.sent) == 2


def test_unanswered_streak_pauses_target_until_reply(tmp_path: Path) -> None:
    ctx = _FakeContext()
    plugin = _build_plugin(
        tmp_path,
        ctx,
        targets={
            "max_unanswered": 1,
            "daily_cap": 10,
            "per_target_cooldown_seconds": 60,
        },
    )
    umo = "default:FriendMessage:bob"
    _register_target_and_source(plugin, umo)

    assert asyncio.run(plugin.run_tick(now_ts=1000)) is True
    assert len(ctx.sent) == 1

    assert asyncio.run(plugin.run_tick(now_ts=1100)) is False
    assert len(ctx.sent) == 1

    plugin.store.mark_latest_answered(umo, ts=1200, window_seconds=24 * 3600)
    assert asyncio.run(plugin.run_tick(now_ts=1300)) is True
    assert len(ctx.sent) == 2


def test_mute_blocks_target(tmp_path: Path) -> None:
    ctx = _FakeContext()
    plugin = _build_plugin(tmp_path, ctx)
    umo = "default:FriendMessage:carol"
    _register_target_and_source(plugin, umo)
    plugin.store.mute(umo, ts=900)

    assert asyncio.run(plugin.run_tick(now_ts=1000)) is False
    assert ctx.sent == []

    plugin.store.unmute(umo)
    assert asyncio.run(plugin.run_tick(now_ts=1000)) is True


def test_allow_prefix_filters_extension_targets(tmp_path: Path) -> None:
    ctx = _FakeContext()
    plugin = _build_plugin(
        tmp_path, ctx, targets={"allow_umo_prefixes": "default:FriendMessage"}
    )
    plugin.extension_api.register_target_selector(
        "t",
        lambda _ctx: [
            ProactiveTarget(umo="other:GroupMessage:1"),
            ProactiveTarget(umo="default:FriendMessage:x"),
        ],
    )
    plugin.extension_api.register_source_planner(
        "s", lambda sctx: ProactiveTurnPlan(umo=sctx.target.umo, kind="k", text="在呢")
    )

    assert asyncio.run(plugin.run_tick(now_ts=1000)) is True
    assert ctx.sent == [("default:FriendMessage:x", "在呢")]


def test_activity_survives_restart(tmp_path: Path) -> None:
    umo = "default:FriendMessage:dave"
    first = _build_plugin(tmp_path, _FakeContext())
    asyncio.run(first._remember_inbound(umo, "我回来了", 1000))

    second = _build_plugin(tmp_path, _FakeContext(reply_text="刚在楼下看见只猫"))
    assert umo not in second._last_user_msg_ts
    asyncio.run(second._restore_state(now_ts=1000))
    assert second._last_user_msg_ts[umo] == 1000
    assert any("我回来了" in turn for turn in second._recent_turns[umo])

    assert asyncio.run(second.run_tick(now_ts=1010)) is True
    assert second.store.count_since(umo, since_ts=0) == 1


def test_extension_registry_survives_plugin_reload(tmp_path: Path) -> None:
    umo = "default:FriendMessage:erin"
    first = _build_plugin(tmp_path, _FakeContext())
    first.extension_api.register_target_selector(
        "persist.targets",
        lambda _ctx: [ProactiveTarget(umo=umo, last_user_msg_ts=1000)],
    )
    first.extension_api.register_source_planner(
        "persist.sources",
        lambda sctx: ProactiveTurnPlan(umo=sctx.target.umo, kind="k", text="还在忙吗"),
    )
    asyncio.run(first.terminate())

    ctx = _FakeContext()
    second = _build_plugin(tmp_path, ctx)
    assert second.extension_api.describe()["target_selectors"] == 1
    assert second.extension_api.describe()["source_planners"] == 1

    second.extension_api.register_target_selector(
        "persist.targets",
        lambda _ctx: [ProactiveTarget(umo=umo, last_user_msg_ts=1000)],
    )
    assert second.extension_api.describe()["target_selectors"] == 1

    assert asyncio.run(second.run_tick(now_ts=1000)) is True
    assert ctx.sent == [(umo, "还在忙吗")]


def test_duplicate_generation_is_rejected(tmp_path: Path) -> None:
    ctx = _FakeContext(reply_text="刚在楼下看见只猫")
    plugin = _build_plugin(tmp_path, ctx)
    umo = "default:FriendMessage:frank"
    plugin.store.record_delivery(umo=umo, kind="k", text="刚在楼下看见只猫", sent_at=10)
    asyncio.run(plugin._remember_inbound(umo, "嗯", 900))

    assert asyncio.run(plugin.run_tick(now_ts=1000)) is False
    assert ctx.sent == []


# ---------------------------------------------------------------------------
# Delivery, host objects, commands
# ---------------------------------------------------------------------------


def test_send_failure_isolation_and_recorded_text(tmp_path: Path, monkeypatch) -> None:
    async def _fast_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    fail_umo = "default:FriendMessage:fail"
    ctx = _FakeContext(fail_umos={fail_umo})
    plugin = _build_plugin(tmp_path, ctx, generation={"max_bubbles": 2})
    plugin.extension_api.register_target_selector(
        "t", lambda _ctx: [ProactiveTarget(umo=fail_umo)]
    )
    plugin.extension_api.register_source_planner(
        "s",
        lambda sctx: ProactiveTurnPlan(
            umo=sctx.target.umo, kind="k", text="第一句\n第二句"
        ),
    )
    assert asyncio.run(plugin.run_tick(now_ts=1000)) is False
    assert ctx.sent == []
    assert plugin.store.count_since(fail_umo, since_ts=0) == 0

    ok_umo = "default:FriendMessage:ok"
    ctx2 = _FakeContext(fail_on_call=2)
    plugin2 = _build_plugin(
        tmp_path / "second", ctx2, generation={"max_bubbles": 2}
    )
    plugin2.extension_api.register_target_selector(
        "t", lambda _ctx: [ProactiveTarget(umo=ok_umo)]
    )
    plugin2.extension_api.register_source_planner(
        "s",
        lambda sctx: ProactiveTurnPlan(
            umo=sctx.target.umo, kind="k", text="第一句\n第二句"
        ),
    )
    assert asyncio.run(plugin2.run_tick(now_ts=1000)) is True
    assert ctx2.sent == [(ok_umo, "第一句")]
    assert plugin2.store.recent_texts_for_umo(ok_umo, limit=1) == ["第一句"]


def test_history_and_persona_are_parsed_from_host_objects(tmp_path: Path) -> None:
    class _ConvMgr:
        async def get_curr_conversation_id(self, umo: str):
            return "cid"

        async def get_conversation(self, umo: str, cid: str):
            return SimpleNamespace(
                persona_id="p1",
                history=json.dumps(
                    [
                        {"role": "user", "content": "今天好困"},
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "早点睡"}],
                        },
                    ],
                    ensure_ascii=False,
                ),
            )

    class _PersonaMgr:
        async def get_persona(self, persona_id: str):
            assert persona_id == "p1"
            return SimpleNamespace(system_prompt="你是艾莉诺尔")

        async def get_default_persona_v3(self, umo: str):
            return {"prompt": "默认人设"}

    ctx = _FakeContext(conversation_manager=_ConvMgr(), persona_manager=_PersonaMgr())
    plugin = _build_plugin(tmp_path, ctx)

    turns = asyncio.run(plugin._load_history_turns("u", plugin.policy))
    assert turns == ("对方: 今天好困", "我: 早点睡")
    assert asyncio.run(plugin._resolve_persona("u", plugin.policy)) == "你是艾莉诺尔"

    fallback = _build_plugin(
        tmp_path / "fallback", _FakeContext(persona_manager=_PersonaMgr())
    )
    assert (
        asyncio.run(fallback._resolve_persona("u", fallback.policy)) == "默认人设"
    )


def test_commands_mute_status_unmute_and_test_send(tmp_path: Path) -> None:
    ctx = _FakeContext(reply_text="刚在楼下看见只猫")
    plugin = _build_plugin(tmp_path, ctx)
    umo = "default:FriendMessage:gina"
    event = _FakeEvent(umo=umo)

    def _collect(agen):
        return asyncio.run(_collect_async(agen))

    results = _collect(plugin.cmd_mute(event))
    assert results and plugin.store.is_muted(umo)

    results = _collect(plugin.cmd_status(event))
    assert results and PLUGIN_VERSION in results[0].text
    assert "下次全局允许" in results[0].text

    results = _collect(plugin.cmd_unmute(event))
    assert results and not plugin.store.is_muted(umo)

    results = _collect(plugin.cmd_test(event))
    assert results and "刚在楼下看见只猫" in results[0].text
    assert ctx.sent == [(umo, "刚在楼下看见只猫")]
    assert plugin.store.count_since(umo, since_ts=0) == 0


async def _collect_async(agen):
    return [item async for item in agen]
