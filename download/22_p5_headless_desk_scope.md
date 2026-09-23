# Part 22 — P5 Track: Headless Arbitrage Desk (+ Extensions)

Date: 2026-09-21 · Follows: Part 21 (VOOI competitive scan)
Decision record (user, this session):

> Vision = build the VOOI Arbitrage Desk equivalent, headless (no frontend yet),
> plus extended features. Decisions: start NOW in parallel with P4b · surface =
> CLI + JSON · first extension = spot-perp basis (extensions also include venue
> expansion + anything arbitrage research surfaces, each with its own
> optimization + risk management) · desk can OPEN positions, gated.

## §1 Track map

| Phase | Scope | Needs keys? | Status |
|---|---|---|---|
| **P5a** | `v3_desk.py` read surface: `scan` / `show` / `history` | no (public APIs) | **this part** |
| **P5b** | `desk open` — gated on-demand entry reusing engine tripwires + P4a seam; paper/DRY_RUN until P4b lands, live requires YES_REAL + EXEC_MODE=live | no (paper) / yes (live) | next |
| **P5c** | Extension 1: spot-perp basis scanner (spot long + perp short where basis beats perp-perp spread) | no | **shipped (s.8)** |
| **P5d** | Extension 2: venue expansion (Lighter / HL HIP-3 / Extended public endpoints) | no | queued |
| — | Cross-cutting per user remark: every extension ships with its own optimization + risk gates (TW-style checks, sizing caps) | — | rule |

P4b (signed REST execution) stays blocked on user keys and continues in
parallel via ops windows; P5 does not gate on it.

## §2 What already exists vs what P5a adds

Exists (Part 21 gap map): scanner candidates (`apr/rate/interval_h/oi_usd`),
90d+ funding history (`funding_obs`, 1.58M rows in v3.db), book-walk (`ds.walk`),
entry RT model, TW1–TW6 + merit gates, P4a seam, Sharpe aggregator connector
(`arb_table`, `history`, `settlement`), `pl.sharpe_current` per-coin live map.

P5a adds the **surfacing layer**: one CLI that publishes the strategy table and
per-strategy deep rows VOOI shows in their UI, computed with OUR fee/RT model
and OUR median-APR merit signal — plus artifacts for later consumption.

## §3 P5a design

### Data sources (all key-free)
1. `vc.Sharpe.arb_table()` — broad cross-venue combo feed (coin, long/short
   venue, gross `apr`, `netApr`, OI per leg, executable depth, exec status)
2. `pl.sharpe_current([coin])` + `vc.get_funding(venue, coin)` — live per-leg
   funding (rate, interval_h, next_time); native wins over Sharpe
3. `vc.Sharpe.history(coin, days)` — per-venue rate series → Max F 1h/24h
4. `pl.get_book(venue, coin)` + `ds.walk` — mid, P-spread, P-spread @ size
5. `pl.load_medians()` — 30d median APR per known combo (merit context)
6. `pl.TAKER/pl.MAKER` — our fee tables (our venues only)

### Math (shared, pure, tested)
- `hourly = rate / interval_h`; `apr = hourly × 24 × 365` (cross-interval safe:
  aster 1h vs binance 8h normalize before diffing)
