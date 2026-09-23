# Part 12 — v3 System Build Spec (Consolidated)

*September 2, 2026 · This document consolidates Parts 1–11 into the single build specification for the v3 carry-trading system. Everything numeric below is **measured**, not assumed: fees from venue-native fields and tier tables (Part 11), slippage from walk-the-book ladders (Parts 5/7/11), persistence from 60–90 day distributions (Parts 4/10), floor parameters from venue-native config endpoints (Parts 9–11). Research-first constraint holds: nothing here trades real capital until the paper phases pass.*

---

## 1. Mission, scope, non-goals

**Mission.** A delta-neutral funding-carry system that: discovers cross-venue funding spreads (discovery layer), verifies them against live books and native venue data (verification layer), enters and unwinds with per-venue execution styles (execution layer), reacts to funding-regime events (event layer), and manages the whole book under explicit capacity and risk caps (portfolio layer). Capital path: **paper → $1k pilot → $10k** (scale decision gated on 30-day live-paper statistics).

**In scope:** E1 floor-pinned DEX carries (crypto + RWA/commodity/stock perps), dYdX maker shorts on wide books, floor-majors parking, E1r negative-funding cap events, E2/E3 listing-driven funding episodes.

**Non-goals (explicit exclusions from Part 8, unchanged):** spot arbitrage, quarterly futures basis, directional bets, chasing >100% live APR snapshots without persistence evidence, any strategy whose economics depend on unverified third-party data.

## 2. Verified parameter baseline

### 2.1 Fee table (taker/maker, verified)

| Venue | Taker | Maker | Source of truth |
|---|---|---|---|
| Aster | 4.0bps | **0.0%** | Part 2 docs |
| Hyperliquid | 4.5bps | tier | Part 2 docs |
| Binance / OKX | 5.0bps | tier | docs |
| dYdX | 5.0bps | tier | docs |
| Bybit | 5.5bps | tier | docs |
| Bitget | 6.0bps | tier | docs |
| **BingX** | 5.0bps | 2.0bps | published VIP0 (Part 11) |
| **Nado** | **3.5bps** | **1.0bps** | **native** `symbols` map field (Part 11) |
| **Orderly** | ~3.0bps | ~0/rebate | Orderly docs (broker-configurable — confirm at key signup) |
| **Backpack** | **9.5bps** | 8.5bps | tier table (needs app-level confirmation) |

### 2.2 Gate parameters (pre-trade, hard)

| Gate | Threshold | Origin |
|---|---|---|
| Entry/exit slippage | ≤25bps per leg per side at target size (walk-the-book, median of 3 passes) | Part 5 |
| Depth | min 4-side depth-within-25bps ≥ target notional | Part 5 (fixed in Part 7) |
| Net APR | net@size > 5% after measured fees + slippage | Part 5 |
| Persistence | pos_day_frac ≥ 0.95 for E1 basket rows; ≥0.80 for watchlist | Part 4 |
| **Live-spread rule (new)** | reject entry if live spread < 40% of the 60d median | Part 11 divergence board |
| Identity guard | same base asset on both legs; venue marks within 5% | Part 3 |
| Stability (multi-session) | swing_1k (max/median RT cost) < 2× across windows; any window FAIL → pair out (FLAKY) | Part 7 |
| Staleness | any data row with `is_stale=true` or age > SLA → quarantine | Part 3 + Sharpe headers |

### 2.3 Funding-interval normalization (mandatory math rule)

All persistence and P&L math on a **union-hour grid with per-hour carry ffill**, converting each venue's raw rate as `rate / interval_hours` before annualization (`× 24 × 365`). Never multiply raw 4h/8h rates by hourly counts — that is the over-count trap that produced the fake 87.6% Binance floor in Part 1.

## 3. Architecture — five components

