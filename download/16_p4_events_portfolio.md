# Part 16 — P4: M4 Event Monitors + M5 Portfolio Layer (rebuild)

> Rebuilt 2026-09-08 after the Sep-4 environment rollback lost the original
> build (Task 24). Same design, tested with 32/32 synthetic checks.

## Scope (spec Part 12 §3.4 / §3.5)

**M4 — `scripts/v3_events.py`** (log-only; engine consumes outputs)

| Monitor | Cadence | What it does |
|---|---|---|
| `e1r` | daily | cap-slam sweep, sign-only + names-only. Kraken bulk verified (273 syms); HTX / Extended / Crypto.com / Variational best-effort graceful-down. Output: basket bases with negative E1r-venue funding (magnitude pending spec open item 4). |
| `listings` | daily | E2: new-listing diff vs baseline (14d floor-pin watch window, the BTW pattern). E3: held-leg coverage probe via Sharpe funding — zero coverage or both held venues missing → `E3_HELD_ALERT`. |
| `floor` | hourly | standalone TW5-style param probes across ALL native param venues (aster/binance/dydx/hl/backpack) × their listed basket bases; change → `venue_freeze_{venue}` kv + `FLOOR_PARAM_CHANGE` event (= kill source #1). |
| `health` | daily | tripwire fail rates split held vs pre-entry; settlement top10 (kv); held-leg funding staleness (>36h); Sharpe-vs-native disagreement baseline (kill source #3 reference). |

**M5 — `scripts/v3_portfolio.py`**

| Function | Behaviour |
|---|---|
| `m5_gate` | kill file → sizing (0.7× pair_cap incumbent, 0.5× new pairs <14d) → venue caps across BOTH books (tier1 $20k / tier2 $5k) → correlation ≤2 open pairs same long coin → RWA/commodity bucket ≤30% deployed → leverage ≤2.5× $1k equity. |
| `kill_eval` | 4 spec sources → `download/data/kill_switch.json`: (1) floor-param change 24h, (2) ≥3 held TW4 depth fails 24h, (3) Sharpe-vs-native disagreement spike (>3× baseline median or >0.50 abs APR), (4) drift ratio >1.5× 24h. |
| `kill_halt` | fail-safe read: missing / stale (>6h) / corrupt → HALT. |
| `trickle` | hourly per pair per book: funding collected − (RT fee+slip) amortized 14d → `pnl_log`. |
| `weekly` | realized-vs-model 7d rollup → `download/data/pnl_weekly.csv`. |
| `state` | snapshot → `portfolio_snap` (equity/deployed/leverage/venue exposure/kill state). |

**Engine wiring (`v3_exec.py`)**: `tick()` re-evaluates the kill file when
stale and blocks ALL new entries on halt; `start_entry()` runs the full
`m5_gate` before `new_position()` (M2 chain still required first).

## Tests

`scripts/test_v3_portfolio.py` — 32/32 PASS (kill fail-safe states, all 4
kill sources hit/no-hit, gate sizing clamps, venue/correlation/RWA/
leverage denies, trickle exact math + cadence guard, both-book aggregation).

## Live verification (2026-09-08)

- kill_eval: halt=False, disagreement gap 0.086 vs baseline 0.150
- state: equity $1000, deployed $1050, leverage 1.05x
- trickle: 3 rows (engine UAI + P0 INJ + P0 UAI)
- tick: kill check clean, chain gating unchanged, M5 notes on entry path
- E1r catch: RUNE/STX/TRUMP/TRX/UNI/XMR/ZEC negative on Kraken

## $1k real deployment — STAGED (unchanged)

Identical code paths; requires user trade-only API keys (env vars) +
BingX $100 withdrawal test. 30-day P4 clock starts at the real flip.