- combo `f_apr = apr(short) − apr(long)`; `f_spread_8h = (h_s − h_l) × 8`
- entry RT taker/taker `rt_tt = TAKER[short] + TAKER[long]` (+maker variant)
- `net_ours_30d = gross_apr − rt × 365/30` (our fees on their gross — no
  double-count vs Sharpe's netApr; both columns shown)
- P-spread = `(bid_short − ask_long) / ask_long`; @size walks both legs at
  `--size-usd`, also emits per-leg slip bps and `rt@size`

### Commands
```
python3 scripts/v3_desk.py scan [--top 25] [--min-apr 0.10] [--min-oi 100000]
    [--venues a,b] [--exclude COIN,COIN] [--ours-only] [--json PATH]
python3 scripts/v3_desk.py show COIN [--size-usd 250] [--json PATH]
python3 scripts/v3_desk.py history COIN [--days 30] [--csv] [--json PATH]
```
Artifacts → `download/data/desk/` (`scan_latest.json`, `show_<COIN>_latest.json`,
`history_<COIN>.json|.csv`). Printed table mirrors the JSON for IM reporting.

### Honest-data rules
- Venue without book fetcher or native funding → row shows `null` for that
  field, never a guess
- History-based columns carry the window they were computed over
- Sharpe feed staleness header (`asof`, `stale`) propagated into every artifact
- Desk rows NEVER auto-execute; P5b's `open` re-runs the full engine gate chain

## §4 Incident record: rollback #3 + hardening (same session)

- `download/data/v3.db` and the 90d JSON cache wiped by another sandbox
  rollback; v3.db recovered from the /tmp mirror (integrity ok, tripwire_log
  2492, kv 85, funding_obs 1.58M intact) — but the mirror predated window 23
- Position #5 (INJ bingx->okx OPEN) reconstructed from worklog T37 facts via
  `scripts/recover_pos5.py` (rows marked `reconstructed`); fills back to 18
- **Hardening**: `scripts/db_backup.py` — state-only gzip dumps (funding_obs /
  book_samples row-skipped, schema kept) → git-tracked `download/db_backup/`,
  83KB vs 171MB raw, round-trip verified. Runbook: backup at every window end
- Lesson generalized: durable = git-tracked; everything else is a cache

## §5 Acceptance (P5a)

1. `scan` renders ≥15 combos from live Sharpe feed with filters applied; JSON
   artifact schema-stable
2. `show INJ` returns all funded venue pairs for INJ with live rates, next
   funding (native venues), P-spread + P-spread@size for book venues, Max F
   24h from history API
3. `history INJ --days 7` exports per-venue series + stats, CSV round-trips
4. `test_desk.py` network-free green: combo math, fee drag, filters, bucket
   join, P-spread math, artifact schema; full regression suite stays green
5. No engine behavior change (read-only module; engine untouched)

## §6 P5b — desk open (shipped same part)

### Design: pin, never bypass

`desk open COIN [--short v] [--long v] [--size-usd N] [--style taker|maker]
[--minutes M] [--mode paper|live] [--no-run]` is a **selector**, not an
execution path:

1. `gather_show` (shared with `show`) builds live combos for the coin
2. `resolve_combo` picks the best **executable** our-venue combo by
   net30_tt — executable = positive apr + known RT + live books on both
   legs (`pspread` present is the both-books proxy; explicit
   `--short/--long` may override, preflight still vets it)
3. Read-only preflight: evaluated kill halt (hard refuse), stale kill state
   (warn only — engine re-evaluates at window start), venue freezes from kv
   (conservative refuse even if expired-uncleared), pair active/cooldown
   from exec DB, funding/book presence, apr > 0
4. Pin file `download/data/desk/pin.json` (schema 1, TTL 24h, provenance:
   apr/f8h/net30tt/pspread@size/maxF24h + preflight warnings) — then engine
   window spawns with `--pin-desk` (or `--no-run` just prints the command)
5. Engine: `engine_pairs()` prepends the validated pin (dedupes a colliding
   static pair, pin wins on size/style), revalidates EVERY tick (TTL, venue
   coverage, ≤$500/leg engine cap); a rejected pin demotes to static pairs,
   never crashes. Position flags carry `"src": "desk_pin"`; a pin-sourced
   open emits `DESK_PIN_ENTRY`.

The pin only ADDS a candidate. Kill, cooldown, freeze, TW1–TW6, M5 sizing,
pair-cap clamp all run unchanged — desk refusals are fast operator feedback,
not the safety mechanism. Live real orders stay unreachable until P4b
(adapter DRY_RUN refusal armed; `--mode live` = same as ops windows).

### Acceptance (P5b)

- `test_desk_open.py` 74/74 network-free: pin validation (schema/TTL
  boundary/venue/size/style), pin→pair mapping, executability-filtered
  resolution, preflight refusals incl. fail-safe missing-ctx, file
  round-trip, engine load_pin (missing/stale/malformed), engine_pairs
  prepend+dedupe, wiring source checks, mocked cmd_open e2e incl.
  stale-kill-warns and refused→no-pin
- Live: `open INJ` auto-resolved the best both-books combo (bingx->dydx
  23.5% apr) after correctly refusing bookless hl->bybit; warned no-median
  + stale-kill; pin written; 4-min `--pin-desk` paper window: pin banner
  every tick, **TW2 FAIL on the pinned combo (pos_day_frac 0.51 < 0.95)
  → no entry — gate chain refused a desk-pinned trade, exactly as designed**
- Full regression green: desk 61, adapter 33, fixqueue 33, settings 24,
  portfolio 32, synthetic, py_compile

## §7 Incident (same session): status-casing corruption → duplicate open

- Rollback-recovery script `recover_pos5.py` had written pos #5 with
  `status='OPEN'` (uppercase); the engine reads lowercase only → pos #5 was
  invisible to `active_positions` for 5 days: no triggers monitored it and
  the pair looked free
- Discovered during the P5b acceptance window: the engine re-opened the
  pair as pos #6 (INJ bingx->okx, 32.58/leg @7.674/7.672, entry RT 0.113%
  one-way) through a fully PASSing gate chain — the chain is correct; the
  data behind the pair-occupancy check was not
- Remediation: pos #5 status repaired to `open` (+ audit note in flags);
  pos #6 unwound via the standard engine path with the reason on record
  (exit slips 1.96/1.20 bps, fees 5 bps ×4); round-trip net −$0.43 (price
  P&L ~flat — delta-neutrality held); freeze TTLs + kill re-eval behaved
  correctly during the same window (bitget/aster freezes expired zero-touch)
