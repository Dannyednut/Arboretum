# Part 17 — P3 Day-7 Acceptance Review
*(recreated 2026-09-15 after sandbox rollback lost the original; evidence intact in v3.db + worklog)*

## Verdict: **P3 PASS, with 2 conditions (C1, C2)**

P3 gate: "1 week fully automated paper trading without manual fixes" (spec §5, Part 15 tracker).

## Evidence (run day 11, pulled 2026-09-14)

| Criterion | Evidence | Result |
|---|---|---|
| Tripwire coverage | 2,261 log rows / 323 chains (11 PASS, 310 FAIL, 2 FAIL_HARD) | PASS |
| TW breakdown | TW2 195f, TW3 155f, TW4 96f, TW5 4f (incl 2 genuine Sep-14 catches), TW6 0f | PASS |
| Fill reconciliation | 16 paper_fills = exactly 4 opens + 4 closes x 2 legs | PASS |
| Planned-exit cost | 3/3 = 100% realized ≤ 1.3x assumed | PASS |
| P0 ledger drift | 13/14 = 93% ≤ 1.3x | PASS |
| Incl. emergency exit | 3/4 = 75% vs 80% bar | CONDITION C2 |
| Unwind completeness | position #4 left ~$318 + ~$113 hedge residual (USD-sized exits vs qty exposure) | CONDITION C1 |
| Automation | Sep-14 chain: TW5 probe → immediate unwind → venue freeze → re-catch → drift OVER → kill halt, ALL zero manual | PASS |

## Kill-switch correction
kill halt=TRUE since 04:08:33Z Sep 14 (`drift_over_15x` on #4's 1.556x sample). Verified `kill_eval` is a pure 24h-window function — sample auto-ages out after Sep 15 ~03:09Z. **Confirmed live: unhalted 03:34:55Z Sep 15, zero touch.**

## Conditions
- **C1**: fix unwind sizing to unit-qty completion before real capital (fixed Part 18/19)
- **C2**: emergency exits need a dedicated assumed-RT class, not the planned-exit 2x model (fixed Part 18/19)

## P4-real gates (unchanged)
1. C1+C2 fixes ✅ 2. User trade-only API keys ⬜ 3. BingX $100 withdrawal test ⬜
