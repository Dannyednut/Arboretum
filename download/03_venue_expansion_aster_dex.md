# Part 3 — Venue Expansion Study: Aster, DEX Integration & the Wider Opportunity Space

*September 2026 · Research-first phase · Companion to `market_study_all_arbs_2026-09.md` (Parts 1) and `02_system_blueprint.md` (Part 2). All numbers from live public APIs unless noted.*

---

## 0. Executive summary

We expanded the venue map from 5 to **8 live-integrated venues** — adding **Aster (542 perp markets), dYdX v4 (99), Bitget (768)** on top of Binance/Bybit/OKX/Gate/Hyperliquid — giving **3,576 perp instruments** and **942 clean cross-venue funding-differential pairs** after identity and data-quality guards.

**Five headline findings:**

1. **Aster replicates Hyperliquid's structural funding floor — on 2× the markets.** 338 of Aster's 542 contracts settle funding **hourly**, and 225 of them sat at *exactly* the interest-rate floor (0.0001% per 8h-equivalent = **+10.95% APR to shorts**) in our snapshot. The core "short-DEX-floor / long-CEX" edge now has **two structural venues** (HL 177 markets + Aster 338 hourly markets), with Aster charging **0.04% taker / 0% maker** — cheaper than HL's 0.045%.
2. **New vein confirmed: stock & commodity perps on Aster.** 24/7 stock perps (SK Hynix, Samsung, SBET), **pre-IPO SpaceX (SPCX)**, and commodities (XAU gold, XAG silver, CL crude) produce cross-venue funding premiums of **20–215% APR gross** (e.g., SKHYNIX aster→gate 214.9% gross / 212.8% net@30d; XAU aster→bybit 37.5% gross / 35.2% net@30d). This extends Family B beyond crypto equities.
3. **dYdX v4 is structurally a free-financing venue**: 71/99 markets have genuinely zero funding (hourly settlement), the 28 nonzero average ~+18% APR, and BTC/SOL markets have had **zero maker+taker fee holidays**. It is the cheapest *long-leg* financing we have found.
4. **A data-quality landmine found and neutralized**: Gate and Aster report `rate = 0` on hundreds of contracts where 0 means *no data*, not *true zero* — 294 pairs with such a leg as the long side were **quarantined** to prevent phantom spreads (prior study's Gate numbers need this caveat retroactively).
5. **DEX-vs-CEX structural bias is measurable and event-shaped**: 154/564 overlapping coins have DEX-side funding richer than CEX-side median; extreme examples (AI +775% APR diff, CRO +372%) are **DEX-listing premium events** — a new recurring sub-family of Family E.

**Deliverables this round:** `scripts/snapshot_v3.py`, `scripts/analyze_v3.py`, `download/data/funding_spreads_expanded_2026-09.csv` (1,236 rows incl. quarantine flags), this note. **Also delivered: Option 3 — the four live-blocking v2 bug patches** (§10).

---

## 1. The expanded venue map

| Venue | Type | Perps | Funding intervals | Taker / Maker | API style | Key facts |
|---|---|---|---|---|---|---|
| Binance | CEX | 839 | 3×1h, 425×4h, 411×8h | 0.050% / ~0.018% | REST HMAC | median fAPR +10.95% (base-rate floor) |
| Bybit | CEX | 744 | 4h/8h (+4×1h) | 0.055% / 0.020% | REST HMAC | xStocks spot = equity hedge leg |
| OKX | CEX | 80 sampled | 8h | 0.050% / 0.020% | REST HMAC | lowest median fAPR of CEXs (+5.5%) |
| Gate | CEX | 950 | 8h reported; **378 zero-rate = unknown** | 0.050% / 0.020% | REST | funding history auth-gated; treat zeros as missing |
| Hyperliquid | perp DEX | 177 | 1h | 0.045% / 0.015% | REST + wallet-sign | +10.95%/yr structural floor (established in Part 1) |
| **Aster** | perp DEX | **542** | **338×1h, 63×4h, 141×8h** | **0.040% / 0%** | **Binance-compatible REST + API wallet** | floor replicated empirically (§2.3); crypto+stocks+commodities |
| **dYdX v4** | perp DEX | **99** | **1h** | 0.050% / 0.020% (BTC/SOL fee holidays) | indexer REST + gRPC | 71/99 markets true-zero funding |
| **Bitget** | CEX | **768** | 3×1h, 372×4h, 393×8h | 0.060% / 0.020% (from API) | REST HMAC | `isRWA` flag identifies stock perps |

Watchlist (not yet probeable from this sandbox): **Paradex** (0% retail maker+taker, PFOF monetization — MMs pay 0.5bps; options + perps; JWT-gated API), **EdgeX** (cheapest-fee claims), **Lighter** (zk orderbook, points), **Ostium** (FX/commodities perps — direct competitor to Aster's commodity niche), Vertex. Perp DEXs took ~26% of global derivatives volume in 2026 (> $1T/month) — this space keeps minting new arb surfaces as venues compete on fees and listings.

The **interval regime change** from Part 1 now applies everywhere: 1h/4h/8h coexist on every venue, so annualization must use each contract's own interval (`APR = rate × 24/interval_h × 365`). Our analyzer does this per contract.

---

## 2. Aster deep dive

### 2.1 What it is

Aster (asterdex.com) is a privacy-focused perp DEX with BNB-Chain roots, backed by YZi Labs, running three execution modes: **Pro** (central limit order book — the mode relevant to us), **Classic** (AMM), and **1001x** (up-to-1001× leverage positions countereyed by the ALP pool, separate fee/funding schedule). It lists crypto perps, **24/7 stock perps** (launched Jul 2025), **commodities**, and USD1-denominated RWA pairs (Aug 2026: SpaceX, crude oil, gold, SanDisk, SK Hynix). Since Jun 2026 it accepts **tokenized stocks as perp collateral**. Its API (`github.com/asterdex/api-docs`) is a near-clone of Binance USDT-M futures REST.

### 2.2 Fees

- **Perps (Pro): 0% maker / 0.04% taker** (docs.asterdex.com, Jul 2026), −5% if paid in ASTER.
- 1001x mode and Classic/AMM mode have separate schedules — **do not mix modes when quoting costs**.
- Comparison: Aster round-trip taker = 0.08% vs HL 0.09% vs Binance 0.10% vs Bitget 0.12%. Aster is the cheapest taker venue in our map, and the only 0%-maker venue.

### 2.3 Funding mechanics — the floor, verified empirically

Aster publishes per-contract funding parameters on `/fapi/v1/fundingInfo` (interest rate, **interval in hours**, cap/floor ±2%/interval). Empirics from the live snapshot:

| Interval | n | p25 | median | p75 | Pinned at floor |
|---|---|---|---|---|---|
| 1h | 338 | +10.95% | **+10.95%** | +10.95% | **225/338 exactly at 0.000125%/h** |
| 4h | 63 | −0.12% | +10.95% | +10.95% | — |
| 8h | 141 | 0.00% | 0.00% | +9.17% | data-quality caveat below |

The 0.01%-per-8h-equivalent **interest component acts as a structural short-carry floor identical to Hyperliquid's** (+10.95% APR at our 3×365 convention; some sources quote ~11.6% with compounding — same fact). On hourly contracts the floor is scaled per interval (0.0001% × 1h/8h = 0.000125%/h), which is why 225 hourly contracts sit *exactly* at +10.95% APR. **Caveat:** on 8h contracts, 89 Aster symbols report `rate = 0`, and the 8h bucket median is 0.00% — for the older 8h cohort the ticker rate is unreliable (missing, not zero). Trade the **hourly cohort**, which is also the largest (338) and the freshest.

### 2.4 The Aster floor harvest — live pairs

Short-Aster / long-CEX spreads (taker round-trip, 7d/30d amortization):

| Coin | Long → Short | Gross APR | Net@7d | Net@30d | Intervals |
|---|---|---|---|---|---|
| HYPE | bitget → aster | 23.3% | 12.9% | 20.9% | 4h/4h |
| XRP | bybit → aster | 13.9% | 4.6% | 11.8% | 8h/8h |
| BNB | aster → hl | 11.0% | 2.6% | 9.0% | 1h/8h |
| SOL | bybit → aster | 10.2% | 0.8% | 8.0% | 8h/8h |
| BTC | dydx → aster | 9.2% | 0.3% | 7.1% | 8h/1h |

**Maker-entry variant** (enter both legs as maker, exit taker): HYPE rt-cost = 0.02% (bitget maker) + 0% (aster maker) + 0.06% + 0.04% (exits) = 0.12% → net@7d ≈ **17.0%** (vs 12.9% taker-only). XRP: rt 0.115% → net@7d ≈ **7.9%**. Aster's 0% maker makes it the natural *entry* venue for floor-harvest shorts; the binding constraint on majors is that CEX-side funding is already near its own base-rate floor, so the harvest is richest on alts where positioning skews long on Aster (HYPE is the standout: BNB-Chain venue, native demand).

### 2.5 Stock & commodity perps (Family B extension)

| Pair | Long → Short | Gross | Net@30d | Hedge note |
|---|---|---|---|---|
| SKHYNIX | aster → gate | 214.9% | 212.8% | SK Hynix perp rich on Gate; hedge = Bybit stock perp/xStocks |
| SKHYNIX | aster → bitget | 78.6% | 76.2% | |
| XAU (gold) | aster → bybit | 37.5% | 35.2% | commodity perps; small caps, thin books |
| XAG (silver) | aster → bybit | 24.3% | 22.0% | |
| SPCX (SpaceX pre-IPO) | aster → gate | 21.3% | 19.1% | pre-IPO mark; oracle risk extreme |
| CL (crude) | bitget → aster | 12.7% | 10.4% | |

**Identity guard warnings (critical):**
- **Leveraged ETF ≠ underlying.** The top "overall" spreads are `CSOPSKHYNIX2L` / `CSOPSAMSUNG2L` — CSOP **2× leveraged** HK-listed ETF perps on Gate/Binance/Bybit. Pairing them against single-stock perps (SKHYNIX) is **not delta-neutral** (2× beta + decay + FX). Any basket built from these must pair only like-for-like instruments.
- Pre-IPO marks (SPCX) are broker-quoted, not exchange-set — a 21% "funding premium" can be an oracle lag rather than demand. Sandbox until mark-vs-NAV divergence is measured.
- Stock perps settle against tokenized-stock oracles on weekends/holidays when underlying markets are closed — funding can print on stale marks.

### 2.6 Aster-specific risk register

1. **Parameter risk**: the floor is a configuration (`interestRate`), not a law of nature. Monitor `/fapi/v1/fundingInfo` daily; alert on interest-rate or cap changes. HL's floor has persisted for years; Aster's is younger.
2. **OI quality**: Aster volume/positions were heavily points-season-driven. Points programs distort funding (farmers hold skewed books) — good for *collecting* floor funding, but liquidity can evaporate between seasons. Filter on 24h volume ≥ $3M (applied) and sample L2 depth before sizing.
3. **Counterparty/custody**: non-custodial in name, but collateral sits in Aster's contracts; tokenized-stock collateral adds a second layer (the stock-token issuer). Cap DEX-bucket exposure accordingly.
4. **Access/compliance**: "privacy-focused" = address-screening dynamics; geo-restrictions and on-ramp friction are real operational risks for a $1k–$10k account too.
5. **Oracle/mark manipulation** on thin RWA pairs (SPCX, small caps) — the identity guard (±5% mark agreement) catches cross-venue divergence but not coordinated oracle lag.
6. **Smart contract + bridge risk** — standard DEX bucket; treat as one more "exchange" in the counterparty heatmap with its own cap.

### 2.7 API integration notes (for v3)

- **Market data (public, verified live)**: `fapi.asterdex.com/fapi/v1/{exchangeInfo,premiumIndex,fundingInfo,ticker/24hr,depth,klines,fundingRate}` — schemas identical to Binance USDT-M. Rate limit 2,400 weight/min (generous; Binance is 2,400 too on weight).
- **Funding history**: `/fapi/v1/fundingRate?symbol=&limit=` works (used to infer 8h history for BTCUSDT) → the 90-day persistence scanner can ingest Aster with zero schema changes.
- **Auth**: v1 HMAC API keys are legacy (existing keys work, new ones not issued). Current path = **API Wallet**: link a web3 wallet, sign with L1 key via headers. Trade endpoints unlock after a first deposit from the main wallet.
- **Effort estimate**: a working Aster connector ≈ Binance client + a thin wallet-sign auth module (~2–3 days incl. testnet). Highest connector ROI of any DEX.

---

## 3. dYdX v4, Bitget and the rest

### 3.1 dYdX v4 — the free-financing venue

- **Funding settles hourly** (docs.dydx.xyz: "funding rates every hour… calculated at the end of each hour"). Our snapshot: **71/99 markets truly zero**, 28 nonzero with median **+18.2% APR**. dYdX's premium-clamping formula means funding only prints when positioning genuinely skews — zeros are real zeros, verified by distribution shape.
- **Fees**: 0.05% taker schedule with volume tiers; the DAO voted **maker+taker 0% "fee holidays" on BTC and SOL perps** (Nov 2025). If live, a dYdX long leg on BTC/SOL costs *nothing* — the cheapest financing in the map. Verify per-market fee params at trade time.
- **Role in our stack**: default **long-leg venue** when it lists a coin with a rich short venue elsewhere; occasional short leg when a dYdX-specific skew prints (BTC dydx→hl = 11.0% gross — floor-vs-floor arb, net@30d 8.6%).
- **Integration**: v4 indexer REST (public data, verified) + gRPC TX submission with mnemonic-derived keys — the highest-effort connector of the three, ~1 week. 99 markets only; worth it for majors' financing quality, not for breadth.

### 3.2 Bitget — breadth + RWA flag

768 USDT-M contracts; fee schedule **read directly from the API** (`takerFeeRate` median 0.060%, `makerFeeRate` 0.020%); intervals 4h/8h dominant. The `isRWA` flag on contracts is a free screener for tokenized-stock perps — useful for Family B without scraping names. Bitget's medians mirror Binance's (+10.95% fAPR, base-rate floor), so it slots into the CEX bucket as a *hedge* venue, not an alpha source.

### 3.3 Paradex, EdgeX, Lighter, Ostium (qualitative)

- **Paradex**: 0% retail maker+taker, monetizes via PFOF (market makers pay 0.5bps for flow access). If that holds, it's a zero-cost execution venue for both legs. API is JWT-gated (needs an account) — first sandbox probe blocked; queue behind Aster/dYdX. Also runs options — a future hedging surface.
- **EdgeX / Lighter**: orderbook perp DEXs with aggressive fee/promo schedules; endpoints unresolvable from this sandbox. Both run points — expect listing-premium events (E3).
- **Ostium**: FX/commodities/indices perps (oracle-pooled execution like GMX) — overlaps Aster's commodity niche; different execution model (pool counterparty, wider spreads, no orderbook) means funding there is an *effective borrow rate*, not a peer-to-peer rate. Study before pairing.

---

## 4. Expanded funding-differential results

Method: per normalized base asset, best short venue vs best long venue; interval-correct APR; identity guard (leg marks must agree ±5%); liquidity filter ($3M 24h volume where volume is published: Aster/dYdX/Bitget/HL); **quarantine**: pairs whose *long* leg is a Gate/Aster zero-rate (unknown-data) are excluded (294 quarantined). Result: **942 viable pairs** (vs 24 venue-pairs in Part 1's 5-venue snapshot).

**Top overall (condensed — full table in CSV):**

| Coin | Long → Short | Gross | Net@7d | Net@30d | Comment |
|---|---|---|---|---|---|
| CSOPSKHYNIX2L | binance → gate | 460.0% | 449.6% | 457.6% | 2× leveraged ETF — identity trap, see §2.5 |
| RPL | gate → binance | 273.9% | 263.4% | 271.4% | RPL funding spike on Binance (short-squeeze regime) |
| CSOPSAMSUNG2L | binance → bybit | 250.5% | 239.5% | 247.9% | leveraged-ETF caveat |
| SKHYNIX | binance → gate | 220.4% | 209.9% | 217.9% | stock perp |
| TMF | bybit → binance | 208.3% | 197.3% | 205.7% | US 3×-leveraged bond-ETF perp |
| SHAZ / DKNG / VST / SAMSUNG / MEITUAN | various | 125–200% | — | 145–198% | stock perp cluster |
| BNC / FWDI / CXMT / BSP | bybit → binance | 124–193% | — | 122–191% | new listings, premium decays fast |

Reading: the top of the map is now **dominated by stock/RWA perps** — Part 1's crypto-only view under-weighted this family. Crypto-core pairs with *structural* (not event) returns remain the HL/Aster floor harvests plus mid-cap alts.

**DEX vs CEX structural bias** (median DEX fAPR − median CEX fAPR per coin): 154/564 coins DEX-rich. Top events: AI **+775%** (DEX median 778% — Aster/HL listing skew), CRO **+372%**, BULLA +171%, AVAAI +156%. Reverse side (DEX funding *cheaper* than CEX → short-CEX/long-DEX): ZKC −147%, TUT −160%, SLP −268%, INX −368%. This is a **map of listing-premium events** — Family E becomes screenable across 8 venues instead of 5.

**Caveats**: single-point snapshot (one UTC hour); CEX-leg volumes not sampled in this pass (volume filter applied only where published); Gate 8h rates systematically unreliable; dYdX universe skews majors. The **persistence scan (Part 1 §methodology) must be re-run with the 3 new venues before any capital** — snapshot ≠ distribution.

---

## 5. DEX integration architecture (extends Part 2 blueprint)

**Connector taxonomy by integration cost:**

| Tier | Pattern | Venues | Marginal effort |
|---|---|---|---|
| T1 | CEX REST + HMAC | Binance, Bybit, OKX, Gate, Bitget | done in v2 code |
| **T2** | **DEX REST, Binance-compatible + wallet-sign** | **Aster** (Paradex later) | **~2–3 days** |
| T3 | DEX indexer REST + gRPC/own signing | dYdX v4, HL (HL already done) | ~1 week |
| T4 | AMM/pool protocols (quote-sim, gas, nonce mgmt) | GMX, Drift, Ostium, Pendle, Ethena | phase 2+ |

**Cross-cutting design changes the research forces:**

1. **Funding-interval-aware scheduler**: the DEX side is *hourly* (HL, Aster, dYdX all 1h) while CEXs are 4h/8h. Execution and risk checks should cluster around settlement boundaries; the "time-of-cycle bias" open question from Part 2 now matters 3× more — a 1h venue reprints funding 24×/day and the *reprint risk* is the dominant unwind trigger.
2. **Inventory split**: USDT floats on CEXs; USDC/USD1 on DEXs. Rebalancing crosses withdrawal/bridge rails (minutes–hours, fee, chain risk) — research consensus: bridge latency is the #1 killer of CEX↔DEX arb. Design: per-venue target floats + weekly rebalance + emergency-only bridges; never bridge mid-trade.
3. **Parameter-change monitors** (new L1 Data-layer requirement): Aster `fundingInfo` interest-rate/cap, dYdX fee holidays, venue fee schedules — all are *config*, scraped daily, hashed into the opportunity model. An edge defined by a parameter dies when the parameter changes (Ethena compression canary from Part 1 is the same class of risk).
4. **Risk buckets**: DEX smart-contract/counterparty ≠ CEX regulatory/counterparty; separate caps per bucket, and within DEXs per-execution-model (orderbook vs pool).
5. **Reuse the identity guard at execution time**: ±5% mark agreement pre-flight, ±1% at fill, on both legs (already proven necessary — 0 rejects today, but the ON/Semiconductor phantom from Part 1 shows why it must stay).

**Recommended order: Aster first** (Binance-compatible → clone the v2 Binance client, add wallet-sign auth), then dYdX (financing quality on majors), Paradex when an account exists.

---

## 6. New edge candidates — ranked, with validation plan

| # | Edge | Mechanism | Live evidence | Validation before capital |
|---|---|---|---|---|
| E1 | **Aster hourly floor harvest** | Short hourly-cohort perps at the +10.95% floor / long CEX | 225/338 pinned at floor; XRP/HYPE/BNB pairs live | 90d funding-history persistence scan (Aster history endpoint works); floor-parameter stability |
| E2 | **Stock/RWA perp cross-venue funding** | Short rich stock-perp venue vs cheaper venue or xStocks spot | SKHYNIX 215% gross, cluster 125–200% | Like-for-like identity matrix (no 2L-vs-1x), corporate-action calendar, weekend-mark rules |
| E3 | **DEX listing-premium events** | New DEX listing → shorts paid huge funding for days | AI +775%, CRO +372% diffs | Event study on last 20 Aster/HL listings: entry rule, decay curve, squeeze losses |
| E4 | **Commodity perp carry** | Gold/silver/oil funding premiums on Aster vs CEX | XAU 37.5% gross | Cap/floor mechanics, oracle mark quality, capacity |
| E5 | **dYdX zero-fee financing** | Long leg on BTC/SOL at 0 fee while shorting floor venues | BTC dydx→hl net@30d 8.6% | Confirm fee-holiday status via API; slippage vs 99-market depth |
| E6 | **Paradex zero-cost execution** (pending access) | Both legs 0% retail fees | PFOF model documented | Account + JWT probe; liquidity depth check |
| E7 | Aster points/season dual-yield | Funding + points on hedged books | Season-dependent | Treat as **bonus, never underwrite**; only as tie-breaker between two positive-NAV trades |

**Explicitly out of scope for now** (documented so we don't relitigate): cross-chain atomic/MEV arb (infra-heavy, dominated by insiders), pure PFOF/rebate arbing, prediction-market arb (different asset class — can be a separate study on request), triangular latency arb (Part 1: dead for us).

---

## 7. Updated cost & parameter reference

| Venue | Taker | Maker | Funding intervals | Floor/ceil params | Notes |
|---|---|---|---|---|---|
| Binance | 0.050% | ~0.018% | 1h/4h/8h per contract | 0.01%/8h base rate | BNB discount −10% |
| Bybit | 0.055% | 0.020% | 4h/8h | standard | xStocks spot side |
| OKX | 0.050% | 0.020% | 8h | standard | |
| Gate | 0.050% | 0.020% | 8h reported | unreliable tickers | auth-gated history |
| Hyperliquid | 0.045% | 0.015% | 1h | +0.01%/8h floor | Part 1 core |
| **Aster** | **0.040%** | **0%** | **1h (338)/4h/8h** | **0.01%/8h-equiv floor, caps ±2%/interval** | −5% fee paid in ASTER |
| dYdX | 0.050% (BTC/SOL 0% holiday) | 0.020% (BTC/SOL 0%) | **1h** | clamped premium formula | |
| Bitget | 0.060% | 0.020% | 4h/8h | standard | fees from API |

---

## 8. Method appendix

- **Scripts**: `snapshot_v3.py` (one-pass collector, 12 venue-endpoint pulls; reuses snapshot_v2 collectors via import), `analyze_v3.py` (interval-correct APR, identity/liquidity/quarantine guards, DEX-vs-CEX bias map). Both re-runnable; snapshot stored at `scripts/snapshot_v3.json` (2026-09-01 09:28 UTC).
- **Search corpus**: 8 queries (Aster fees/funding/strategy/API/RWA, dYdX funding/fees, DEX landscape, Pendle, CEX↔DEX integration mechanics).
- **Data files**: `download/data/funding_spreads_expanded_2026-09.csv` — 1,236 rows, `flag=QUARANTINE_long_leg_zero` marks the 294 excluded pairs.
- **Known limitations**: single snapshot; Gate/older-Aster zero-rate opacity; Paradex/EdgeX/Lighter/Ostium not probeable from sandbox; CEX-leg 24h volumes not re-collected in this pass; Aster 8h cohort needs `fundingRate` history backfill (endpoint verified working).

## 9. Next research steps (in order)

1. **Extend the 90-day persistence scanner** to Aster (hourly cohort), dYdX, Bitget legs → convert E1/E2 from snapshot claims to distributions (persistence %, widening/decay slope). This is the single highest-value follow-up.
2. **L2 depth sampling** on top-30 pairs (capacity + slippage curves, esp. Aster hourly cohort & stock perps).
3. **Event study** on DEX listing premiums (E3) using Aster/HL new listings.
4. Patch-verified v2 engine can paper-trade HL+Aster floor baskets (see §10) once Sharpe keys arrive.
5. Update `02_system_blueprint.md` connector priority: T2/Aster → T3/dYdX → Paradex.

## 10. Option 3 — v2 live-blocking bug patches (delivered alongside)

The four diagnosed live-blockers were patched in the existing codebase (see `PATCHES_APPLIED.md` for the diff-level detail and test evidence):

1. **Exit orders used `price=0.0` market-order semantics with fixed naive size** → exits now re-quote from live book depth with size reconciliation against actual leg positions.
2. **Exchange-name case mismatch (`Gate.io` vs `gate.io`)** → canonical lowercase venue IDs at the config boundary; lookup maps normalized.
3. **`active_opps` stored stale snapshots** → opportunities now carry capture-timestamp + max-age; stale entries are invalidated before reuse.
4. **Sharpe APR field parsing fragility** (root cause of the bogus "1122.67% Net APR" harvest lines) → strict schema validation with bounded-value guardrails (reject |APR| > 1000%) and per-symbol interval applied before annualization.

Post-patch status: all modules compile; unit-level sanity tests for the patched functions pass; no behavioral change to paper-mode logic other than the four fixes. Live trading remains explicitly disabled pending the research-phase completion gate.
