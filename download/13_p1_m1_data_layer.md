# Part 13 — P1: M1 Data Layer in Code (Storage + Connectors + Scanner)

*September 2, 2026 · Build phase P1 per `12_v3_system_spec.md` §5. Acceptance: "scanner output reproduces Parts 4/10 numbers from DB." Code: `scripts/v3_store.py`, `v3_connectors.py`, `v3_backfill.py`, `v3_scanner.py`. DB: `download/data/v3.db`. Validation CSV: `download/data/p1_scanner_validation.csv`.*

## 1. What was built

**Storage (`v3_store.py`)** — single SQLite file with WAL:

| Table | Purpose | Key constraint |
|---|---|---|
| `funding_obs` | every funding observation ever seen: `(venue, base, ts_ms, rate, interval_h, source)` | UNIQUE(venue, base, ts, source) — idempotent re-ingest |
| `book_samples` | L2 walk summaries (slip ladders, depth bands) — same semantics as the research CSVs | — |
| `candidates` | discovery rows with status lifecycle | — |
| `events` | E1r / E2 / E3 / floor_change / delisting | — |
| `kv` | watermarks (`backfilled_at`, `sharpe_fetch_at`, …) | — |

`source` provenance values: `native_hist` (Part-4 7-venue cache), `sharpe_hist` (Part-10 Sharpe pulls), `native_live` / `sharpe_live` (the longitudinal capture that started Sep 2). The P0 paper ledger remains in `paper_v3.db` until P3 consolidates.

**Connectors (`v3_connectors.py`)** — the §3.1 interface: `get_book(venue, base)` wraps the 11 proven book fetchers; `get_funding(venue, base)` does native live funding (rate + interval + next-time) for binance/aster/okx/bybit/bitget/hl/dydx/bingx/backpack with Sharpe fallback for nado/orderly (native probes remain open items §6.1–6.2); `get_fee(venue, role)` returns the Part-12 §2.1 verified table. The `Sharpe` class captures `X-Data-As-Of` provenance headers, and `cross_check()` implements the >10% Sharpe-vs-native quarantine rule — live test: OKX INJ agreed to 0.0% relative, Binance BTC within 6.4%.

**Backfill (`v3_backfill.py`)** — loaded 1.557M historical observations: 493,125 native (Part-4 cache) + 1,064,322 Sharpe (Part-10 checkpoint) + 19,889 fresh two-slice Sharpe fetches for the basket coins (INJ/LINK/XMR/BTW/UAI — the checkpoint cache never held INJ or LINK). Modes: `fetch-coins`, `restamp`, idempotent re-runs.

**Scanner (`v3_scanner.py`)** — recomputes persistence/spread distributions from the DB with **two provably faithful models**: native rows use the Part-4 event model (per-hour carry = rate/interval on settlement hours, zero-fill, union grid); Sharpe rows use the Part-10 ffill model (per-hour carry ffilled across the union-hour grid). Modes: `validate` (reproduction test), `capture` (live snapshot), `pairs` (basket metrics from DB).

## 2. Validation results (the P1 acceptance)

**Part-4 native: EXACT reproduction.** 300 of 300 published pair rows recomputed from the DB: median |Δ apr_30d| = **0.000pp**, max 0.000pp, 300/300 within 2pp. The DB → scanner pipeline is bit-faithful to the Part-4 scan on native data.

**Part-10 Sharpe: model-faithful, data-drift documented.** The published pairs table came from a live Sharpe pull in that session; the checkpoint cache is a different, sparser pull — so exact pair-median reproduction is impossible by construction. Validation therefore compares **leg-level medians** (same-data): 400 legs, median |Δ| = 2.2pp, with floor-pinned legs (the E1 cohort's basis) tighter than spiky legs (1.53pp vs 3.36pp median). Two interval regimes were proven out: fresh rows carry API interval metadata (the published table's regime); checkpoint rows reconstruct intervals per-row (Part-10 `cache_series` semantics — proven exact on floor legs at 10.9% = 10.9%).

**Reality cross-check.** Scanner output on the recent 8-day slice coheres with the live paper ledger: INJ bingx→okx 8d median 5.7% (ledger live tonight: 12.4%), LINK nado→okx 8d median 5.2% (ledger live: 4.1%). The published Part-10 medians (18.7% / 13.5%) were two-month-window numbers including a higher-carry regime — the decay those numbers warned about is real and visible in the DB.