```
                    ┌──────────────────────────────────────────┐
                    │  M1 DATA LAYER                           │
                    │  connectors: 11 venues + Sharpe          │
                    │  stores: funding_hist, book_samples,     │
                    │          candidates, events              │
                    └──────────────┬───────────────────────────┘
                                   ▼
                    ┌──────────────────────────────────────────┐
                    │  M2 TRIPWIRES (pre-trade chain)          │
                    │  identity → persistence → live spread →  │
                    │  depth re-walk → floor monitor           │
                    └──────────────┬───────────────────────────┘
                                   ▼
                    ┌──────────────────────────────────────────┐
   M4 EVENTS ──────▶│  M3 EXECUTION                            │
   (E1r/E2/E3,     │  entry styles per venue; unwind ladder;  │
    delisting)      │  paper ledger / live adapter             │
                    └──────────────┬───────────────────────────┘
                                   ▼
                    ┌──────────────────────────────────────────┐
                    │  M5 PORTFOLIO LAYER                      │
                    │  sizing, venue caps, kill-switches, P&L  │
                    └──────────────────────────────────────────┘
```

### 3.1 M1 — Data layer

**Direct connectors (execution-grade, book + funding):** Binance, OKX, Bybit, Bitget, Hyperliquid, Aster, dYdX (WS book), BingX, Backpack, Nado (gateway typed queries), Orderly (public funding/info now; book after D1 key). All fetchers already exist as working code from Parts 5/7/11 — v3 wraps them in one interface: `get_book(venue, base) -> (bids, asks)`, `get_funding(venue, base) -> (rate, interval_h, next_time)`, `get_fee(venue, role) -> bps`.

**Sharpe aggregator (discovery-grade):** one connector polling (a) `funding/rates?type=current` every 15 min — new-coin and new-venue triage; (b) `arbitrage/cross-exchange` daily — diff against our candidate set, new names go to watchlist (never straight to basket); (c) `listings/recent` daily — E2/E3 triggers; (d) `funding/settlement` daily — dollar-weighted ranking refresh; (e) `rwa-perps/rates` weekly — RWA universe sync. Provenance headers (`X-Data-As-Of`, `is_stale`) stored per row and enforced by the staleness gate. Sharpe rate vs venue-native rate disagreement >10% → row quarantined (the Part-10 Gate lesson).

**Persistence scanner:** nightly job recomputing, per (venue, coin): pos_frac, exact-pin fraction, median/mean interval-true APR, p10/p90, OLS slope over rolling 30d/60d/90d; pair spreads on the union-hour grid. Sources: our own history first, Sharpe history as the independent second opinion and cross-validation target (87–99% near-agreement achieved in Part 10 — re-run monthly as drift check).

**Storage:** SQLite (single file, no infra): tables `funding_obs(venue, base, ts, rate, interval_h, source)`, `book_samples(...)` (same columns as the CSVs), `candidates`, `events`, `paper_fills`, `tripwire_log`. CSV exports kept for research reproducibility.

### 3.2 M2 — Tripwire chain (runs before every entry, in order)

1. **Identity guard:** base-asset equality, both-venue marks within 5%, contract margining type recorded (BitMEX inverse lesson).
2. **Persistence gate:** pair row exists in scanner output with required pos_frac; funding interval for each leg re-fetched live (venues change intervals silently).
3. **Live-spread rule:** current interval-true spread ≥ 40% of 60d median, else defer (Part 11: 3 of 4 new pairs inverted at sampling time).
4. **Depth re-walk:** walk both books at target size, both sides (entry + projected unwind), 25bps gate + min-depth; swing vs recent windows < 2×.
5. **Floor monitor:** for floor-pinned legs, fetch venue funding params natively (Aster exchangeInfo; Orderly `/v1/public/info` → `cap_funding`/`floor_funding`/`interest_rate` — captured in Part 11; Backpack `fundingRates` history; HL/dYdX per-market config). Any parameter change on a held or entering leg → freeze entries in that venue, alert.
6. **Counterparty tier check:** position size vs per-venue exposure cap (M5).

Every tripwire evaluation writes an immutable row to `tripwire_log` (pass/fail + measured values) — this is the dataset that later proves/disproves the gates.

### 3.3 M3 — Execution

**Entry styles per venue:**
- **Maker-default venues:** Nado (1.0bp maker, tightest book measured), Aster (0% maker for short entries when spread > 15bps — the XMR/dYdX pattern generalized), dYdX shorts on wide books (post-only at touch, chase N rungs).
- **Taker-default venues:** OKX, Binance, BingX (deep books, slip < 8bps through $25k measured), Bybit, Bitget.
- **Leg sequencing:** long leg first if it is the thinner/maker leg is wrong — enter the leg with the *tighter* book first when spread risk dominates, the *floor leg* (short) first when timing risk dominates; default = short (floor) leg maker, long leg taker, then both resting.
- **Max entry window:** both legs must be filled within 90s or the unfilled side converts to taker (legging-risk cap from Part 8).

