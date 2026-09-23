# Part 15 — P3: M3 Execution Engine (Paper-Mode, Automated)

**STATUS: BUILT + LIVE (2026-09-03, first automated position open)**
Spec: Part 12 §3.3 / §5 P3. Deliverable: the execution engine as code —
entry styles per venue, leg sequencing, the 90-second legging cap, the
unwind ladder on depth rungs, and trigger monitors — all in paper mode,
every intended action logged as spec-schema `paper_fills` rows in v3.db.
Acceptance (§5): **1 week of fully automated paper trading without manual
fixes** — starts with this build; the engine requires no human fill
decisions, only session pings that run ticks.

## 1. What was built

`scripts/v3_exec.py` (~1,100 lines) — the M3 engine. Stateless processes,
stateful database: every tick restores the full machine state from v3.db
tables, advances it against freshly fetched live books, and persists before
returning. Any gap between ticks is safe by construction (see §3).

**Storage (v3.db, new tables):**

| Table | Role |
|---|---|
| `exec_positions` | engine-owned positions; status machine `seq_open → open → unwinding → closed` (+ `cancelled`); flags JSON holds stage, cost ledger, trigger streaks, unwind progress |
| `exec_orders` | order lifecycle (resting → filled / cancelled / expired), maker repost counts, limit price |
| `paper_fills` | the §3.3 spec schema exactly: `{ts, pair, leg, venue, side, style, target_qty, sim_fill_px, sim_slip_bps, fee_bps, funding_interval, notes}` |
| `exec_drift` | per closed cycle: assumed RT (2 × entry one-way) vs realized RT, ratio, verdict (OK ≤ 1.3×) |

**Entry sequencing (spec §3.3 default):** short (floor) leg posts maker at
the touch first — while it rests there is no legging exposure; on the
observed cross (`best_bid ≥ limit` for our sell), the long leg executes
taker immediately, and must fill within **90 s** (`pending_deadline` in
flags; retries each tick; past the cap it force-takes and writes an
`EXEC_LEGGING` event — the legging cap is a defense, not a strategy).
Taker-short pairs (INJ) run `tt`: both legs near-simultaneously. If the
entry maker never crosses within `MAKER_PATIENCE_S=600`, the order cancels
and the position records `cancelled` with an `EXEC_DEFER` event — no
conversion-to-taker on the *first* leg, because a resting order has no
legging risk to cap (the spec's taker-conversion rule is about the
unfilled *second* side).

**Maker realism:** a resting order is re-posted at the touch whenever the
market moves through it (repost count persisted, capped at 25), fills are
recognized only when a tick observes the book crossed through our price
(conservative — never invents fills), and a maker fill books slip 0 at the
limit price with the venue maker fee. Venue fee tables and median sources
are reused from `paper_ledger` so the engine and the P0 ledger price the
same trade identically.

**Unwind ladder (spec §3.3):** exits in tranches sized to the **10/25/50
bps depth rungs** of the exit-side book, recomputed at execution time
(cumulative-slice logic: each tranche may use everything still unwindable
within progressively wider rungs). Maker-first unwind on Nado/Aster legs
(resting close-side orders with `UNWIND_MAKER_PATIENCE_S=900` then taker
conversion); taker on CEX legs. **Event exits** (floor-param change,
delisting) may cross full size at market. If a book is thinner than 50 bps
for the remainder, the ladder waits up to 3 consecutive zero-progress
ticks, then takes one logged full-remain tranche (`thin-book extended
tranche`) — a strand-proof against stuck positions. Position closes when
both legs are flat; the realized-vs-assumed RT row lands in `exec_drift`.

**Trigger monitors (M4-lite until P4 splits them out; each isolated in
try/except so a broken monitor can never stall the engine):**

| Trigger | Check cadence | Fire condition | Exit mode |
|---|---|---|---|
| spread compression | ≥10 min apart | live spread < 20% of median, 3 consecutive | ladder |
| spread inverted | ≥10 min apart | live spread < 0, 2 consecutive | ladder |
| floor-param change | hourly (reuses TW5 probes + kv baselines) | any param change on a held leg | **immediate** |
| delisting/suspension | daily (Sharpe `listings/recent`) | lifecycle flag on held coin | **immediate** |
| funding decay | daily | 3-day OLS slope of interval-true spread from `funding_obs` < −2 APR pts/day on 2 consecutive checks | ladder |

**Pre-entry gate:** the full M2 chain (`v3_tripwires.run_chain`,
`pre_entry` mode) must return PASS before any sequence starts; venue
freeze flags (set by TW5 on param changes) also block entries outright.
Sizing applies the M5-lite hard clamp `min(target, 0.7 × pair_cap)` where
pair_cap = min of leg 25 bps entry-side capacities, measured at entry.

**Book telemetry:** every tick writes throttled (1/min per venue-coin)
`book_samples` rows with `source='exec_tick'` — the engine continuously
feeds the same swing-depth dataset TW4 consumes.

## 2. Validation: synthetic lifecycle test (all paths, no network)

`scripts/test_v3_exec_synthetic.py` drives the state machine against a
temp DB and fabricated books — the paths live ticks may wait days to hit:

