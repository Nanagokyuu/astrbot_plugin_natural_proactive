# astrbot_plugin_natural_proactive

A generic, persona-grounded AstrBot Star plugin for **natural proactive messaging**: randomized global jitter, timestamped conversation grounding, anti-interrogation constraints, unanswered-streak backoff, optional group ice-breaking, host history writeback, and a versioned extension API (`natural-proactive.v1`).

![version](https://img.shields.io/badge/version-0.3.0-blue)

## 功能

- **同源人设 & 时间感知语境**：
  - 复用会话当前 `Persona` 的 system prompt（没有会话人设时回落到默认人设）。
  - 近期对话记录带本地时间标记（`[HH:MM]` / `[MM-DD HH:MM]`），并在提示词中注入「距离上次互动时长」，避免把昨天的话题当成刚刚发生。
- **冷启动自动发现**：新装或重启后尚未收到新消息时，自动从宿主 `conversation_manager` 发现活跃窗口内的近期会话。
- **私聊优先 + 可选群聊破冰**：默认仅对私聊主动；开启 `targets.enable_group_proactive` 后支持群聊主动破冰，自动切换独立群聊提示词与独立静默/冷却门槛，并支持读取 `message_history_manager` 群聊流水。
- **反查户口 / 反闹钟**：
  - 全局随机间隔（默认 25–55 分钟）避免准点轮流群发；
  - 提示词禁止句首叫昵称、每句都带问号、机械报时、写一半截断；
  - 气泡数量由 `generation.max_bubbles` 同时约束提示词和实际发送条数；
  - 自动剥离 `<think>`、`<tool_call>` 与意外的 JSON 外壳，保护 URL 不被拆断或误删标点。
- **竞态保护 & 双向存档**：
  - LLM 生成前快照最后消息时间，发送前与多气泡间隔后复查是否出现用户新消息、`bubble_flow` 是否 `in_flight`、插件是否正在重载，有则立刻中止发送；
  - 发送成功后可选写回宿主 `Conversation.history`（默认开启），让用户紧接着回复时主对话模型能无缝接上刚主动发出的内容。
- **回应率感知与护栏**：本地时区清醒时段、每日上限、单对象冷却、聊天后沉默门槛、未回复次数指数退避与熔断，以及基于相似度的主动消息去重。
- **扩展 API**：下游插件可注册进程级 `target_selector` / `source_planner` / `delivery_listener`（热重载不丢失）。

## 运行链路

```
后台 task（启动后 30s 起，每 tick_interval_seconds 一次）
  → 清醒时段闸门
  → 全局抖动闸门（落库，重启/热重载后继续生效）
  → 目标：首次调度自动从宿主 conversation_manager 发现近期活跃会话；优先使用扩展 selector，否则读取持久化活跃对象
  → 过滤：群聊开关 / UMO 白名单 / 静音 / bubble_flow in-flight / 聊天后沉默 / 日上限 / 冷却（含未回复退避）/ 未回复熔断
  → 生成：人设 + 带时间标记的最近对话（群聊可回落 LTM）+ 距上次互动时长 + 最近主动消息避重 + 未回复提示
  → 清洗：剥离 <think>/<tool_call>/JSON 包装、去前缀/引号/代码围栏、去句尾句号、URL 保护、截断与相似度拦截
  → 发送：逐条 Plain，发送前与气泡间复查新消息 / in-flight / 停用状态，条间 1.4–3.8s 间隔
  → 记账：SQLite 落盘 + 写回宿主 Conversation.history + 更新下一次全局允许时间 + 通知 listener
```

## 安装

AstrBot WebUI → 插件市场 → 从 URL 安装，或：

```bash
cd <astrbot_root>/data/plugins
git clone https://github.com/Nanagokyuu/astrbot_plugin_natural_proactive.git
```

需要 AstrBot 4.x。插件无第三方 Python 依赖。

## 配置

| 分组 | 关键项 | 默认 | 说明 |
| --- | --- | --- | --- |
| root | `enabled` | true | 总开关 |
| schedule | `tick_interval_seconds` | 300 | 后台检查周期 |
| schedule | `awake_start_hour` / `awake_end_hour` | 8 / 23 | 本地时区清醒时段（结束小时不含） |
| schedule | `timezone` | Asia/Shanghai | 作息时区 |
| schedule | `global_min_gap_seconds` / `global_max_gap_seconds` | 1500 / 3300 | 全局随机间隔区间 |
| targets | `daily_cap` | 3 | 单对象每日上限 |
| targets | `per_target_cooldown_seconds` | 5400 | 单私聊对象基础冷却 |
| targets | `min_silence_after_chat_seconds` | 600 | 私聊说话后至少沉默多久 |
| targets | `active_window_hours` | 48 | 只对最近 N 小时聊过天的对象主动 |
| targets | `allow_umo_prefixes` | "" | UMO 前缀白名单，对扩展目标同样生效 |
| targets | `max_unanswered` | 3 | 连续未回复达到该值后熔断 |
| targets | `enable_group_proactive` | false | 是否开启群聊主动破冰 |
| targets | `group_min_silence_seconds` | 3600 | 群聊安静至少多久才允许破冰 |
| targets | `group_cooldown_seconds` | 14400 | 同一个群聊的基础冷却 |
| targets | `bootstrap_from_host` | true | 启动时自动从宿主会话历史发现近期活跃对象 |
| engagement | `reply_window_hours` | 24 | 多久内的回复才算"回应" |
| engagement | `unanswered_backoff` / `unanswered_backoff_cap` | 2.0 / 8.0 | 未回复时冷却放大倍数与上限 |
| engagement | `persist_to_conversation_history` | true | 将发出的主动消息写回 AstrBot 会话历史 |
| generation | `provider_id` | "" | 指定 Provider，留空用默认聊天模型 |
| generation | `history_turns` | 6 | 回落读取的会话历史条数 |
| generation | `max_bubbles` | 2 | 单次最多气泡数 |
| generation | `strip_trailing_period` | true | 去掉气泡末尾僵硬的中文句号 |
| generation | `dedupe_similarity` | 0.85 | 与最近主动消息的相似度阈值 |
| generation | `persona_cache_seconds` | 600 | 人设/历史缓存时间 |
| diagnostics | `log_skip_reasons` | true | 记录跳过原因 |
| diagnostics | `retention_days` | 30 | 投递记录保留天数 |

## 命令

| 命令 | 权限 | 作用 |
| --- | --- | --- |
| `/冒泡状态`（`np-status`） | 管理员 | 查看开关、时段、下次全局允许、累计发送、静音数、扩展回调数 |
| `/冒泡测试`（`np-test`） | 管理员 | 立即生成并发送一条，不计入配额与全局闸门 |
| `/别主动找我`（`np-mute`） | 任何人 | 静音当前会话的主动消息 |
| `/恢复冒泡`（`np-unmute`） | 任何人 | 解除静音 |

## 扩展 API（natural-proactive.v1）

```python
from astrbot_plugin_natural_proactive import (
    ProactiveTarget,
    ProactiveTurnPlan,
    discover_proactive_api,
)

api = discover_proactive_api(self.context)
if api is not None:
    token = api.register_target_selector(
        "my.targets", lambda ctx: [ProactiveTarget(umo="default:FriendMessage:123")]
    )
    api.register_source_planner(
        "my.source",
        lambda ctx: ProactiveTurnPlan(umo=ctx.target.umo, kind="mine", text="在忙吗"),
    )
    api.register_delivery_listener("my.observer", lambda plan: print(plan.text))
```

- 回调统一做异常隔离，坏插件不会拖垮主循环。
- 注册表是**进程级**的：配置文件保存导致本插件热重载时，已注册的回调不会丢失；同名（`selector_id` 等）重复注册会替换旧回调，不会叠加。
- 如需在插件卸载时清理，可 `api.unregister(token)`；`api.clear_registrations()` 会清空全部回调，仅建议在测试或明确停用时调用。
- `discover_proactive_api` 依赖本插件已加载。若你的插件先于它加载，可在 `filter.on_plugin_loaded()` 钩子里重试。

## 数据与隐私

状态保存在 `data/plugin_data/astrbot_plugin_natural_proactive/proactive.sqlite3`：

- `proactive_deliveries`：实际发出的文本、时间、是否被回应；
- `proactive_activity`：每个 UMO 最近一次发言时间与最近若干轮对话；
- `proactive_mutes`：用户主动静音；
- `proactive_kv`：下一次全局允许时间。

只记录文本消息，不记录图片/文件；默认仅私聊生效，聊天后沉默、每日上限、未回复熔断共同避免骚扰。用户可随时用 `/别主动找我` 关闭。

## 测试

```bash
python3 -m pytest tests -q
```

测试自带一个极简的 AstrBot stub（`tests/conftest.py`），因此不装 AstrBot 也能跑；CI 覆盖 Python 3.10–3.13 + ruff。

## 设计借鉴

"未回复次数退避"、生成期新消息竞态拦截、写回宿主对话历史、带时间标记的对话记录与群聊 LTM 读取等思路参考了 [astrbot_plugin_proactive_chat](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat)（AGPL-3.0）的交互设计。本项目**没有复制其代码**，实现与提示词均为独立编写。

## License

MIT License. See [LICENSE](LICENSE).
