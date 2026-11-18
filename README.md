# astrbot_plugin_natural_proactive

A generic, persona-grounded AstrBot Star plugin for **natural proactive messaging**: randomized global jitter, recent-conversation grounding, anti-interrogation constraints, unanswered-streak backoff, and a versioned extension API (`natural-proactive.v1`).

![version](https://img.shields.io/badge/version-0.2.0-blue)

## 0.2.0：修了什么

这一版把 0.1.0 里几个会让功能"静默失灵"的问题修掉了：

- **截断检测不再误杀正常句子**。旧版把结尾的「了 / 的 / 着 / 在」当作句子被截断，导致「我到家了」「笑死了」「刚才那个图真的绝了」这类日常口语被整条丢弃且不写日志。现在只把真正的未完成信号判为截断：句中标点结尾、悬空连词（然后/但是/因为…）、引号或括号未闭合。
- **真实历史对话终于生效**。AstrBot 的 `Conversation.history` 是 JSON 字符串而不是 list，旧代码的 `isinstance(hist, list)` 永远为假，README 宣称的 "Real-History Grounding" 实际从未运行。现在会正确解析字符串/列表两种形态，并支持 content 分片。
- **活跃状态持久化**。观测到的"谁最近说过话 + 最近几轮对话"会写入 SQLite，重启或 WebUI 保存配置触发热重载后自动恢复，不再出现"重启后一整天不主动找人"。
- **首次发送有抖动基线**。新装或清库后会先写入一个随机的 next-allowed 时间，不会在启动 30 秒后立刻发消息。
- **热重载不再丢下游注册**。扩展回调放进模块级注册表，同名注册会替换而不是叠加；配置文件保存导致插件重建时，下游插件注册的 selector/planner/listener 仍然有效。
- **回应率感知**。新增 未回复计数 → 冷却指数退避 → 达到上限后熔断，并把"对方几次没接话"以克制的方式写进提示词。
- **近似重复拦截**。发送前用归一化 + difflib 相似度对比最近 4 条主动消息，避免每次都是同一句话。
- **发送前复查 in-flight**。不再只在选目标时检查 `astrbot_plugin_bubble_flow`，真正发送每条气泡前都会再确认一次，避免和正在生成的回复交叉。
- **可观测性**。每个对象被跳过的原因（时段/全局闸门/冷却/日上限/未回复熔断/静音/in-flight/截断/重复）都会按配置写日志。
- **管理/用户命令**：`/冒泡状态`、`/冒泡测试`、`/别主动找我`、`/恢复冒泡`。
- SQLite 开启 WAL、按保留期自动清理；人设与历史按 `persona_cache_seconds` 缓存，避免每个 tick 反复序列化整段会话。

## 功能

- **同源人设 & 真实语境**：复用会话当前 `Persona` 的 system prompt（没有会话人设时回落到默认人设），并拼接最近真实对话。
- **反查户口 / 反闹钟**：
  - 全局随机间隔（默认 25–55 分钟）避免准点轮流群发；
  - 提示词禁止句首叫昵称、每句都带问号、机械报时、写一半截断；
  - 气泡数量由 `generation.max_bubbles` 同时约束提示词和实际发送条数。
- **清醒时段与单对象护栏**：本地时区清醒时段、每日上限、单对象冷却、聊天后沉默门槛、未回复退避与熔断。
- **发送故障隔离**：单条发送异常不会中断调度循环；只记录真正发出去的内容。
- **扩展 API**：下游插件可注册 `target_selector` / `source_planner` / `delivery_listener`。

## 运行链路

```
后台 task（启动后 30s 起，每 tick_interval_seconds 一次）
  → 清醒时段闸门
  → 全局抖动闸门（落库，重启/热重载后继续生效）
  → 目标：扩展 selector，否则读取持久化的 48h 内私聊活跃对象
  → 过滤：白名单 / 静音 / bubble_flow in-flight / 聊天后沉默 / 日上限 / 冷却（含未回复退避）/ 未回复熔断
  → 生成：人设 + 最近对话 + 最近主动消息避重 + 未回复提示
  → 清洗：去前缀/引号/代码围栏、去句尾句号、气泡数限制、URL 保护
  → 发送：逐条 Plain，发送前复查 in-flight，条间 1.4–3.8s 间隔
  → 记账：实际发送文本 + 下一次全局允许时间 + 通知 listener
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
| targets | `per_target_cooldown_seconds` | 5400 | 单对象基础冷却 |
| targets | `min_silence_after_chat_seconds` | 600 | 对方说话后至少沉默多久 |
| targets | `active_window_hours` | 48 | 只对最近 N 小时聊过的人主动 |
| targets | `allow_umo_prefixes` | "" | UMO 前缀白名单，对扩展目标同样生效 |
| targets | `max_unanswered` | 3 | 连续未回复达到该值后熔断 |
| engagement | `reply_window_hours` | 24 | 多久内的回复才算"回应" |
| engagement | `unanswered_backoff` / `unanswered_backoff_cap` | 2.0 / 8.0 | 未回复时冷却放大倍数与上限 |
| generation | `provider_id` | "" | 指定 Provider，留空用默认聊天模型 |
| generation | `history_turns` | 6 | 回落读取的会话历史条数 |
| generation | `max_bubbles` | 2 | 单次最多气泡数 |
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

只记录私聊文本消息，不记录图片/文件；聊天后沉默、每日上限、未回复熔断共同避免骚扰。用户可随时用 `/别主动找我` 关闭。

## 测试

```bash
python3 -m pytest tests -q
```

测试自带一个极简的 AstrBot stub（`tests/conftest.py`），因此不装 AstrBot 也能跑；CI 覆盖 Python 3.10–3.13 + ruff。

## 设计借鉴

"未回复次数"这一产品思路参考了 [astrbot_plugin_proactive_chat](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat) 的 `unanswered_count` / `max_unanswered` 设计（AGPL-3.0）。本项目**没有复制其代码**，只借鉴了公开的交互概念，实现与提示词均为独立编写。

## License

MIT License. See [LICENSE](LICENSE).
