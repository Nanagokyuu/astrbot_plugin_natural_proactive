# Changelog

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
