import asyncio
from pathlib import Path
import random
import sys
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_PARENT = REPO_ROOT.parent
if str(PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(PKG_PARENT))

from astrbot_plugin_natural_proactive import (
    API_VERSION,
    ProactivePolicy,
    ProactiveTarget,
    ProactiveTurnPlan,
    clean_proactive_text,
    discover_proactive_api,
    is_in_awake_window,
)
from astrbot_plugin_natural_proactive.main import NaturalProactivePlugin
from astrbot_plugin_natural_proactive.scheduler import (
    build_proactive_prompts,
    looks_like_truncated_sentence,
    next_jittered_timestamp,
)


class _FakeContext:
    def __init__(self, *, fail_umos: set[str] | None = None, reply_text: str = "刚想到昨天聊那事，笑死") -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_umos = fail_umos or set()
        self.reply_text = reply_text

    def get_all_stars(self):
        return []

    def get_using_provider(self, umo: str = ""):
        text = self.reply_text

        class _Prov:
            async def text_chat(self, prompt: str, system_prompt: str = ""):
                return SimpleNamespace(completion_text=text)

        return _Prov()

    async def send_message(self, umo: str, chain) -> bool:
        if umo in self.fail_umos:
            raise RuntimeError("ApiNotAvailable")
        txt = " | ".join(getattr(c, "text", "") for c in getattr(chain, "chain", ()) if getattr(c, "text", ""))
        self.sent.append((umo, txt))
        return True


def test_clean_text_truncation_awake_window_and_prompts() -> None:
    assert clean_proactive_text("第一句。\n第二句 https://example.com/a。", max_bubbles=2) == "第一句\n第二句 https://example.com/a"
    assert looks_like_truncated_sentence("花了九千多给他买了个配置拉") is False
    assert looks_like_truncated_sentence("花九千多买了个，") is True
    assert looks_like_truncated_sentence("当时买了个的") is True

    sys_p, usr_p = build_proactive_prompts(
        persona_prompt="你是艾莉诺尔，平时说话松弛。",
        recent_turns=["对方: 今天好困", "我: 早点摸鱼"],
        recent_proactives=["刚才那个图绝了"],
        now_ts=1_760_000_000,
    )
    assert "你是艾莉诺尔" in sys_p
    assert "不要每次都以问号结尾查户口" in sys_p
    assert "今天好困" in usr_p
    assert "刚才那个图绝了" in usr_p

    t_next = next_jittered_timestamp(1000, min_gap_sec=1500, max_gap_sec=3300, rng=random.Random(42))
    assert 2500 <= t_next <= 4300
    assert is_in_awake_window(1_763_438_400, start_hour=8, end_hour=23, tz_name="Asia/Shanghai") is True


def test_scheduler_jitter_extension_api_and_send_isolation(tmp_path: Path) -> None:
    async def _run() -> None:
        ctx = _FakeContext(fail_umos={"elle:FriendMessage:fail_user"})
        plugin = NaturalProactivePlugin(
            ctx,
            {
                "enabled": True,
                "schedule": {"awake_start_hour": 0, "awake_end_hour": 24, "global_min_gap_seconds": 1500, "global_max_gap_seconds": 3300},
                "targets": {"daily_cap": 2, "per_target_cooldown_seconds": 3600, "min_silence_after_chat_seconds": 60},
            },
        )
        # Isolate DB in tmp_path
        from astrbot_plugin_natural_proactive.scheduler import ProactiveStateStore
        plugin.store = ProactiveStateStore(tmp_path / "proactive.sqlite3")

        # Register extension API target + source
        delivered: list[str] = []
        plugin.extension_api.register_target_selector(
            "test.targets",
            lambda _ctx: [
                ProactiveTarget(umo="elle:FriendMessage:fail_user", last_user_msg_ts=1000),
                ProactiveTarget(umo="elle:FriendMessage:ok_user", last_user_msg_ts=1000),
            ],
        )
        plugin.extension_api.register_source_planner(
            "test.sources",
            lambda sctx: ProactiveTurnPlan(
                umo=sctx.target.umo,
                kind="custom_spark",
                text="顺手发个冒泡",
                item_key="spark:1",
            )
            if "spark:1" not in sctx.recent_global_keys
            else None,
        )
        plugin.extension_api.register_delivery_listener("test.obs", lambda plan: delivered.append(plan.umo))

        try:
            ok = await plugin.run_tick(now_ts=2000)
            assert ok is True
            assert ctx.sent == [("elle:FriendMessage:ok_user", "顺手发个冒泡")]
            assert delivered == ["elle:FriendMessage:ok_user"]

            # Within global jitter window (e.g. +600s < 1500s min gap) -> throttled
            assert await plugin.run_tick(now_ts=2600) is False

            # After max jitter window (+3700s > 3600s cooldown) -> allowed again
            ok2 = await plugin.run_tick(now_ts=5700)
            assert ok2 is True
        finally:
            await plugin.terminate()

    asyncio.run(_run())
