# Crypto Arbitrage Market Study — Venues, Strategies, Edges
**Date:** 2026-09-01 · **Scope:** all delta-neutral / quasi-arbitrage families accessible to $1k–$10k capital · **Method:** live snapshot of 3,500+ instruments across 5 venues + 90-day funding-history persistence scan (112 coins, 221 venue-pairs) + industry research
**Companion file:** `research_notes_cross_ex_funding_arb.md` (strategy mechanics deep-dive from the previous session)

---

## 0. Executive summary

We studied every arbitrage family realistically accessible at retail scale on Binance, Bybit, OKX, Gate and Hyperliquid (HL). Data was collected directly from exchange APIs (no third-party feeds), and every candidate opportunity was stress-tested against **90 days of funding history** rather than the seductive live snapshot. Five conclusions survive scrutiny:

1. **There is one clean, structural, explainable edge: short-Hyperliquid / long-CEX funding capture on liquid alts.** HL pays its interest-rate floor (≈0.03%/day ≈ 11%/yr) to shorts while CEX funding on the same assets averages ≈0. Measured over 90 days in today's direction: **HYPE 10.5% APR with 100% positive days, NEAR 11.9% / 99% days, GMX 11.7% / 98%, ENA 9.7% / 97%, LINK 10.5% / 94%**. Max drawdown of the funding-income curve on these pairs: 0.01–0.05% of notional. Currently the premium is *widening* (14-day APRs exceed 30-day APRs on most of the basket).
2. **A second, newer vein: tokenized-stock perps.** Binance/Bybit/Gate list perps on US and HK/KR equities (HOOD, GOOGL, META, TSLA, PDD, MEITUAN, KUAISHOU, KODEX200…). Longs persistently pay: **GOOGL 20% APR over 90d and accelerating (25% last 14d), META 13→26%, TSLA 10→21%, KUAISHOU 114% in its first 14 days**. The natural hedge (1:1-backed xStocks spot on Bybit) exists with ~0.15% round-trip cost. Caveats: new products, thin xStock liquidity, weekend basis.
3. **Live snapshots systematically lie.** Momentary funding differentials of 80–340% APR decay to 6–30% within weeks (OP looked like 87% net on our live scan; its true 90-day realized capture is ≈9%). Mean reversion in cross-ex funding spreads is fast. Any system must rank candidates by *persistence*, not by instantaneous APR.
4. **Most classical arbs are dead at our scale.** Cross-exchange spot spreads on majors: median 0.009–0.019% vs ~0.18–0.21% taker round-trip. Quarterly basis: 4.5–4.8% annualized on BTC — a T-bill competitor, not an edge. Same-venue spot-perp basis is ≈0 (±0.08%) with only the ~11% default-funding floor as carry.
5. **Data integrity is a first-class research problem.** We hit (and fixed) three landmines that would silently destroy a live bot: symbol truncation creating phantom tickers (`ETHFI`→`ETH`), ticker collisions across asset classes (`ON` = $0.23 memecoin on Binance vs $74 ON Semiconductor on Bybit — a "49% APR arb" between two different companies), and per-contract funding intervals (Binance: 425 symbols now settle every **4h**, Bybit: 369) that double APRs if you assume 8h.

**Bottom line for the system we will build:** a rotation portfolio of 5–10 short-HL/long-CEX carry pairs on liquid alts as the reliable core (~8–10% net APR target at low risk), plus a rules-gated satellite for stock-perp carry and new-listing funding events (lumpy, higher APR, higher operational risk). Both fit the same engine: scanner → persistence filter → protected execution → portfolio risk manager. Details in Part 2 (system blueprint, `02_system_blueprint.md`).

---

## 1. Venue landscape and cost base (September 2026)

### 1.1 Who we can trade

| Venue | USDT-M perps | Spot | Funding intervals observed | Taker / maker fee (base tier) | Notes |
|---|---|---|---|---|---|
| Binance | 838 | 485 | **4h ×425, 8h ×410, 1h ×3** | 0.050% / 0.020% | Deepest books; tokenized-stock perps (US + HK/KR names) |
| Bybit | 744 | 399 | 4h ×369, 8h ×371, 1h ×4 | 0.055% / 0.020% | xStocks spot (TSLAX, NVDAX, HOODX, GOOGLX, METAX, AMZNX, MCDX, CRCLX…) + stock perps |
| OKX | 80 sampled | 395 | mostly 8h | 0.050% / 0.020% | Deep majors; no xStocks found in this snapshot |
| Gate | 950 | — | 8h / 4h | 0.050% / 0.020% | Widest alt coverage incl. many illiquid names; funding *history* API now requires signed headers (live-only for us) |
| Hyperliquid | 177 | 305 (memecoins; xStocks NOT currently in spot meta) | **1h for everything** | 0.045% / 0.015% | Interest-rate floor pays shorts ≈0.03%/day; no KYC; HLP vault; pre-launch perps |

