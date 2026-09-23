# Part 4 — Persistence Distributions: E1/E2 Converted from Snapshot Claims to 90-Day History

*September 2026 · Research-first phase · Companion to Part 3 (`03_venue_expansion_aster_dex.md`). Scanner: `scripts/funding_history_scan_v2.py` · Data: `download/data/funding_persistence_90d_v3.csv`, `e1_aster_hourly_distribution.csv`, `e2_stock_rwa_distribution.csv`.*

---

## 0. Executive summary

The Part-1 persistence scanner was extended from 5 to **7 venues** (adding Aster, dYdX v4, Bitget legs) and re-run over a 90-day window: **158 coins, 1,455 venue pairs, zero fetch failures**. The two flagship edge candidates from Part 3 were converted from single-snapshot claims into distributions (persistence %, conditional survival, episode durations, widening/decay slopes). Three verdicts:

1. **E1 (Aster hourly floor harvest): CONFIRMED as a persistent edge — with a discovered structure.** 163 aster-short hourly-cohort pairs evaluated; the **persistent cohort (≥95% positive days) contains 51 pairs**, 41 with net-30d APR > 10% and 18 > 25%. Majors (FIL, FET, KAS, AR, ICP) run **8–11% APR with 100% positive days across all 90 days** — a structural, slope-flat carry. Micro-caps (BTW 82%, MAGMA 45–47%, POWER, CYS, UAI, CASHCAT, RIVER) run 25–85% net-30d. A third regime was discovered: **cap-slammed negative funding** (LA, PROM) where the Aster *long* leg collects at the −2%/interval cap — a reverse harvest worth its own playbook.
2. **E2 (stock/RWA perp cross-venue funding): REFUTED as persistent carry — reclassified as an event family.** 300 pairs: median full-window APR **0.0%**, p90 0.7%, **zero pairs above 10%**, median positive episode **1 day**. Forensics on the Aster stock legs show 44–96% of settlements are exact zeros with rare two-sided spikes (SAMSUNG max +0.629%/4h, min −0.337%). Part 3's 125–215% claims were event spikes (and Gate-leg artifacts) that mean-revert within 1–2 days. Stock/RWA funding belongs in the E3 listing-premium playbook, not a carry basket.
3. **New finding — dYdX is a *short* venue too.** The Part-3 framing ("free long-leg financing") is only half the story: 18 dydx-short pairs clear net-30d > 15%, led by **XMR dydx→CEX at ~104–105% net-30d (98% positive days, 33-day median episode, +46% APR median funding on the dydx leg)**, plus ONDO (37%), ZEC (31%), TAO (23%). dYdX's clamped-premium formula prints persistent skews on low-float, conviction-heavy names.

**Portfolio implication for $1k–$10k:** the tradeable core is now a **barbell** — (a) a boring, near-riskless **floor basket** of liquid E1 majors at 8–11% APR (100% positive days, flat slopes), (b) a **high-yield E1 micro-cap sleeve** (25–85%, capacity-limited, pending L2 depth checks), and (c) an **event playbook** (E2 spikes + E3 listing premiums + E1r cap-slams) that requires detection-and-exit logic rather than passive carry.

---

## 1. Method — what changed vs the Part-1 scanner

The Part-1 scanner (`funding_history_scan.py`) supported Binance/Bybit/OKX/Gate/HL. The v2 scanner (`funding_history_scan_v2.py`) adds three legs and a distribution layer:

