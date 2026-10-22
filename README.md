# claude-mem-ops

**Cost-efficient hybrid memory pipeline for [claude-mem](https://github.com/thedotmack/claude-mem).**

claude-mem records what your coding agents do. This repo makes it cheap to run **every day** with a local model while keeping the *detail* of what happened:

```
┌─────────────────────────── local, cheap ───────────────────────────┐
│  Observer (e.g. qwen2.5:3b via Ollama)                             │
│    • records ONLY decisions / edits (skips bash, read, job_*)      │
│    • produces lightweight observation index — no need to summarize  │
│  Raw tool calls + full text are kept in claude-mem's `tool_uses`     │
│  table (retention 30 days) — nothing is lost                        │
└─────────────────────────────────────────────────────────────────────┘
                              │ hourly (systemd timer)
                              ▼
┌─────────────────────────── cloud, smart ───────────────────────────┐
│  consolidate.py → DeepSeek (or any OpenAI-compatible API)          │
│    • input = observation index + linked raw tool-call content       │
│    • emits hourly / daily / weekly / monthly / yearly notes         │
│    • per-call token & cost accounting, daily budget warning         │
└─────────────────────────────────────────────────────────────────────┘
```

## Measured results (2026-10-22, one workstation)

| Metric | Before | After |
|---|---|---|
| Observation throughput | 153 per 5 min (costly source) | ~10 per 5 min, only edits/decisions |
| Observer backlog (`queueDepth`) | 800–1500, growing | 0–20 |
| Observer GPU | 76–79 °C / 187–199 W / 11.5 GB VRAM (8B) | 58 °C / 80 W / 3.7 GB (3B) |
| Cloud cost (consolidation only) | — | **≈ ¥0.10 / run, ¥1.5–2.5 / day** |
| Full-cloud observation alternative | — | ¥16–20 / day |
| Detail retention | local model summarization loses numbers | raw text kept 30 days + cloud reads it |

## Why this exists

- claude-mem's `openai-compatible` observer path sends **one tool call per turn** and cannot batch (tracked upstream: [#4967](https://github.com/thedotmack/claude-mem/issues/4967)). With a local model the queue grows all day.
- Watching every tool call produces a **logbook**, not memory. Skipping `bash`/`read`/management tools leaves only changes and decisions.
- A small local model is fine for indexing but **loses details when summarizing**. Keep the raw text and let a strong cloud model consolidate from raw data on a timer.

## Minimal setup vs optional add-ons

**You do not need everything in this repo.** The daily value comes from four things:

### Core four — all you need for cheap, accurate daily memory

| # | What | How | Effort / hardware |
|---|---|---|---|
| 1 | Skip low-value tools (`bash`, `read`, management tools) | settings only | 5 min / zero |
| 2 | Small local observer model (index only, e.g. `qwen2.5:3b`) | settings only | 4 GB VRAM or CPU |
| 3 | Keep raw tool-call text 30 days | settings only | zero |
| 4 | Scheduled cloud consolidation **from raw text** | `consolidate.py` + systemd timer | 20 min / API key |

If you only do #1 and #3, you already remove the logbook problem and keep the evidence.
#4 turns evidence into conclusions; #2 makes retrieval/injection work without paying for cloud observation.

### Optional add-ons — only install if the scenario applies

| Add-on | When it helps | Skip it if |
|---|---|---|
| `queue-cloud-router.py` (pressure valve) | extreme backlog (`queueDepth` ≥ 1000) on a busy multi-session machine | your backlog is normally near zero after step 1 |
| `compact-knowledge.py` + `knowledge_rules.py` | you keep a hand-maintained semantic doc and want a ≤250-line guard | you do not maintain such a file (most users) |
| `patch-dsh-work-state.py` | you use the DeepSeek Harness (DSH) integration and want `work_state_*` tools | you use Claude Code / Codex / other hosts |

> TL;DR: install the **core four**; everything else is situational.

## Quick start

Requirements: Linux with systemd `--user`, Python ≥ 3.10, claude-mem ≥ 15.0.1, (optional) DeepSeek/OpenAI-compatible API key.

### 1. Recommended claude-mem settings

Copy the relevant entries from [`config/settings.recommended.json`](config/settings.recommended.json) into `~/.claude-mem/settings.json` (stop the worker first, then restart it):

```bash
systemctl --user stop claude-mem.service
# edit ~/.claude-mem/settings.json
systemctl --user start claude-mem.service
```

Key entries: `CLAUDE_MEM_SKIP_TOOLS` (do not observe bash/read/management tools),
`CLAUDE_MEM_CLAUDE_OBSERVATION_BATCH_SIZE`, `CLAUDE_MEM_MAX_CONCURRENT_AGENTS`,
`CLAUDE_MEM_TOOL_USES_RETENTION_DAYS=30`.

### 2. Install scripts

```bash
./install.sh            # copies scripts to ~/.local/share/claude-mem-ops and installs systemd units
# edit ~/.config/claude-mem-ops/env  (API key, model, paths)
systemctl --user daemon-reload
systemctl --user enable --now claude-mem-consolidate.timer
# optional: backlog pressure valve
systemctl --user enable --now claude-mem-cloud-router.timer
```

### 3. DSH users only

```bash
python3 scripts/patch-dsh-work-state.py    # expose work_state_write/read tools in the DSH integration
systemctl --user restart dsh-web           # required: the patch is a plugin bundle edit
```

## Scripts

| Script | Core? | Purpose |
|---|---|---|
| `scripts/consolidate.py` | ✅ core | Layered consolidation (hourly→yearly) **from raw tool-call text**, with token/cost accounting |
| `scripts/queue-cloud-router.py` | optional | When `queueDepth` ≥ threshold, temporarily route the observer to a cloud model; switch back when drained or when the daily budget is spent |
| `scripts/patch-dsh-work-state.py` | optional (DSH) | Idempotent patch for `@claude-mem/dsh` adding `work_state_write/read` tools |
| `scripts/compact-knowledge.py`, `knowledge_rules.py` | optional | Semantic-doc guard (keeps a hand-maintained knowledge file ≤ 250 lines); not part of the memory pipeline |

## Environment variables

| Variable | Default | Used by |
|---|---|---|
| `MEMORY_ROOT` | `<repo>/docs/memory` | consolidate, compact-knowledge |
| `CLAUDE_MEM_DB` | `~/.claude-mem/claude-mem.db` | consolidate |
| `CONSOLIDATE_API_BASE` | `https://api.deepseek.com/v1` | consolidate |
| `DEEPSEEK_API_KEY` | from `~/.claude-mem/consolidate.env` | consolidate |
| `CLOUD_ROUTER_MONITOR` | `<repo>/docs/memory/.monitor/cloud-router.md` | queue-cloud-router |

## Notes & pitfalls

See [`docs/pitfalls.md`](docs/pitfalls.md). Highlights:

- Switching the observer model **requires a worker restart** (the session caches `lastModelId`; settings edits / `POST /api/settings` are not enough).
- Never widen the context window before switching provider — a local Ollama model (40960 ctx) may choke on oversized batches (`stream broke off`). Switch provider first, confirm, then raise batching.
- `POST /api/settings` silently ignores API-key writes; keys live in `~/.claude-mem/.env`.
- Scripts read claude-mem's SQLite schema (`observations`, `tool_uses`) and HTTP endpoints (`/api/settings`, `/api/processing-status`); pin to a tested claude-mem version.

## Provenance

This is an **independent implementation**, not affiliated with claude-mem or its author.

It was developed and measured on a single workstation on **2026-10-22** (times CST / UTC):

| Milestone | CST | UTC |
|---|---|---|
| consolidation script refactored from local helpers | 16:10 | 08:10 |
| README written | 16:25 | 08:25 |
| repository first published | 16:26:36 | 08:26:36 |
| upstream comments posted (#4967 / #4963) | 16:26:56 / 16:27:00 | 08:26:56 / 08:27:00 |

At publication time, GitHub code search (`CLAUDE_MEM_SKIP_TOOLS`, `claude-mem consolidation`,
`claude-mem queueDepth`), repository search, and the claude-mem issue tracker showed **no public
implementation of the same approach**. The closest items were direction discussions
([#4963](https://github.com/thedotmack/claude-mem/issues/4963) tier routing,
[#4967](https://github.com/thedotmack/claude-mem/issues/4967) batching) and unrelated memory tools.
Any later similar work is independent parallel development.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with claude-mem.