### 1.2 The interval regime change (critical math)

Funding APR = rate × (24 / interval_hours) × 365. Nearly half of Binance/Bybit perps now settle every **4 hours**, and everything on HL settles hourly. Annualizing a 4h-settlement symbol with 8h math understates its APR by **2×**. Any scanner must read the interval per contract (Binance exposes it via `/fapi/v1/fundingInfo`; Bybit via `fundingInterval` in instruments-info; Gate via `funding_interval` on tickers; HL is hourly by construction). Our legacy v1 codebase hardcoded 8h — one of its core sizing bugs.

### 1.3 The fee hurdle

Round-trip, both legs, taker:

| Pair of venues | Round-trip cost |
|---|---|
| Binance ↔ Bybit | 0.21% |
| Binance ↔ HL | 0.19% |
| Bybit ↔ HL | 0.20% |
| Binance ↔ OKX | 0.20% |
| Spot (Bybit) + perp (Binance) for stock carry | 0.15% |

At 30-day holds that is ≈2.4–2.6% APR of drag; at 7-day holds ≈10–11% APR. **Holding period and maker/taker mix dominate net APR** more than the funding differential itself. Maker entries roughly halve the hurdle.

---

## 2. The arbitrage taxonomy, with live + 90-day evidence

### Family A — Cross-exchange perp funding differential (the core edge)

**Mechanics:** short the perp where funding is high, long the perp where funding is low, equal notional, delta-neutral. Income = notional × (f_short − f_long) paid at each venue's settlement times. Risks: funding flip, leg execution slippage, asymmetric liquidation, venue risk.

