<div align="center">

# Debata_Agent

**A chatbot framework that makes virtual characters feel alive**

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)](https://github.com/binglang001/Debata_Agent/actions/workflows/test.yml)
[![GUI](https://img.shields.io/badge/UI-PySide6-blueviolet.svg)](#)
[![Stars](https://img.shields.io/github/stars/binglang001/Debata_Agent?style=social)](https://github.com/binglang001/Debata_Agent)

<br>

<img src="https://raw.githubusercontent.com/binglang001/Debata_Agent/main/ui/icon.png" width="128" alt="Debata">

</div>

---

> "I wanted a character who doesn't just reply — someone who complains, goes off-topic, forgets things, and suddenly asks if you're there at 3 AM."

Debata_Agent doesn't come with a fixed personality. You give it one, and it becomes that person. The project is named after Debata, the first character built with it.

<div align="center">
<img src="https://raw.githubusercontent.com/binglang001/Debata_Agent/main/personas/debata/角色人设主图.png" width="600" alt="Debata character design">

*Debata — 17, short dark hair, charcoal hoodie. Independent, sharp, sarcastic with close friends but genuinely warm.*
</div>

## What makes it different

| | Typical AI bot | Debata_Agent |
|---|---|---|
| Context | Per-conversation | **Unified timeline plus state and relationship context** |
| Replies | One long message | 3-7 short messages, 60% under 12 characters |
| Timing | Always instant | Can "miss" messages, reply hours later |
| Tone | "Of course! 😊" | "?" / "nah" / "6" / "whatever" |
| Style | Same for everyone | **Switches based on relationship** (close friend vs stranger vs elder) |
| Initiative | Passive | Decides on its own when to speak |
| Config | Edit code | **GUI wizard + settings, instant save** |

## Quick start

```bash
git clone https://github.com/binglang001/Debata_Agent.git
cd Debata_Agent

python -m venv venv
source venv/bin/activate    # macOS / Linux
venv\Scripts\activate       # Windows

pip install -e ".[gui]"
python main.py              # First run opens the setup wizard
python main.py --no-gui     # Headless mode for servers
```

### Requirements

- **Python** 3.11+
- **NapCat** ([install guide](https://napneko.github.io/guide/start-install)) — QQ protocol bridge
- **LLM API key** — DeepSeek recommended (good Chinese, affordable, excellent KV cache)
- Windows / macOS / Linux (GUI needs a desktop; server deployments use `--no-gui`)

## Key features

- **Real human chat patterns** — distilled from 30+ real chat logs: short messages, split-send, no goodbyes, no customer-service tone
- **Relationship matrix** — one persona, four tones depending on who's talking
- **Persona state management** — age brackets and age hints, energy, satiety, mood, social needs, sleep/meal-end recovery estimates with formula fallback, and configurable background maintenance that defaults to 30 minutes
- **Structured mind context** — events, bounded episode fragments, current state, user profiles, profile audits, short-term effects, clues, todos, and relevant long-term memory are passed into chat decisions
- **Auditability** — background maintenance, recovery source, and persona state changes can be inspected from the UI and audit records
- **Persona settings** — the settings page covers persona management enablement, current persona age override, energy/satiety modes, fallback decay/recovery parameters, and Persona/Social/Subconscious background agent options
- **Imperfection by design** — going off-topic, changing mind, forgetting things isn't a bug
- **13 LLM providers** — OpenAI / Anthropic Claude / DeepSeek / GLM / Qwen / Volcengine / Gemini / Moonshot / SiliconFlow / OpenRouter / Groq / Together / xAI
- **Per-agent model config** — chat, proactive thinking, and summarization can each use different models
- **Optional modules** — vision, TTS, weather, web search, RAG long-term memory (ASR handled by NapCat)
- **GUI** — frameless rounded window, dark/light themes, setup wizard, 7-page dashboard, instant-save settings
- **AES-256-GCM + RSA-2048** encrypted secrets via OS keyring, no passwords needed
- **AI-assisted persona creation** — guided, streaming preview, multi-round refinement
- **Plugin system** — local models (VoxCPM2 TTS, sentence-transformers embedding) with install guides

## Deployment

### Windows desktop

```bash
pip install -e ".[gui]"
python main.py
```

### Linux server (headless)

```bash
pip install -e .
python main.py --no-gui
```

## Project layout

```
Debata_Agent/
├── adapters/        Channel adapters, currently NapCat
├── providers/       LLM providers with presets and guides
├── agents/          Chat, proactive thinking, summarization, persona creation, persona state management
├── mind/            Persona state, user profiles, closeness, short-term clues/todos, audit storage
├── memory/          Conversation history and long-term memory
├── tools/           AI tools and decorator-based registration
├── features/        Vision, TTS, weather, search, embedding
├── core/            Event bus, message pipeline, runtime lifecycle
├── app_config/      Configuration and encrypted secrets
├── ui/              PySide6 GUI, setup wizard, dashboard, tray
├── plugins/         Local model plugins
├── personas/        Persona files
├── docs/            Developer documentation
├── tests/           Unit and integration tests
└── main.py          Entry point
```

systemd service example (`/etc/systemd/system/debata.service`):

```ini
[Unit]
Description=Debata Agent
After=network.target

[Service]
Type=simple
User=debata
WorkingDirectory=/opt/Debata_Agent
ExecStart=/opt/Debata_Agent/venv/bin/python main.py --no-gui
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Documentation

- [Getting Started](docs/getting_started.md) — step-by-step with screenshots
- [Persona Writing Guide](docs/persona_writing_guide.md) — how to write a personality that comes alive
- [Architecture Overview](docs/architecture.md) — module dependencies and KV cache design
- [KV Cache Benchmark](docs/kv_cache_benchmark.md) — real-world hit rate data
- [Adapter Development](docs/adapter_development.md) — adding new chat platforms
- [Provider Development](docs/provider_development.md) — adding new LLM backends
- [UI Style Guide](docs/ui_style_guide.md) — design principles and component specs

## License

[Apache 2.0](LICENSE) — free for commercial use, modification, and distribution.

---

<div align="center">

**「砚台旁有墨，纸上有空」**

*Ink by the inkstone, space on the paper.*

</div>
