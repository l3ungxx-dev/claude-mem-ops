# Cost analysis (measured 2026-10-22, DeepSeek `deepseek-flash`)

Prices (official, 2026-10): peak hours (Mon–Fri 09–12, 14–18 CST) input ¥2/M (cache miss), output ¥8/M; off-peak half price. Cache-hit input ¥0.04/M.

## Consolidation only (this setup)

| Item | Value |
|---|---|
| Calls | 1/hour (hours with <15 observations are skipped by design) |
| Input per call | ~35k tokens (observation index + raw excerpts, 400k char budget) |
| Output per call | ~1–3k tokens (incl. reasoning) |
| Cost per call | ≈ **¥0.10** (peak) |
| **Daily total** | **≈ ¥1.5–2.5** |

Observed: one manual run cost ¥0.0074 with a nearly empty window; a full hourly window measured ≈¥0.098.

## Alternatives

| Approach | Quality | Daily cost | Local GPU |
|---|---|---|---|
| All-cloud observation + cloud consolidation | best | ¥16–20 | idle |
| **This repo (local index + cloud raw-text consolidation)** | good (raw detail preserved) | **¥1.5–2.5** | 58 °C / 80 W / 3.7 GB |
| Local 8B observation + cloud consolidation | good enough, details lost by local summarizing | ≈¥1 | 78 °C / 194 W / 11.5 GB |
| Local-only, no cloud | weak (no consolidation) | 0 | same |

The mixed setup saves ~¥1.6/day of electricity (194 W → 80 W) — roughly the entire cloud-bill of tier 2.
