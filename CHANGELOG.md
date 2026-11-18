# Changelog

## [0.3.1] - 2026-11-18

### Fixed

- 晚回复永久熔断：达到 max_unanswered 后，即使回复晚于 reply_window_hours，也会解除熔断；回复窗口只用于统计“回应”，不再把对象永久锁死。
- 扩展注册表在真实热重载下丢失：注册表移到解释器级（sys），AstrBot 的模块 purge/reload 不再清空下游回调。
- 写回宿主历史伪造 user 消息：改为写入 system 事件标记 + assistant 消息，主模型不会把「主动发起对话」当成用户发言。
- 纯图片计划无法通过 source_planner：plan_from_sources 现在接受 text="" + image_path。
- 冷启动发现失败一次后永久放弃：_bootstrap_host_targets 成功后才置位，失败会在下个 tick 重试；get_filtered_conversations 异常时回落到 get_conversations。
- 非文本回复不算回应：表情/图片/语音回复也会更新活动时间、清除聊天后沉默并标记回应，但内容不落盘。
- 截断启发式不再把省略号、破折号结尾判为截断（「我想想…」「算了——」）。
- history_turns 大于 6 时多出的对话现在会进入提示词。
- 保留期清理不再只在启动时执行，改为每 24 小时一次。
- CI：修复测试 import 顺序与错误的 from scheduler import 路径；stub 环境下的历史写回测试现在可通过。

### Added

- generation.max_chars_per_bubble（默认 80）：单条气泡字符上限，在标点处截断，URL 不截断。
- generation.generation_timeout_seconds（默认 60）：LLM 生成超时保护。
- 连续 3 个 tick 生成失败后自动推进全局闸门，避免每 5 分钟重复调用模型。
- target_selector 可通过 ProactiveTarget.metadata 的 source_hint 向默认生成注入素材。

### Changed

- metadata.yaml 的 astrbot_version 改为 >=4.19.1（filter.on_plugin_loaded 从 4.19 起提供），并对该钩子做了兼容处理。
- unanswered_streak 的查询上限从 12 提高到 50，与 max_unanswered 的配置范围一致。

## [0.3.0] - 2026-11-18

- 群聊主动破冰、独立群聊静默/冷却门槛、群聊 LTM 读取。
- 首次调度从宿主 conversation_manager 发现近期活跃对象。
- 对话轮次带本地时间标记；未回复退避与熔断；写回宿主 Conversation.history。
- 扩展 API 注册表支持同名替换与优先级。

## [0.2.0] - 2026-11-18

### Fixed

- 截断启发式不再把「了 / 的 / 着 / 在」结尾的正常口语句子判为截断；只拦截句中标点、悬空连词与未闭合引号。
- 修复 `Conversation.history` 为 JSON 字符串时历史对话从未被读取的问题（旧代码只接受 list）。
- 活跃对象与最近对话写入 SQLite，重启/热重载后自动恢复。
- 新装或清库时写入初始随机抖动基线，避免启动后立即发送。
- 扩展回调改为进程级注册表，配置热重载不再丢失下游注册；同名注册替换而非叠加。
- 第 2 条气泡发送失败时，只记录真正发出的文本。
- 发送每条气泡前复查 bubble_flow in-flight 状态。

### Added

- 未回复计数、冷却指数退避与 `max_unanswered` 熔断，并在提示词中加入克制的未回复提示。
- 与最近主动消息的相似度去重（`dedupe_similarity`）。
- 人设/历史缓存与默认人设回落。
- 命令：`/冒泡状态`、`/冒泡测试`、`/别主动找我`、`/恢复冒泡`。
- 跳过原因日志（`log_skip_reasons`）。
- SQLite WAL、投递记录保留期与自动清理。
- 测试自带 AstrBot stub，可在无宿主环境运行；GitHub Actions（3.10–3.13 + ruff）。

### Changed

- 提示词中的气泡数改为读取 `max_bubbles`（旧版硬编码 2 行）。
- 配置新增 `engagement`、`diagnostics` 分组与 `targets.max_unanswered`。
- state store 拆分为独立模块 `store.py`。
- 版本号提升到 0.2.0，metadata 增加 `display_name` / `short_desc` / `astrbot_version`。

## [0.1.0] - 2026-11-18

- 首个版本：随机抖动的主动私聊、人设与历史语境、扩展 API `natural-proactive.v1`。
