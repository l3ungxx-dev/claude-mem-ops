# Pitfalls found while building this

1. **Switching the observer model needs a worker restart.** `settings.json` edits and even `POST /api/settings` do not switch the model for already-running observer sessions (the session caches `lastModelId`). Always: edit config → `systemctl --user restart claude-mem`. Keep the file and the runtime state consistent.
2. **Never widen the context window before switching provider.** If you raise `CLAUDE_MEM_OBSERVER_CONTEXT_WINDOW` (e.g. 128k) while still on a local Ollama model (40960 ctx), batches get oversized and the local stream breaks (`Ollama stream broke off`), stalling the queue. Switch provider first, confirm cloud calls in the log, then raise batching.
3. **`POST /api/settings` does not write API keys.** Sensitive keys are silently ignored; keys come from `~/.claude-mem/.env` (`OPENAI_COMPAT_API_KEY`).
4. **Batch merging is effectively single-item on `openai-compatible`.** Upstream issue #4967. `CLAUDE_MEM_CLAUDE_OBSERVATION_BATCH_SIZE` does not help the openai-compatible path; a busy machine will backlog. Use volume reduction and/or the pressure valve.
5. **Thinking models are bad observers.** `qwen3:4b` emitted 3,351–4,096 output tokens per observation and got truncated / dropped batches. `qwen2.5:3b` (non-thinking) is stable. If you must use a thinking model, disable thinking explicitly.
6. **Observation types drift across versions.** e.g. `gotcha` is a *concept*, not a type, in 14.x/15.x. Listing it in `CLAUDE_MEM_CONTEXT_OBSERVATION_TYPES` produces thousands of config warnings per day and the id is silently discarded.
7. **A worker restart discards in-memory backlog.** Switching models/versions with a large backlog loses the queued, not-yet-generated events. Accept it (old noise) or drain first.
8. **Raw retention matters.** Keep `CLAUDE_MEM_TOOL_USES_RETENTION_DAYS` ≥ 30 so consolidation can still read details; observation indexes alone are lossy.
