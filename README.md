# astrbot_plugin_natural_proactive

A generic, persona-grounded AstrBot Star plugin for **natural proactive messaging** with **randomized global jitter**, **recent conversation grounding**, **anti-interrogation constraints**, and a versioned extension API (`natural-proactive.v1`).

## Features

- **Same-Persona & Real-History Grounding**: Reuses your bot's active AstrBot `Persona` system prompt and recent conversation turns instead of an isolated, robotic prompt.
- **Anti-Interrogation & Anti-Clockwork**:
  - Randomized global interval (`global_min_gap_seconds`–`global_max_gap_seconds`, default 25–55 min) prevents clockwork round-robin pinging across contacts.
  - Prompt rules forbid opening with the user's nickname, forcing a question mark every turn, mechanical time-of-day announcements, or truncated sentences.
- **Awake Window & Per-Target Guardrails**: Enforces local timezone awake hours, daily cap, per-target cooldown, post-chat silence threshold, and automatic `astrbot_plugin_bubble_flow` in-flight avoidance.
- **Clean Send Fault Isolation**: Catches platform exceptions (e.g. `ApiNotAvailable`) without aborting the scheduler loop or committing unsent deliveries.
- **Extension API (`natural-proactive.v1`)**: Downstream plugins can register `target_selector`, `source_planner`, and `delivery_listener` callbacks via `discover_proactive_api(self.context)`.

## Installation

Install directly from GitHub in AstrBot WebUI (**Plugins -> Install from URL**), or clone into `data/plugins/`:

```bash
cd <astrbot_root>/data/plugins
git clone https://github.com/Nanagokyuu/astrbot_plugin_natural_proactive.git
```

## Testing

```bash
pytest tests/test_natural_proactive.py -v
```

## License

MIT License. See [LICENSE](LICENSE).
