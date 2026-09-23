# Part 14 — P2: M2 Tripwire Chain (Automated, Log-Only)

**STATUS: BUILT + ACCEPTANCE IN PROGRESS (2026-09-03, W09 cycle)**
Spec: Part 12 §3.2 / §5 P2. Deliverable: the six-tripwire chain as code, every
evaluation writing immutable rows to `v3.db tripwire_log`. No orders.

## 1. What was built

`scripts/v3_tripwires.py` — the M2 chain, run in order, short-circuit only on
TW1 (identity is non-negotiable); every tripwire logs pass/fail/skip + measured
JSON; a CHAIN row records the verdict.

| # | Tripwire | Implementation | Gate |
|---|---|---|---|
| TW1 | identity guard | both books fetched, mark dev, margining type recorded per venue | dev ≤ 5% |
| TW2 | persistence | pair metrics from v3.db (native event model, ffill fallback) + **live funding interval per leg vs DB median** | pos_frac ≥ 0.95, intervals match |
| TW3 | live spread | live interval-true spread APR vs 60d median from DB (ffill model; native 30d fallback) | ≥ 40% of median |
| TW4 | depth re-walk | 4 walks (entry+unwind, both legs) at target size + swing vs recent windows from `book_samples` | slip ≤ 25bps/side, filled, swing < 2× |
| TW5 | floor monitor | native funding params per venue; change-detect vs `kv` baseline → venue freeze | any param change = FAIL |
| TW6 | counterparty | existing exposure (paper ledger, both legs) + size vs per-venue cap | tier-2 $5k, tier-1 $20k |

Storage: `tripwire_log` table added to v3.db (ts, coin, legs, size, mode,
tw, result, measured-JSON). `book_samples` backfilled from the 9-window L2
CSVs (2,594 rows) so TW4's swing check works from the DB.

Modes: `all` (5 basket pairs, pre-entry), `one COIN SV LV USD`, `held`
(re-checks open positions), `backfill-books`.

## 2. Wiring into the paper ledger (the acceptance path)

`paper_ledger.py open` now invokes the full chain (step 3.5) after its legacy
inline gates and before any position INSERT: **verdict != PASS → no entry**.
From P2 onward every paper entry is accompanied by a complete tripwire_log
row-set — that is the §5 P2 acceptance criterion, satisfied going forward.
(The two currently open positions predate the wiring; `held` mode covers them
as health checks.)

Session lifecycle hardening that made this safe: `vc.init()` now adopts a
caller-managed live aiohttp session instead of creating a second one, and
`vc.close()` only closes self-owned sessions — the chain can run inside the
ledger's event loop without killing its transport.

## 3. First full run — results and two real catches (2026-09-03 ~04:50 UTC)

| Pair | Verdict | Blocking tripwire(s) |
|---|---|---|
| XMR dydx→binance | FAIL | TW3 (spread −11% APR, inverted) + TW4 (127bps) |
| BTW aster→bitget | FAIL | **TW2 (interval change)** |
| UAI aster→binance | FAIL | TW3 (0% live this hour) |
| INJ bingx→okx | **PASS** | — |
| LINK nado→okx | FAIL | TW2 (pos 0.932 < 0.95) + TW3 (0%) |

**Catch 1 — Bitget changed BTW funding interval 4h → 8h** between 09-01
08:00 and 09-02 19:20 UTC (native rows confirm; Sharpe metadata still says
4h — its known lag). TW2's live-vs-DB-interval check caught it on the first
full run; logged as `events` row `funding_interval_change`. Materially: BTW
passed ALL legacy inline gates and would have opened — the chain blocked it
because the model must re-baseline before capital goes in. This is the
first demonstrable P2 catch.

**Catch 2 — XMR depth swung 20.1bps → 127.3bps within ~10 minutes** (two
chain runs at 04:44 then 04:53 UTC), after failing at 167.8bps in the 19:50
W08 sweep. Confirms the Part-7 episodic-depth verdict for XMR: the swing
gate (<2× recent median) and per-run re-walks are the only defensible
defense; no standing size.

Held checks: INJ full PASS (spread re-expanded to ~97% live APR — strong
accrual hours ahead). UAI FAIL on TW3 only (0% spread this settlement hour —
informational for held legs: earns ~0 now; exit decisions remain the drift
job's job).

## 4. P0/P1 evidence state after this cycle

- Drift: **9/9 samples ≤ 1.3× (100%)**, worst 1.17× (UAI), 0 kill flags.
- Ledger: INJ 26h held ($0.019 accrued, live 2.5%→recovering), UAI 13h
  ($0.064 accrued, spread compressed ~9h).
- Funding capture: +252 obs (longitudinal dataset at ~1,000 live obs).
- 9-window stability: 12/18/39 verdicts identical (third convergence).

## 5. What P2 sets up

- The `tripwire_log` table is now the single evidence dataset for gate
  performance ("the dataset that later proves/disproves the gates" per
  spec §3.2) — P3's execution engine consumes the same chain as a hard
  pre-trade require.
- `venue_freeze_*` kv keys are the M5 kill-switch hooks: TW5 writes them,
  P3/P4 read them.
- TW5's param coverage: Aster `fundingInfo` (cap/floor/interval/interest —
  the flagship floor-pinned venue, both basket short legs), Binance
  fundingInfo, dYdX `defaultFundingRate1H`, Backpack bounds/interval, HL
  ctx (record-only). Orderly stays auth-gated (open item). Probe outage on
  a param-bearing venue = unverifiable = FAIL (conservative by design).

## 6. Next: P3 (per spec §5)

M3 execution engine in paper-mode — simulated fills driven by live books,
unwind ladder, consuming M2 as a hard pre-trade gate and writing
`paper_fills`-equivalent rows. Acceptance: 1 week of fully automated paper
trading without manual fixes. Prerequisite note: P0's 2–4 week manual
evidence window continues in parallel and is unaffected.
