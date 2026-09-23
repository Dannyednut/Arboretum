# Part 18 — P3 Fix Queue Implementation
*(recreated 2026-09-15 after sandbox rollback lost the original implementation AND this doc; code re-implemented same day — see Part 19 §0 for the rollback story)*

## Scope
Part 17 conditions C1+C2 plus the observability gaps found on Sep 14 (Tasks 30-31).

## Items implemented

### C1 — unwind unit-qty sizing (v3_exec.py)
- `start_unwind` snapshots `entry_leg_qty` per leg from `paper_fills` (pos-scoped via notes prefix, entry side only)
- `unwind_tranche` sizes tranches qty-first: `remain_q * _px_covering(levels, remain_q)` (worst covering VWAP, may exceed size_usd when price rose) + post-walk clamp on taker and maker branches
- all 3 exit fill sites (tranche taker, crossed maker, maker→taker conversion) accumulate `unwind_done_qty`
- `check_unwind_complete` completes on unit qty (≥98% both legs) with legacy USD ≥95% fallback when no snapshot

### C2 — emergency cost class (v3_exec.py + v3_portfolio.py)
- immediate closes: `assumed = max(2x entry_cost, EMERGENCY_ASSUME_RT_PCT=0.30%)`
- `exec_drift.detail` JSON tagged `class: immediate|planned`; `EXEC_CLOSE` event carries `[cls]`
- kill source `drift_over_15x` counts planned only: `IFNULL(json_extract(detail,'$.class'),'planned')='planned'` — legacy rows = planned, age out naturally

### fix3 — freeze TTL (v3_exec.py)
- `freeze_active(con, venue)`: parses both KV formats (epoch float, `frozen <ISO>: ...`), auto-expires after `FREEZE_TTL_S=86400`, deletes KV + emits `EXEC_FREEZE_EXPIRE`; unparseable = stay frozen (fail-safe)
- **wired into the entry path** (v3_exec.py entry check) — replacing the raw `kv_get` that left freezes stale forever (first implementation wired only the test, not the call site — caught live in window 22, see Part 19 §0)

### fix4 — exec-side event emission (v3_store.py, v3_tripwires.py, v3_events.py)
- shared `st.ev()` writer; `tw5_floor` emits `FLOOR_PARAM_CHANGE {old,new,path:"exec-side"}` so kill source #1 sees held-probe catches; M4 watcher tags `path:"m4-floor-watch"`

### fix5 — held-probe persistence (v3_exec.py)
- `trigger_floor` persists held probes to `tripwire_log` (`mode=held_check, tw=TW5_floor_monitor`, full measured incl old/new)

## Tests (scripts/test_fixqueue.py) — 33/33
C1: exact 100u/leg exit zero residual, ladder multi-tick completion, legacy fallback, px_covering, no-overfill clamp · C2: floor/class/kill exclusion both directions + legacy=planned · fix3: TTL 4 formats + fail-safe + entry wiring (source-level check) · fix4: emission + freeze set · fix5: persistence + immediate unwind flag
Plus: `test_v3_portfolio.py` 32/32, `py_compile` all modules.

## Live verification (2026-09-15 04:41Z, real tick on v3.db)
```
freeze aster: TTL 24h reached, cleared
freeze bitget: TTL 24h reached, cleared
EXEC_FREEZE_EXPIRE events: 2026-09-15T04:41:33Z aster, bitget
```
Stale Sep-14 freezes (the exact failure that exposed the rollback) expired zero-touch.