**Unwind ladder:** exits in tranches sized to the 10/25/50bps depth rungs measured per leg (never cross the full size at market unless E1r event exit). Maker-first unwind on Nado/Aster, taker on CEX legs. Trigger events: funding regime decay (slope < −2 APR pts/day for 3 days), spread compression below cost-of-carry threshold, floor-parameter change (immediate), delisting alert (immediate, Sharpe `listings`), E1r mean-reversion target hit.

**Paper-fill ledger (P0, the 2–4 week validation):** every intended action logged as `{ts, pair, leg, venue, side, style, target_qty, sim_fill_px, sim_slip_bps, fee_bps, funding_interval, notes}`; nightly job compares simulated RT cost vs the gate's assumption and appends to a drift table. Acceptance: realized RT cost ≤ 1.3× assumed on ≥80% of fills.

**Live adapter (P3+):** same interface as the paper adapter; API keys held in env vars; hard order-size clamp = min(target, 0.7 × pair_cap); daily kill-switch state file consulted before every order.

### 3.4 M4 — Event monitors

- **E1r cap-slam sweep (daily, native):** sign-only scan of live funding on Kraken (v3 tickers), Crypto.com, HTX, Variational, Extended for legs we can short elsewhere; magnitude unverified → names-only output, position only after native history check resolves the unit question.
- **E2/E3 listing monitor (daily, Sharpe):** new listing → watch its funding for floor-pinning within 14 days (the BTW pattern: listing → floor pin → carry window); suspension/delisting lifecycle flags on any **held** leg → immediate unwind ladder.
- **Floor-parameter watch (hourly, native):** Aster/HL/Orderly/Backpack floor params diffed; change = kill-switch event for that venue.
- **Crowding/staleness watch (daily, Sharpe):** crowding-risk chart feed + settlement dollar-flow; plus our own `tripwire_log` health (rising failure rates = system-level warning).

### 3.5 M5 — Portfolio layer

- **Sizing:** position ≤ 0.7 × pair_cap (pair_cap = min of leg 25bps capacities at entry time); new pairs start at 0.5× for the first 14 days.
- **Margin:** 30–50% buffer above maintenance on both legs; total account leverage ≤ 2.5×.
- **Venue exposure caps:** BingX ≤ $5k (counterparty tier pending withdrawal test); any venue outside trust-tier-1 (Binance/OKX/Bybit/dYdX/HL/Aster) ≤ $5k until a $100 test withdrawal passes; Orderly/Backpack/Nado counts as tier-2.
- **Correlation:** no more than 2 pairs sharing the same long leg; commodity/RWA rows (XAG/CL/TSLA) treated as one macro bucket ≤ 30% of deployed capital.
- **Kill-switches (any one halts new entries globally):** floor param change on any basket venue; 3 tripwire depth failures in 24h; Sharpe-vs-native disagreement spike; paper-drift ratio > 1.5× over a day.
- **P&L accounting:** hourly trickle accrual per pair (funding collected − fees amortized over expected hold − slippage amortized), weekly realized-vs-model report. The identity of this desk is the vending machine: smooth accrual, small unwind costs, capacity ceiling as the moat.

## 4. Initial paper basket (rows entering P0)

| Tier | Pair | Net model | Status |
|---|---|---|---|
| $10k | TAO dYdX→Binance | 28.6% worst-window | pending W04–W06 stability windows |
| $10k | INJ BingX→OKX | 13.8% @10k | **gated, Part 11** — live-aligned at sampling |
| $10k | LINK Nado→OKX | 10.3% @10k | **gated** — enter only on live-spread rule pass |
| $1k | XMR dYdX(maker)→Binance | 72–87% maker variant | incumbent (Parts 5–7) |
| $1k | BTW Aster→Bitget | 63% basket est. | incumbent |
| $1k | INJ / LINK rows | 14.1% / 11.1% @1k | **gated, Part 11** |
| S3 parking | floor majors (BNB/LTC on Aster/HL/Backpack/Orderly/KuCoin/WhiteBIT/BitMEX) | 5–8% net | no gate needed beyond floor check |
| Watchlist | XAG Orderly→dYdX (17.5%), CL, TSLA, AAVE Backpack (book watch), PENG/PENGU Nado, E1r names | — | blocked on Orderly key / book recovery / native feeds |