- **Venues (7):** Binance, Bybit, OKX, HL (unchanged fetchers) + **Aster** (Binance-compatible `/fapi/v1/fundingRate`, `startTime` pagination, symbol map built from `exchangeInfo`), **dYdX v4** (indexer `historicalFunding`, hourly), **Bitget** (v2 `history-fund-rate`, `pageNo` paging — `pageSize` silently capped at 100). Gate is **excluded** (funding history remains auth-gated), which directly limits E2 validation (see §6).
- **Distribution metrics added per pair:** persistence at 3/10/25 bps per day (≈ 11%/36.5%/91% APR equivalents), conditional survival P(carry > 0 at t+1 | t) and at t+7, episode structure (count, median/max duration of contiguous positive-carry runs), **widening/decay slopes** (full-window OLS + 7d-MA OLS in APR-points/day, plus median within-episode slope), and net APR at the empirically-median episode duration.
- **Unchanged core methodology:** funding is treated as a **per-event cash flow** (union-of-settlement-hours zero-fill); intervals are inferred per contract from the history itself; identity guard (±5% mark agreement); taker round-trip costs (Aster 0.04% and Bitget 0.06% added).
- **Candidate universe:** all liquid Aster hourly-cohort coins with ≥1 alternative venue (58, the E1 cohort), all stock/RWA bases from Aster's `underlyingSubType` tags (121), top-25 live differentials per venue pair, and majors → 158 coins after the multi-venue/cap prioritization.

Data quality: zero fetch failures; 23 pairs dropped for <14-day common coverage; 13 identity-guard skips. dYdX legs carry a **42-day window** (the indexer ignored both `beforeOrAt` and `beforeAtHeight` cursor params — pages are capped at the newest 1,000 hourly records), so dydx-leg "APR90" figures are 42d-based and flagged via `overlap_days` in the CSV.

---

## 2. E1 — Aster hourly floor harvest, as a distribution

**Claim under test (Part 3, E1):** "Short hourly-cohort Aster perps at the +10.95% floor / long CEX; 225/338 pinned at floor." A snapshot cannot distinguish a permanently pinned parameter from a fleeting print; the 90-day history can.

**Distribution across all 163 aster-short hourly pairs:**

| Metric | p10 | p25 | median | p75 | p90 |
|---|---|---|---|---|---|
| APR, full window | −9.0% | −2.2% | 6.1% | 12.5% | 28.2% |
| APR, last 30d | −13.2% | 2.6% | 8.3% | 13.1% | 41.3% |
| Positive-day fraction | 37% | 62% | 84% | 99% | 100% |
| Days ≥ 3 bps carry | 0% | 2% | 10% | 47% | 73% |
| Median episode length | 2d | 3d | 7d | 57d | 91d |

Reading: the cohort is **barbell-shaped**, and the median understates the edge because it mixes in pairs where the CEX long leg's own funding eats the Aster floor (floor-vs-floor majors net ≈ 0) and pairs whose direction flipped. The honest selection is the **persistent cohort — 51 pairs with ≥95% positive days**:

| Slice | Count | Net-30d APR (p25 / med / p75) |
|---|---|---|
| All persistent (pos ≥ 95%) | 51 | 22.2% / 8.8%* / 6.9% |
| Persistent with net-30d > 10% | 41 | — |
| Persistent with net-30d > 25% | 18 | — |

*\*median pulled down by many ~6–9% liquid pairs; the p25 tail is micro-caps.*

**The two E1 sleeves, concretely:**