**The structural finding.** Hyperliquid charges its interest-rate floor even when the premium is zero, and its retail crowd is long-biased; institutional basis capital (Ethena-style) arbitrages CEX funding toward zero but does *not* operate on HL. Result: a persistent, widening HL-vs-CEX funding premium on liquid alts. 90-day persistence scan (direction = today's, costs excluded; `net30` = after taker round-trip amortized over 30d):

| Coin | short / long | APR 90d | APR 30d | APR 14d | Positive days | Max DD of income | net30 |
|---|---|---|---|---|---|---|---|
| XMR | hl / binance | **29.7%** | 47.3% | **63.8%** | 91% | 0.24% | 27.4% |
| XMR | hl / bybit | 29.8% | 47.9% | 65.3% | 91% | 0.26% | 27.4% |
| TRUMP | bybit / hl | 20.4% | 1.1% | −2.2% | 68% | 0.32% | 18.0% |
| LIT | hl / bybit | 14.1% | 0% | 0% | 98% | 0.12% | 11.6% |
| NEAR | hl / binance | 11.9% | 15.7% | 22.6% | **99%** | 0.02% | 9.6% |
| GMX | hl / bybit | 11.7% | 15.6% | 21.5% | **98%** | 0.02% | 9.3% |
| FET | hl / binance | 10.7% | 13.8% | 22.8% | 93% | 0.13% | 8.4% |
| HYPE | hl / bybit | 10.5% | 0.8% | 0% | **100%** | 0.01% | 8.1% |
| LINK | hl / bybit | 10.5% | 11.2% | 15.1% | 94% | 0.02% | 8.0% |
| AAVE | hl / binance | 10.1% | 13.2% | 22.9% | 92% | 0.05% | 7.7% |
| ENA | hl / okx | 9.7% | 13.7% | 21.2% | 97% | 0.04% | 7.4% |
| PENDLE | hl / binance | 9.0% | 12.0% | 15.9% | 92% | 0.07% | 6.6% |
| PEPE | hl / binance | 8.7% | 12.0% | 16.6% | 91% | 0.15% | 6.4% |
| SUI | hl / binance | 8.5% | 11.9% | 18.0% | 86% | 0.08% | 6.2% |

Readings:
- The "floor-only" names (HYPE, GMX, LINK, FET, NEAR, AAVE — HL funding pinned at 0.00125%/h) yield a stable ≈10–12% gross APR. The premium names (XMR at 13× floor right now) add lumpy upside.
- Regime shifts are visible and fast: ENA/FET/AAVE/GMX all jumped ~day 76–82 of the window (mid-August). The 14d > 30d > 90d ordering across the basket says the premium is **currently widening**, likely because CEX-side basis capital keeps compressing CEX funding while HL flow stays retail-long.
- Direction risk is real but measurable: the same scan flags traps (e.g., "TRX short-Binance/long-HL" showed +11% APR90 but −6% over the last 14 days; ONDO/AR/ENA in the flipped direction are negative 90+% of days). The scanner must compute the 14/30/90-day metrics *in the candidate direction* and require consistency.

Charts: `charts/hl_structural_premium.png` (basket APR + positive-day %), `charts/funding_equity_curves.png` (cumulative capture curves — near-straight lines = carry, not luck). Full universe: `data/funding_persistence_90d.csv` (221 pairs).

**Verdict: CORE STRATEGY.** Target net 6–10% APR at high consistency, with occasional 30–60% regimes. Capacity at our size: effectively unlimited (perp books of the majors absorb $10k notional trivially).

### Family B — Tokenized-equity carry: stock perps vs xStocks (the new vein)

**Mechanics:** stock perps on Binance/Bybit/Gate settle funding in USDT like any perp; the long side has persistently paid because equity exposure on crypto rails is structurally long-demand (weekend access, no brokerage, retail speculation) while natural short hedges (xStocks spot, 1:1 Backed tokens on Bybit/Kraken) are thin and operationally awkward. Short the perp + buy the matching xStock = equity-neutral carry.

90-day funding APR on Binance stock perps (short side receives; sign = longs pay when positive):

| Underlying | APR 90d | APR 30d | APR 14d | share of positive 8h events |
|---|---|---|---|---|
| GOOGL | **20.0%** | 22.7% | 24.8% | 44% |
| META | 12.9% | 14.3% | **25.7%** | 36% |
| CRCL | 37.4% | 11.6% | 18.9% | 28% |
| COIN | 15.3% | 12.2% | 2.5% | 37% |
| NVDA | 15.1% | 4.8% | 10.6% | 11% |
| TSLA | 10.4% | 9.6% | 20.5% | 29% |
| AMZN | 11.8% | 9.3% | 18.6% | 26% |
| HOOD | 8.8% | 3.2% | 2.1% | 26% |
| KUAISHOU (HK) | 21.3% | 65.5% | **113.5%** | 24% |
| MEITUAN (HK) | 11.6% | 35.7% | 72.7% | 20% |
| TENCENT (HK) | 18.7% | 29.0% | 43.4% | 16% |
| KODEX200 (KR) | 11.4% | 35.1% | 61.6% | 25% |
| GDX | −1.5% | −4.7% | −18.2% | 18% |
| TMF | −2.2% | −5.1% | −31.6% | 8% |

Live cross-product check (xStock spot vs perp, same moment): HOODX–HOOD@binance funding 40.2% with basis +0.03%; GOOGLX 15.9% basis −0.12%; METAX 13.6% basis −0.14%; round-trip cost ≈0.15%. Negative-funding names (GDX, TMF — crowded *shorts*) imply a reverse carry (long perp, short xStock) where the xStock leg exists.

**Why this persists:** funding is set by retail flow on the perp; the arb capital that would compress it must hold xStocks (issuer/redemption constraints, weekend gaps, small books) — friction = our premium. The big HK/KR numbers are listing-phase crowding (KUAISHOU/MEITUAN listed ~Aug 10, 2026).

**Verdict: SATELLITE STRATEGY with strict rules.** Trade only the mega-cap US names with xStock hedge leg available; cap per-name exposure; expect weekend/after-hours basis wobble; re-verify funding persistence weekly (these are 2–10 month old products — behavior may drift). Do NOT chase the 100%+ HK listings at size.

### Family C — Same-venue spot-perp basis (long spot / short perp)

Live measurement across Binance/Bybit/OKX majors: perp-vs-spot basis is ≈0 (−0.08% to +0.08%), leaving only the default funding floor: **≈10.95% APR gross at 0.01%/8h**, ≈8.5% net of 0.20% round-trip at 30d holds. But this is exactly the trade institutional basis funds (Ethena: $5.5–6B AUM) compress; the *aggregate* basis yield environment is visibly compressed in 2026 (sUSDe paying ≈4%, vs 27% at the 2025 turn-of-year peak per FT/industry data). BTC quarterly basis for reference: Sep contract 4.5% annualized, Dec 4.8%; ETH 3.0–4.1%.

**Verdict: benchmark, not edge.** Use 11% as the yardstick every other strategy must beat after costs; run it only opportunistically when basis/funding spikes (e.g., QNTX-type squeezes at 130–330% APR).

### Family D — Cross-exchange spot dislocation

Median |mid_A/mid_B − 1| on 28–29 shared majors: Binance↔Bybit 0.0087%, Binance↔OKX 0.0193%, Bybit↔OKX 0.0130%; p95 ≤0.11%; max 0.32%. Taker round-trip is 0.18–0.20%, plus withdrawal latency and inventory risk. Effective spread capture for takers: **zero after costs**; this is a maker-with-rebates + colocation game (and academic work — Guo 2025 — shows exchange default risk eats much of what's left).

**Verdict: dead for $1k–$10k takers. Keep as monitoring dashboard only** (large dislocations = venue-stress signal feeding the risk manager).

### Family E — Event-driven funding / listing events

Observed live regimes our scan caught in passing: new stock-perp listings (KUAISHOU 113% APR14), crowded-long squeezes on alts (QNTX 130–330% APR across venue pairs), TRX HL-vs-CEX spikes, tokenized Korean ETF (KODEX200 61% APR14). Also HL "hyperps" (pre-launch perps with mark capped at 1.5× external perp price) and prediction-market perps (HIP-3) create periodic listing events.

**Verdict: monetizable but NOT a standing strategy.** Build it as an alert layer: scanner flags any pair whose live differential × persistence screen passes thresholds; human (or strict rules) approves entry; exits when funding normalizes or after N days cap. The v1 bot's fixed-size, price-blind entry logic must never touch these.

### Family F — Statistical arbitrage / pairs (directional-neutral, model-driven)

Cointegration pairs trading is well documented academically (dynamic cointegration, HFP), but at our capital the funding-differential families above dominate it: they have *structural* reasons for mean reversion (funding formulas), while stat-arb needs latency, many pairs, and tolerates tail correlation breaks. Keep on the research roadmap after the core engine is live.

**Verdict: deferred.**

### Family G — Latency arb / triangular (documented as NOT viable)

Sub-second cross-venue price arb requires colocated infrastructure, maker rebates, and inventory pre-positioning across venues; retail triangular arb is a well-known loser after fees (median spot dislocation 0.009–0.019% vs 0.18% cost round-trip is the same conclusion in cross-venue form). We skip deliberately.

---

## 3. Edge-discovery: six empirical laws from this study

1. **Per-contract interval math is non-negotiable.** 425 Binance + 369 Bybit symbols settle 4-hourly; HL hourly. Wrong interval ⇒ 2× APR error ⇒ wrong sizing and wrong ranking.
2. **Ticker identity must be validated by price, never assumed by symbol.** Found: `ON` = memecoin ($0.234, Binance/Gate) vs ON Semiconductor ($74.3, Bybit) — the 90d scanner initially reported a "49% APR spread" between two unrelated assets; `SPX` = Spx6900 memecoin on HL, not the S&P 500; `AVA`/`APEX` collisions with xStock names. Guard: same-asset cross-venue marks must agree within ~5%.
3. **Live differentials are marketing; persistence is product.** Live-scan top differentials ran 87–340% APR; their 90-day realized capture is 6–30%. Rank by 14/30/90-day consistency (positive-day %, max DD), enter only when all three agree.
4. **The HL interest-floor premium is the cleanest structural edge found** (mechanism in §Family A). It also widens when CEX basis capital grows — a trend aligned with Ethena's expansion.
5. **Stock-perp funding is a young, friction-protected vein** — persistent on US mega-caps (GOOGL/META/TSLA/AMZN), explosive on fresh HK/KR listings, occasionally *negative* (GDX/TMF) which is itself signal.
6. **The fee hurdle decides everything at $1k–$10k.** 0.15–0.21% round-trip = 10–11% APR drag at 7-day holds vs 2.4–2.6% at 30-day holds. Prefer fewer, longer, higher-conviction holds; maker entries; and never trade sub-5%-expected-capture events.

---

## 4. Capital math ($1k–$10k)

### 4.1 Core HL-carry basket, $5,000 account

Allocation: 8 pairs × $500 notional per leg (=$4,000 gross per side; ~1.6× effective leverage on $2.5k margin per venue at 3x setting — conservative). Expected blended gross ≈10–12% APR (basket weights the 8–30% range), minus 2.4–2.6% APR fee drag at 30d holds, minus slippage ~0.04% per full rotation ≈ 0.5% APR → **net ≈6–9% APR with 90%+ positive days and funding-income max-DD of ≈0.1% of notional**. In dollar terms: ≈$300–450/yr on $5k, with the tail upside from XMR-type premium regimes. This is not a get-rich APR — it is a low-risk core that the satellite layer amplifies.

Liquidation safety: both legs move together (delta-neutral); what matters is per-venue margin on adverse *decoupling* and funding flips. At 3× leverage a ±25% adverse move without hedge slippage is required to threaten the margin — the risk manager still caps every pair at its own kill-switch.

### 4.2 Satellite: stock-perp carry, $1,000 sleeve

2–3 US mega-cap names (e.g., GOOGL + META + TSLA) × $300 notional: expected 15–25% APR gross on the sleeve ≈ $45–75/yr plus listing-event spikes; risks: weekend basis, xStock liquidity, product novelty.

### 4.3 What we are NOT doing

No cross-ex spot arb, no triangular, no latency games, no sub-$0.05 price micro-caps, no tokenized-Kospi-style listings at size, no leveraging the core basket beyond ~2× effective.

---

## 5. Risk register (ranked by expected damage)

1. **Legging risk** — one leg fills, the other doesn't (HL has no batch orders). Mitigation: IOC limit + price collar, auto-unwind the filled leg on timeout, per-pair max slippage budget. (Legacy `anti_skew` concept and HedgeLeg recovery logic from v1/v2 are reusable.)
2. **Funding flip / regime decay** — the direction we entered stops paying. Mitigation: rolling 7/14/30d persistence score per open pair; auto-exit when the 14d score turns negative for 3 consecutive days.
3. **Asymmetric liquidation** — violent one-sided move + cross-venue lag. Mitigation: conservative per-venue leverage (≤3×), margin alerts at 150% maintenance, auto-deleverage plan.
4. **Ticker/asset identity errors** — see Law 2. Mitigation: price-agreement guard + hard-coded venue symbol maps generated at startup.
5. **Interval/staleness math errors** — the v1 class of bug. Mitigation: interval read from exchange metadata at scan time; data age stamps everywhere; refuse signals older than 90s.
6. **Venue/counterparty risk** — Gate history endpoint now requires signed headers (API surface churns); HL is a single-operator L1; keep capital spread ≥2 venues and withdraw profits weekly.
7. **xStock-specific** — issuer/redemption gates, weekend gaps, thin books; cap sleeve, never market-order the xStock leg.
8. **Operational/API churn** — Bybit renamed `fundingTime`→`fundingRateTimestamp`; Gate auth-gated history; Binance added `fundingInfo`. Schema-drift monitoring (fail loudly on missing fields) is part of the data layer spec.

---

## 6. Methodology appendix (what was actually run)

- `scripts/snapshot_v2.py` — one-pass collector: perp funding+interval+mark (5 venues), spot mids (4 venues incl. HL spot), Binance quarterly contracts. All public endpoints.
- `scripts/funding_history_scan.py` — 90d funding history for 112 coins across Binance/Bybit/OKX/HL (Gate live-only; endpoint requires signed headers since ~mid-2026); per-event cash-flow model (union of settlement hours, zero-fill, per-contract interval inference); outputs `download/data/funding_persistence_90d.csv`.
- `scripts/analyze_v2.py` — family quantifier A–E (differentials, same-venue basis, cross-ex spot, quarterly basis, xStock-vs-stock-perp) → `scripts/arb_family_summary.json`, console report `scripts/arb_family_report.txt`.
- `scripts/stock_perp_history.py` — 90d funding persistence for 20 tokenized-stock perps on Binance.
- `scripts/make_charts.py` — the two charts in `download/charts/`.
- Known modeling choices: funding treated as per-event cash flows (no forward-fill); taker fees throughout (maker upside ignored → conservative); no borrow/rebate side-yields counted; snapshot direction bias possible right after settlements (live rates read ≈0 in the first hour of a cycle).