## 5. Build plan and acceptance criteria

| Phase | Deliverable | Acceptance |
|---|---|---|
| **P0** (now) | paper ledger + manual execution of the $1k rows; nightly drift job | 2–4 weeks of fills; realized/assumed RT ≤ 1.3× on 80% |
| **P1** | M1 storage + nightly scanner + Sharpe connector in code | scanner output reproduces Parts 4/10 numbers from DB |
| **P2** | M2 tripwire chain automated (log-only, no orders) | every P0 paper entry accompanied by a full tripwire_log row |
| **P3** | M3 execution engine paper-mode (simulated fills driven by live books) | 1 week of fully automated paper trading without manual fixes |
| **P4** | M4 events + M5 portfolio live with $1k real capital | 30 days: no kill-switch breach, trickle P&L within ±30% of model |
| **P5** | scale to $10k | only after P4 passes and Part-7 finalization confirms basket stability |

## 6. Open items (each blocks a specific thing)

1. **Orderly D1 key** → unlocks XAG/CL/TSLA native depth gates (highest-net unverified candidate: XAG 17.5% net30, 99% pos days).
2. **Nado indexer probe** → native funding for LINK/PENG legs (removes Sharpe dependency for a basket row).
3. **Backpack fee confirmation** + book recovery watch → AAVE row decision.
4. **Kraken funding unit resolution** (native `historicalFunding` on 2–3 names) → unblocks E1r magnitudes.
5. **dYdX 42-day history fix** (cursor pagination) → closes the Part-7 caveat on dYdX leg stats.
6. **Crypto.com v2 authed API** → resolves the last pending Part-10 verification row.
7. **BingX $100 withdrawal test** → moves BingX counterparty cap from precaution to evidence.

## 7. Configuration draft (v3 `config.yaml`)

```yaml
capital_usd: 1000            # phase-gated: 1000 -> 10000
sizing: {pair_cap_mult: 0.7, new_pair_mult: 0.5, max_leverage: 2.5,
         margin_buffer: [0.30, 0.50]}
gates: {slip_bps_side: 25, depth_bps: 25, net_apr_min: 0.05,
        live_spread_frac_of_median: 0.40, swing_max_mult: 2.0,
        pos_day_frac_min: 0.95, mark_dev_max: 0.05, staleness_max_s: 3600}
venues:
  binance: {taker: 5.0, maker: 2.0, tier: 1}
  okx:     {taker: 5.0, maker: 2.0, tier: 1}
  bybit:   {taker: 5.5, maker: 2.0, tier: 1}
  bitget:  {taker: 6.0, maker: 2.0, tier: 1}
  hl:      {taker: 4.5, maker: 1.5, tier: 1}
  aster:   {taker: 4.0, maker: 0.0, tier: 1}
  dydx:    {taker: 5.0, maker: 2.0, tier: 1}
  bingx:   {taker: 5.0, maker: 2.0, tier: 2, exposure_cap_usd: 5000}
  nado:    {taker: 3.5, maker: 1.0, tier: 2, exposure_cap_usd: 5000,
            style: {default: maker}}
  backpack:{taker: 9.5, maker: 8.5, tier: 2, exposure_cap_usd: 5000,
            fee_confirmed: false}
  orderly: {taker: 3.0, maker: 0.0, tier: 2, book_access: gated,
            exposure_cap_usd: 5000}
sharpe: {base: "https://www.sharpe.ai/api", poll_current_min: 15,
         poll_arb_daily: true, poll_listings_daily: true,
         staleness_enforced: true}
events: {e1r_venues: [kraken, crypto_com, htx, variational, extended],
         delisting_alert: true, floor_param_watch_h: 1}
```

## 8. What the spec does NOT yet decide

Deliberately deferred to post-P0 evidence: exact unwind-tranche sizes (need the paper ledger's realized rung data), BingX trust-tier promotion (needs the withdrawal test), Orderly execution style (needs the key), and any E2/E3 position sizing (strategy stays paper-only until one full event cycle is logged). The spec's job is to make those decisions mechanical when the data arrives.