- Lessons: (1) recovery scripts must write canonical vocabularies — casing
  is load-bearing; (2) pair-occupancy integrity now belongs on the window
  checklist (`SELECT status, COUNT(*) GROUP BY status` sanity: statuses ⊆
  {seq_open, open, unwinding, cancelled, closed}); (3) backup runbook
  executed post-remediation (v3_state 89KB gz)

## §8 P5c — spot-perp basis scanner (shipped 2026-09-21)

User direction this session: P4b keys deferred (user pastes them in their own
environment later); DRY_RUN stays armed; proceed to P5c and beyond.

### What shipped

`python3 scripts/v3_desk.py basis [--min-fapr 0.10] [--min-basis X]
[--venues v] [--spot-venues v] [--exclude C] [--size-usd 250]
[--max-coins 25] [--scan-keep 12] [--top 20] [--json basis_latest.json]`

Cash-and-carry surface: BUY spot (binance/bybit/okx/bitget, one full-venue
bookTicker call each), SHORT the perp on an our-venue perp where funding is
positive. Two-pass honest-data design:

- **Pass 1 (cheap)**: one `Sharpe.current()` call ranks (coin, perp venue)
  pairs by funding APR; one spot ticker call per spot venue joins listing +
  best ask. Rows ranked by `net30_incl_basis`.
- **Pass 2 (authoritative)**: survivors get NATIVE funding (native wins over
  Sharpe), real perp + spot books -> `basis_entry` recomputed from executable
  best bid/ask (`px_src: books`), `basis_at_size` from walking both books at
  `--size-usd`, per-leg slip bps vs true mid, `min_f1h/min_f24h` from Sharpe
  history (SHORT leg wants funding to STAY positive -> MIN is the flip-risk
  stat, mirror of max_f for perp-perp), and `pp_best_net30` = best executable
  our-venue perp-perp net30 for the same coin (the "does basis beat
  perp-perp" comparison the phase was scoped for).

### Math (extends Part 22 §3 conventions)

- `basis_entry = (perp_bid - spot_ask) / spot_ask` (one-time, locked at entry)
- `rt = 2 x (spot_taker + perp_taker)`; spot fees = flat 10 bps taker
  (conservative, no VIP/BNB discounts)
- `net30 = funding_apr - rt x 365/30` (fees only); `basis_apr = basis x
  365/30` (convergence-over-hold ASSUMPTION, documented in every artifact);
  `net30_incl_basis = net30 + basis_apr`; `breakeven_d = rt / (h x 24)`

### Risk flags (read-only gates; every extension ships its own per scope rule)

`funding_not_positive` - `negative_basis` (you pay the premium) -
`fee_unknown` - `px_divergence_Npct` (|perp/spot-1| > 2%) -
`funding_flip_24h` (min 24h funding <= 0). No execution path exists: the
engine has no spot leg; basis EXECUTION is logged as P5c-b and must ship its
own gate chain + sizing caps (scope rule above).

### Live acceptance + what it taught

- `basis --min-fapr 0.10`: 44 pass-1 rows (21 coins) -> 12 kept. All top rows
  were a REAL dydx hourly-perp long-squeeze (ALGO 1228% / POL 1002% / DYDX
  743% funding APR, native-confirmed) with `minF24h ~= 0` + flip flags ->
  surfaced as transient, not a 30d carry. `pp30` empty = quiet perp-perp
  market, consistent with `scan`.
- Mark-lag discovery: Sharpe mark priced ALGO basis at -0.15% while real
  books showed +1.46% (perp premium during the squeeze) -> books now beat
  the mark for entry pricing (`px_src`).
- Context dedup: (coin, perp venue) funding/books/history fetched ONCE and
  shared across spot venues; native recompute can no longer disagree between
  two rows of the same perp leg.

### Tests

`test_desk_basis.py` 71/71 network-free: basis_math (cross-interval APR, fee
RT, one-time basis, breakeven, None propagation), basis_row honest Nones +
all flags, min-window flip stat, filters, symbol candidates (1000-prefix
both directions), 4 ticker parsers (USDT-only, junk->{}), 4 book parsers
(zero-qty levels dropped), spot_book symbol fallback, mocked cmd_basis e2e
(native recompute, books-wins, per-key dedup, artifact schema).
Full regression: desk 61 + desk_open 74 + adapter 33 + fixqueue 33 +
settings 24 + portfolio 32 + synthetic + py_compile.

### Session incidents (restore before build)

Rollback #4 wiped the uncommitted Task 39-41 artifacts (Part 21/22 docs,
v3_desk.py, tests) AND the working v3.db. /tmp mirror + git-tracked
db_backup gzips made the restore surgical: state from
`v3_state_20260921T0502.sql.gz` (pos #5 open, pos #6 closed, 22 fills) +
1.58M funding_obs history rows re-attached from the mirror DB
(`scripts/restore_state_merge.py`, reusable). Lesson repeated: commit at
every milestone; durable = git-tracked.