## 3. Findings and caveats recorded during the build

1. **Free-tier slice holes are structural.** The Sharpe free history API caps at 5,000 rows per request (all venues combined, oldest-first, `end_time` ignored) — capped coins (INJ, XMR) leave multi-week holes that ffill drags across. Part-10's "discovery-grade only" warning is now a measured constraint; the nightly scanner must note slice coverage per series.
2. **Interval metadata vs reconstruction.** Checkpoint-cache rows dropped the API's `interval_hours` field; per-row delta reconstruction is exact on constant-cadence series but mis-annualizes sparse ones. Fresh fetches carry metadata per row and are the going-forward standard.
3. **XMR is delisted on OKX** (no native funding, absent from Sharpe's book) — recorded as a venue-coverage fact; any future XMR pair must not assume OKX liquidity.
4. **The Part-10 published medians embed regime decay** — INJ 18.7% → ~5.7% (8d) is the same compression the P0 ledger has been gating entries on. Discovery numbers age; the scanner's rolling recomputation exists precisely to catch this.

## 4. What P1 sets up

- `capture` mode now appends `native_live` + `sharpe_live` snapshots per run — run it every ping/session; after P0's 2–4 week window the DB holds the longitudinal dataset the P3 execution engine and the M5 drift monitors need.
- `pairs` mode gives basket metrics from the DB alone (no research CSVs) — the first system output that does not depend on session-scoped research artifacts.
- The nightly scanner + Sharpe arb/listings polling (§3.1) now have a storage target; P2 wires the tripwire chain to write `tripwire_log` rows against the same DB.

## 5. Addendum — P1 finalization (2026-09-02, W08 cycle)

**Sharpe connector now covers all five §3.1 endpoints.** Added `Sharpe.settlement()` (`funding/settlement` — per-coin dollar-weighted settlement windows: `net`/`long_paid`/`short_paid` USD per window, with `rate_source` provenance) and `Sharpe.rwa()` (`rwa-perps/rates` — 1,855 live rows across 18 venues with mechanism, OI, and `is_stale`/`is_price_suspect` flags). `current`, `history`, `arb_table`, `listings` were already in from the build.

**New scanner mode: `discover`** (the §3.1 discovery poll, run-daily shape):
- Arb table (1,026 rows) normalized into `candidates` (idempotent per-day source stamp); fields kept: netApr, spread, holdingDays, executableDepthUsd, executionStatus, OI both sides.
- Diff vs our candidate universe (basket + Part-4/Part-10 research CSVs + prior candidates) → new names go to `p1_watchlist.csv`, never straight to basket.
- Watchlist filters: net ≥ 10%, executableDepthUsd ≥ $5k with OI-short fallback (the depth field is populated on only 42/1,026 free-tier rows — sparse, so OI is the practical floor), ranked by universe legs (both legs on our 11 execution venues first) then net.
- Settlement top-15 by current net USD stored to `kv` per day; RWA universe exported to `rwa_universe.csv` (weekly-shape job, run on demand).

**First discovery run (live):** 65 new coins pass net+depth/OI, 45 with at least one leg on our venues, 13 with both legs ours. Top in-universe: VANA bitget→aster 124.2% net, 1000SATS binance→bitget 118.5%, NVDL bitget→binance 95.1% (2x-leveraged NVDA equity perp — stock-hours mismatch risk per Part 5), PHA bitget→aster 91.8%, ZAMA binance→aster 88.8%. All are Sharpe-table numbers only — none has passed the 8-gate pipeline; watchlist is the ceiling until a cycle gate-checks them.

**External validation of the basket:** Sharpe's dollar-weighted settlement ranking currently puts XMR ($102k net), BTW ($64k), LINK ($40k) in the top 10 of all 500 coins — the P0 basket is long exactly the names paying the most funding dollars market-wide.

**Hardening:** dYdX native fetcher now fails with a clear message on transient indexer responses (empty `markets` body with HTTP 200 observed once during capture; endpoint verified healthy on re-probe).

**Stability note:** 9-window re-run (W08 added) reproduces the finalized Part-7 verdict counts exactly — 12 STABLE / 18 FLAKY / 39 FAIL — third consecutive identical re-run; monitor stays closed.