| Path | Result |
|---|---|
| sm_lt: maker post at touch → book crosses → maker fill → long taker → `open` | PASS |
| entry cost = maker fee (short) + taker slip+fee (long), 0.150% | PASS |
| unwind: thin book → tranche 1 partial (~$200/leg) → deeper book → ladder completes → `closed` + drift row | PASS |
| drift row: realized 0.385% vs assumed 0.300%, ratio 1.283 ≤ 1.3 [OK] | PASS |
| entry patience: maker never crosses → cancel + `EXEC_DEFER` event | PASS |
| legging: short fills while long book dead → deadline set → force taker at restored book → `open` + `EXEC_LEGGING` event | PASS |

**Bug hunt (12 fixed during build/test, all pre-first-week):** inverted
cost-key conditional (would have mislabeled exit costs); entry-patience
wrongly converting to taker instead of defer; unwind makers had no
patience conversion (could rest forever); ladder zero-slice stranding on
sparse books; missing-book guards in `start_entry`/`take_taker`/
`maker_to_taker`/`unwind_tranche` (stale book snapshots); `books.get(k,
default)` returning stored `None`; repost counts not persisting; maker
fills missing the entry-cost contribution; immediate-exit tranche
condition never matching; double unwind-tranche per tick; book-sample
flooding (throttled); `vc._session` stale-pointer across ticks
("Session is closed" on the first chain of each tick — reset before
re-adoption).

## 3. Gap safety — how pings-driven sessions trade continuously

The desk runs on session pings; the engine is designed so that *no fill
decision ever needs a human*:

- state lives in v3.db, not memory — any process restart resumes exactly;
- resting maker orders are **never assumed filled across a gap**; the next
  tick evaluates the current book (understates fills between sessions —
  documented conservatism, never inflates performance);
- within a session, `run --minutes M --every S` polls continuously (20 s
  cadence default) so maker fills, the 90 s cap, and ladders progress in
  real time;
- every stage transition is persisted before the tick returns, so a
  killed process loses at most the current in-flight action, which the
  next tick re-derives from books.

Ops procedure going forward (P3 week): each session ping runs the ledger
cycle (as before) **plus** `python3 scripts/v3_exec.py run --minutes 20
--every 30`; `status`/`drift` dump state; `unwind --pos N --reason T`
exists as a logged override but normal operations never need it.

## 4. First live session (2026-09-03 ~10:26–10:41 UTC)

Nine ticks across three runs, all clean after the session-pointer fix.

- **INJ bingx→okx — engine pos #1 OPEN, fully automated:** chain PASS
  (all six tripwires; live spread 88.3% APR vs 1.3% Sharpe-model median),
  `tt` sequence fired both taker legs same tick: bingx sell 51.5358 @
  4.851 (slip 1.03 bps, fee 5 bps), okx buy 51.5358 @ 4.851 (slip 2.06,
  fee 5). Entry cost **0.131% one-way**; pair_cap $655k → 0.7× clamp not
  binding. Triggers (compression/floor/listings/decay) active and quiet
  on subsequent ticks.
- **XMR dydx→binance deferred** — TW3 inverted (−11% live vs 33.8%
  median) + TW4 dydx depth 55–63 bps vs 25 gate (depth re-swung midday;
  chain caught it both times).
- **BTW aster→bitget deferred** — TW2: Bitget live interval 8 h vs DB
  median 4 h. This is the *real* 09-01→09-02 interval change (Part 14
  catch #1) still gating until the DB median accumulates 8 h rows — the
  designed conservatism, not a false positive. TW3/4/5/6 all pass.
- **UAI aster→binance deferred** — TW3: live spread −15.3% (inverted at
  sampling time on the Sharpe current book).
- **LINK nado→okx deferred** — TW2 pos_frac 0.932 < 0.95 + TW3 ~0% live.
- 308 tripwire rows written today (the chain re-runs every tick per
  pair), 60 exec-tick book samples, 2 spec-schema paper_fills.

**Engine book vs P0 ledger:** deliberately parallel books (independent
duplicate guards) for the same 5 basket rows — the P0 ledger remains the
2–4 week manual-validation record; the engine book accumulates the
automated record. Week-1 comparison of the two books on shared states
(e.g. both open INJ) doubles as a cross-check of manual vs automated
fill quality.

## 5. P3 acceptance tracker

| Check | Status |
|---|---|
| Every engine entry accompanied by full tripwire_log row-set | ✅ by construction (chain-gated) |
| Every intended action → spec-schema paper_fills row | ✅ live (2 rows day one) |
| Unwind ladder + triggers exercised on a real exit | ⏳ waits for a trigger or manual-override-free cycle end |
| 7 days of ticks with zero manual fill decisions | ⏳ started 2026-09-03; check daily heartbeats + events |
| realized/assumed RT ≤ 1.3× on ≥80% of closed cycles (`drift`) | ⏳ needs closed cycles |

Interim review scheduled with the next ops pings: heartbeat continuity,
trigger-fire log, deferred-pair re-arms (BTW median catch-up is the first
expected un-gate).
