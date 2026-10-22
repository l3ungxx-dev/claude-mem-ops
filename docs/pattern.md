# The cost-tiered memory pattern

A reusable pattern for **any** LLM application that processes high-volume agent/event logs. The
idea is simple: don't send everything through the expensive model, and don't let a cheap model
destroy evidence.

This repository is a reference implementation of the pattern for
[claude-mem](https://github.com/thedotmack/claude-mem); the pattern itself is host-agnostic.

## Problem shape

You have one event stream that mixes:

- **high-frequency, low-value items** (shell commands, file reads, job output) — 90%+ of volume;
- **low-frequency, high-value items** (edits, decisions, messages, incidents) — the part you
  actually want to remember.

Using one expensive model for everything means paying the expensive price for the 90% noise.
Using one cheap model for everything means losing detail on the 10% that matters.

## The four layers

| Layer | Store | Writer | Frequency | Cost |
|---|---|---|---|---|
| 1. Raw | append-only original records (tool calls, inputs/outputs) | host | every event | storage only |
| 2. Index | small records: title, one-liner, tags, key facts | small local model | every event (batched) | cheap |
| 3. Distillation | structured notes/decisions per time window | large cloud model | scheduled (hourly → daily → weekly → …) | moderate, bounded |
| 4. Solidified | hand-maintained / long-lived knowledge base | human + automation | on change | negligible |

## The four mechanisms

1. **Filtering at the source** — classify events and drop the low-value classes before any model
   runs (`bash`, `read`, management tools, …). Biggest single win (volume −80–95%).
2. **Tiered compute** — route by *value*, not by request: the small model only ever builds an
   index (its errors are recoverable), the large model only ever reasons over complete raw text.
3. **Deferred distillation** — keep raw text for weeks, distill on a schedule. Paying the large
   model is decoupled from event frequency; detail survives because the raw layer is untouched.
4. **Budget & routing** — record tokens/cost per call, cap daily spend, and route to a stronger
   provider only when a queue-depth threshold says you are falling behind (with automatic fall-back).

## When the pattern applies

- token-billed API or quota-limited local model;
- event stream with a long tail of low-value items;
- evaluation/audit matters more than sparkling prose (facts must be recoverable);
- you can tolerate minutes (not seconds) of latency for distilled knowledge.

It is **not** a fit for real-time user-facing replies (use prompt/semantic caching and request
routing there instead) — the tiers are complementary.

## Reference implementation (claude-mem mapping)

| Pattern layer/mechanism | claude-mem-ops component |
|---|---|
| 1. Raw | claude-mem `tool_uses` table, retention 30 days (`CLAUDE_MEM_TOOL_USES_RETENTION_DAYS`) |
| 2. Index | observer with small local model (`qwen2.5:3b` via `openai-compatible`), `SKIP_TOOLS` filter |
| 3. Distillation | `scripts/consolidate.py` hourly/daily/weekly/monthly/yearly, reading index + raw excerpts |
| 4. Solidified | project `docs/memory/` files (hand-maintained) |
| Filtering | `CLAUDE_MEM_SKIP_TOOLS` / `SKIP_BASH_PATTERNS` / `SKIP_SUBAGENT_OBSERVATIONS` |
| Tiered compute | small local observer + scheduled cloud consolidation |
| Deferred distillation | raw retention + systemd timer |
| Budget & routing | per-call token/cost accounting + `scripts/queue-cloud-router.py` |

## Porting to another system (checklist)

1. Find your **raw store** (does it retain full inputs/outputs? for how long?). If not, add one —
   this is the layer that makes the pattern safe.
2. Find your **index store** (what does the retrieval path read?). Give it a producer that runs
   cheaply and frequently; do not ask it for perfect fidelity.
3. Find your **scheduler** (cron/systemd/cloud scheduler) and add the distillation job. Start with
   one window (hourly), then roll up (daily/weekly).
4. Add **filtering** first. Measure event volume per class and drop the classes that never appear
   in a "what did we do yesterday?" answer.
5. Add **budget accounting** before adding more models: tokens in/out per call → daily total →
   warning threshold.
6. Only then add routing (tier models or switch providers) — routing without filtering just makes
   the noise cheaper.

## Pitfalls

- Do not let the cheap model be the only record of an expensive decision — keep raw text.
- Distillation windows must be **cumulative** (from last successful run, not "last hour")
  otherwise a skipped run silently loses events.
- Bound the distillation input (e.g. 400k chars) and degrade gracefully: shorten raw excerpts
  (600→300→150→80 chars) before dropping items entirely.
- Model/provider switches are usually **session-scoped**: in claude-mem the observer session caches
  the model id, so a config change only takes effect after a worker restart.