- **Liquid floor basket (boring, real):** FIL, FET, KAS, AR, ICP short-Aster vs Binance/Bybit/Bitget/dYdX longs run **7.8–11.0% APR with 100% positive days across the full 90 days** and flat slopes (see below). FIL aster→dydx shows 86% of days ≥ 3 bps — the cleanest floor-print in the map. This is the institutional version of the harvest: capacity-decent, near-zero variance, ~9% net — a direct T-bill-killer that needs no event detection.
- **High-yield micro-cap sleeve:** BTW aster→bitget **82.5% net-30d** (97% pos, 94% of days ≥ 3 bps, 20d episodes), BTW→bybit 72.3% (66d episodes), MAGMA 44.8–46.7% across three long venues (91–100% pos), 龙虾, POWER, CYS, UAI, CASHCAT, RIVER at 22–57%. These passed a ≥$1M/day volume screen but remain capacity-limited; L2 depth sampling (Part-3 next-step #2) gates this sleeve.

**Widening/decay slopes (the question asked):** median within-episode slope in the persistent cohort is **+0.00 APR-points/day and 7d-MA trend ≈ 0.00** — the edge is **flat, not decaying**. That is the signature of a structural parameter (the interest-rate floor), not a fading listing premium: episodes last 28–91 days and end only when CEX-side funding rises to meet the floor, not because the premium bleeds out. For contrast, the decaying candidates (LAB, the E2 spikes) show strongly negative 30d-vs-90d compression (LAB: 64.9% full → 11.4% last-30d).

**Floor stability (parameter risk, quantified):** across 60 Aster hourly coins with ≥30 settlements, the median coin printed **exactly the floor (0.000125%/h, ±10%) on 60% of settlements**; 33/60 coins ≥ 50% of the time; only 6/60 ≥ 90%. Interpretation: the floor is a *frequently-visited attractor with real positioning-driven excursions above it* — good news, because excursions are extra carry for shorts, and bad news only if the floor parameter itself moves (monitor `/fapi/v1/fundingInfo` daily, as planned).

**E1r — the discovered reverse regime (cap-slam):** LA and PROM show Aster hourly funding *repeatedly pinned at the −2%/interval cap* (PROM min −2.000%/h; LA median −0.0036%/h ≈ −31% APR to shorts). There the trade inverts: **short the CEX (collecting its positive funding) / long Aster (collecting the negative)** — LA bitget→aster 154% full / 47% last-30d, PROM ~207% last-30d, but with high variance and 71–84% positive days. This is event-flavored (cap-slams cluster around skew events) and needs its own entry/exit rules; treat as E1's mirror, not passive carry.

---

## 3. E2 — stock/RWA perp cross-venue funding, as a distribution

**Claim under test (Part 3, E2):** "SKHYNIX 215% gross, cluster 125–200% — short rich stock-perp venue vs cheaper venue." The scan evaluated **300 stock/RWA pairs** (universe: Aster's 121 STOCK/ETF/Commodities/USD1-RWA/pre-launch/Semiconductor bases, crossed with all 7 venues; Gate excluded for history).

| Metric | p10 | p25 | median | p75 | p90 |
|---|---|---|---|---|---|
| APR, full window | −0.7% | −0.3% | **0.0%** | 0.4% | 0.7% |
| APR, last 30d | −0.8% | −0.4% | −0.1% | 0.2% | 0.7% |
| Median positive episode | 1d | 1d | **1d** | 2d | 2d |

**Zero pairs exceed 10% APR over the full window.** The best full-window pair is SKHYNIX bitget→bybit at 5.4% APR with 53% positive days — noise, not carry.

**Forensics on the Aster stock legs (the Part-3 "rich" side):** 90-day history shows SKHYNIX 66% exact zeros (max spike +0.096%/4h ≈ 87% APR instantaneous), GOOGL 72% zeros (max +0.050%/8h), TSLA 80% zeros, XAU 96% zeros, SPCX 85% zeros, MINIMAX max +0.287% / min −0.634%, SAMSUNG 44% zeros with **max +0.629%/4h and min −0.337%** — i.e., funding is *normally zero* with occasional **two-sided spikes that mean-revert within a day or two**. The Part-3 snapshot numbers were real prints, but they were **events, not a regime** — and the largest quoted spreads depended on Gate legs whose history is auth-gated (and whose 8h zero-opacity was already flagged in Part 3).

**Verdict:** E2 as *persistent cross-venue carry* is **refuted** at this venue set. What survives is an **event family**: oracle/listing-driven funding spikes on stock & RWA perps, two-sided, 1–2 day half-life, catchable only with a live spike detector and pre-positioned hedges. This merges naturally into the E3 listing-premium playbook (Part 3) with stock/RWA as a sub-screen — the combined detector should watch: new Aster/Bitget stock listings, |rate| > 0.05%/interval prints, and cap-proximity events, then require the like-for-like identity matrix (no 2L-ETF-vs-single-stock pairing) before execution.

---

## 4. Bonus finding — dYdX as a short venue

Part 3 classified dYdX as "free-financing long leg" (71/99 markets at true zero). The history scan adds the other half: **18 dydx-short pairs clear net-30d > 15%**, led by:

| Pair | APR (42d win) | Last-30d | Pos days | Days ≥ 3bps | Med episode | Net-30d |
|---|---|---|---|---|---|---|
| XMR dydx→bybit | 107.2% | 98.4% | 98% | 95% | 33d | **104.6%** |
| XMR dydx→binance | 106.9% | 97.7% | 98% | 95% | 33d | 104.5% |
| XMR dydx→aster | 87.2% | 70.5% | 93% | 79% | 6d | 85.0% |
| ONDO dydx→binance | 39.9% | 45.7% | 93% | 65% | 14d | 37.4% |
| ZEC dydx→binance | 33.9% | 28.9% | 91% | 67% | 12d | 31.5% |
| TAO dydx→binance | 25.9% | 36.0% | 56% | 30% | 2d | 23.4% |

XMR's dYdX leg prints a **median +0.0052%/h (+46% APR) and is above the 3bps/day equivalent 66% of hours** — a persistent, conviction-driven skew (privacy-coin long bias on a DEX with clamped-premium funding), not a one-day event. This upgrades Part 2's XMR finding (bybit/HL, 29.7% APR, accelerating) — **dYdX is now the richest XMR venue in the map**. Caveats: 42-day window; XMR listing/liquidity depth on dYdX must be depth-checked; a skew this rich is a squeeze magnet, so sizing and unwind-cost modeling matter more than usual.

---

## 5. Data caveats

1. **dYdX window = 42 days** (indexer cursor params ignored; newest 1,000 hourly records only). All dydx-leg "APR90" figures are 42d-based; `overlap_days` in the CSV is authoritative per pair. A re-run with a working cursor (or gRPC-based history) is queued.
2. **Aster 8h cohort opacity persists in history**: stock/RWA legs are mostly 4h/8h with 44–96% exact zeros — treated as real zeros (conservative for spreads), but it means E2's Aster-side funding is a lower bound where zeros were missing data.
3. **Gate excluded** (auth-gated funding history): Part-3 gate-dependent claims (SKHYNIX aster→gate 215%, RPL 274%) cannot be validated historically from this sandbox.
4. **Bitget history depth**: 100 records/page × 9 pages ≈ 900 settlements — sufficient for 4h/8h contracts over 90d, partial (~42d) for its three 1h contracts.
5. **Liquidity**: the ≥$1M/day volume screen is coarse. The high-yield E1 sleeve and dYdX-short pairs **require L2 depth sampling before sizing** (next step, unchanged from Part 3).
6. **Costs are taker-only**; the 0%-maker Aster entry variant (Part 3 §2.4) would improve net figures by ~2–4 pts on the floor basket.

## 6. Next research steps (re-ranked by these results)

1. **L2 depth + slippage sampling** on the E1 persistent cohort (51 pairs) and the dYdX-short XMR/ONDO/ZEC set — this is now the only gate between the research and a paper basket.
2. **Basket construction**: liquid floor basket (majors, ~9% net, 100% pos days) + micro-cap sleeve sized by depth; define the CEX-funding-rise exit rule (episode ends when the long leg's funding meets the floor).
3. **Event playbook spec** (merges E2 + E3 + E1r): spike/cap-slam detector thresholds (|rate| > 0.05%/interval, cap-proximity, new listings), entry/exit timing, and two-sided handling.
4. **dYdX cursor fix** for full 90d coverage, and XMR dydx depth/oi verification.
5. **Floor-parameter monitor** (Aster `fundingInfo` + HL) wired into the v3 data layer with change alerts — the E1 basket's only kill-switch.
