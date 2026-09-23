# Part 5 — L2 Depth Gate: Measured Slippage on the Candidate Basket (Option 1)

*September 2026 · Research-first phase · Companion to Part 4 (`04_persistence_distributions.md`). Sampler: `scripts/depth_sampler.py` · Gate math: `scripts/analyze_depth.py` · Data: `download/data/l2_depth_samples.csv` (279 raw samples), `depth_pair_gates.csv` (69 pairs).*

---

## 0. Executive summary

Part 4 ended with one remaining gate between the research and a paper basket: **does the order book actually let a $1k–$10k account capture the funding edge?** We sampled L2 books on all 7 venues for the 93 unique legs of the 69 candidate pairs (51 E1 persistent + 18 dYdX-short): 3 passes, 279 book snapshots, zero fetch failures, walk-the-book VWAP slippage for $0.5k–$25k orders per side. Verdicts:

1. **The micro-cap E1 sleeve survives the gate at $1k — barely and with caps.** 25 of 69 pairs pass the strict gate (≤25 bps/side both legs, depth ≥ notional within 25 bps, net > 5% @30d). Top: 龙虾 aster→binance **103.7% net @1k** (cap $2.5k), BTW aster→bitget **78.8%** (cap $5k), MAGMA aster→bitget **51.5%**, UAI aster→binance **43.7% (cap $25k — passes $10k too)**. Sleeve median 31.6% net @1k.
2. **dYdX shorts are wide-but-executable.** The dYdX XMR book is genuinely sparse — top-of-book spread **125.7 bps vs 0.19 bps on Binance** — so taker entries pay ~63 bps/side. Even paying it, XMR dydx→binance nets **79.6% @1k (taker) / 87.2% (maker entry)** with a 5.6-day cost breakeven; ONDO 33.1/38.0%, ZEC 21.1/23.8%, TAO 29.4/31.4% (**TAO passes $10k**). 14 wide-but-net>15% pairs identified; the dYdX leg should always be entered as maker.
3. **The boring floor basket fails at $1k taker.** Majors (FIL 6.0%, FET 4.1%, LDO 3.6%, KAS 1.2%, AR 2.5%) have 18–28-day cost breakevens at $1k with taker entries — the ~9% gross floor cannot pay 0.2% round-trips fast enough on small capital. It becomes viable with Aster maker entries (FIL 7.8%) and/or larger notionals (FIL passes $10k at 3.9%) — i.e., the floor basket is a **maker+patience** harvest, not a small-taker trade.
4. **The depth gate is load-bearing — it caught 5 hollow books that pure funding persistence ranked highly.** SPX aster→dydx (1,865 bps round-trip), ZEN aster→dydx (2,124 bps), POWER at $10k (2,198 bps), KITE at $10k (300–450 bps), MORPHO dydx→aster (116 bps vs 10.6% carry). All would have paper-traded beautifully on Part-4 numbers and bled out on execution.

**Portfolio implication ($1k–$2.5k):** run the barbell with measured-cost sizing — one or two micro-cap pairs (≤ pair_cap each) + one dYdX short with maker entry — at ~60–70% of measured cap, keep 30–50% margin buffer, re-sample books before every entry. Illustrative: XMR dydx(maker)→binance $400/leg + BTW aster→bitget $400/leg ≈ **$664/yr ≈ 66% on $1k** at ~2.5x with buffer (both pairs 97–98% positive days over their windows).

---

## 1. Method — what was measured

- **Leg universe:** the 93 unique (venue, base) legs of the 69 pairs that cleared Part 4's persistence screens (E1 persistent cohort pos ≥ 95%: 51 pairs; dYdX-short net-30d > 15%: 18 pairs).
- **Endpoints:** Binance `fapi/v1/depth` (1,000 levels), Bybit v5 `orderbook` (200, short keys `b`/`a`), OKX v5 `books` (400), Aster `fapi/v1/depth` (500, Binance-clone, symbol map from `exchangeInfo` incl. USD1/RWA quotes), Bitget v2 `merge-depth` (150, aggregated), Hyperliquid `l2Book` (20 aggregated levels — a lower bound on depth), **dYdX v4 via `v4_orderbook` WebSocket snapshot** (full depth; the indexer has *no* REST book endpoint — `/v4/orderbooks` 404s, confirmed against `perpetualMarkets` tickers).
- **Protocol:** 3 passes ~45 s apart (one ~100 s window, 2026-09-01); per leg and side: top-of-book, spread, cumulative depth within 10/25/50/100 bps of mid, and walk-the-book VWAP slippage for notional ladder **$500 / $1k / $2.5k / $5k / $10k / $25k**; fill-completeness tracked (all 279 samples filled every ladder rung).
- **Pair cost model (per notional N, per leg):** entry = short-venue bid walk + long-venue ask walk; exit = reverse; round-trip cost = `2×(taker_short + taker_long)` + entry+exit slippage; **net APR(N, 30d) = apr_30d − cost×365/30**; breakeven days = cost ÷ daily carry. A **maker variant** re-prices the short-leg entry as post-only (Aster maker fee 0%, dYdX maker = taker tier-0, no queue penalty modeled).
- **Gate:** pass at N if both legs' per-side slippage ≤ 25 bps at N, min leg depth-25bps ≥ N, and net APR(N,30d) > 5%. `pair_cap` = max ladder notional satisfying the slippage bound on both sides of both legs.

---

## 2. Headline results

| Group (Part-4 screen) | Pairs | Net@1k median | Net@1k range | Pass $1k | Pass $10k | Caps |
|---|---|---|---|---|---|---|
| E1 micro-cap sleeve | 22 | **31.6%** | 2.8–103.7% | 14 | 1 | $500–25k |
| dYdX-short set | 18 | **29.4%** | −5.7–79.6% | 1 (TAO) | 4 (TAO×4) | 0–10k |
| E1 floor majors | 29 | 1.4% | −250–6.0% | 9 | 0 | 0–10k |

Wide-but-executable (fails 25 bps/side, still net > 15% with maker short entry): **14 pairs** — XMR dydx→{binance, bybit, bitget, aster} 52–60–79–87%, ONDO dydx→{binance, bybit, bitget, aster, hl} 20–38%, ZEC dydx→binance 21/24%, RUNE dydx→{binance, bybit, bitget} 14–20%, POWER aster→bybit 31/34%.

Cost structure of the headline pairs (taker, $1k per leg, 30d hold):

| Pair | apr30 | RT slippage | RT total | Net @1k | Breakeven | Cap |
|---|---|---|---|---|---|---|
| 龙虾 aster→binance | 110.5% | 38 bps | 0.56% | **103.7%** | 1.8 d | $2.5k |
| XMR dydx→binance | 97.7% | 129 bps | 1.50% | **79.6%** (87.2% maker) | 5.6 d | — (wide) |
| BTW aster→bitget | 86.6% | 44 bps | 0.62% | **78.8%** | 2.7 d | $5k |
| MAGMA aster→bitget | 58.4% | 36 bps | 0.54% | 51.5% | 3.5 d | $5k |
| UAI aster→binance | 48.7% | 23 bps | 0.41% | 43.7% | 3.1 d | **$25k** |
| POWER aster→binance | 43.6% | 64 bps | 0.82% | 33.6% (@10k: **−226%**) | 6.9 d | **$1k** |
| TAO dydx→binance | 36.0% | 33 bps | 0.51% | 29.4% | 5.4 d | **$10k** |
| RIVER aster→bitget | 32.2% | 30 bps | 0.48% | 26.2% | 5.6 d | $5k |
| ZEC dydx→binance | 28.9% | 44 bps | 0.62% | 21.1% | 8.1 d | $1k |
| KITE aster→binance | 20.9% | 13 bps | 0.31% | 17.0% (@10k: −17.9%) | 5.5 d | $5k |
| FIL aster→binance | 10.2% | 16 bps | 0.34% | 6.0% (maker 7.8%) | 12.3 d | $10k |

---

## 3. Sleeve-level findings

**Micro-cap sleeve (E1).** At $1k per leg the books are real: even the thinnest passer (POWER) filled $1k within ~64 bps/side, and the liquid ones (UAI $11.8k depth within 25 bps on the worst side; 龙虾 $3.1k; BTW $2.6k) leave room above the minimum. But capacity is *stepped*: doubling size often multiplies slippage 3–8× (POWER $1k→$10k: 64→2,198 bps; KITE 13→301 bps; UAI is the exception with 23→52 bps). Practical rule: **size to `pair_cap`, not to ambition**; UAI aster→binance is the only micro-cap that scales to $10k today.

**dYdX shorts.** The 125.7 bps XMR spread is a market-structure fact, not noise: dYdX's XMR book had 52 bid / 34 ask levels with real size only ~8% away from mid. Taker entry costs ~63 bps/side; the CEX legs cost 1–8 bps. Even so the carry (97.7% apr30) pays the 1.50% round-trip in 5.6 days. TAO is the standout structurally: tight enough (33 bps RT) to pass $10k with 29% net. ONDO/ZEC sit between — executable, cap-limited by the dYdX leg, materially better with maker entries. **MORPHO fails outright** (116 bps vs 10.6% carry, 46-day breakeven).

**Floor majors.** The ~9% gross structural floor nets 0.7–6.0% at $1k taker with 18–28-day breakevens — inside noise of the floor parameter itself. With Aster maker entry (0% fee, no crossing): FIL 7.8%, FET 6.6%, LDO 5.9%. At $10k, FIL aster→binance passes (3.9% net, cap $10k). Conclusion: the floor basket is confirmed real but is a **capital-parking harvest** (T-bill-plus), correctly positioned as the portfolio's low-variance sleeve for larger, patient capital — not the $1k growth engine.

**Traps caught (negative result, valuable).** ZEN's book at sampling time: 171–183 bps RT at $1k on aster→{binance, bybit} and 2,124 bps on aster→dydx — despite a clean 6.9–7.1% net-30 in Part 4. SPX aster→dydx: 1,865 bps. These names had 98–100% positive funding days and would have passed any funding-only screen. Books change by hour (ZEN proves it within one day of Part 4's snapshot) — the depth check must be **live and pre-trade**, not a one-off.

---

## 4. Paper basket and the $1k worked example

**Construction rules (from measured data):** per pair, notional ≤ `pair_cap` × 0.7; dYdX short legs always maker; 30–50% of account held as margin buffer; re-sample the two books immediately before entry; exit rule unchanged from Part 4 (episode ends when long-leg funding meets the floor).

**$1k account, ~2.5x, one window:**

| Position | Notional/leg | Margin | Net APR (basis) | Year P&L |
|---|---|---|---|---|
| XMR dydx (maker) → binance | $400 | ~$160 | 87.2% | +$349 |
| BTW aster → bitget (taker) | $400 | ~$160 | 78.8% | +$315 |
| Buffer (unrealized-BB / additions) | — | ~$680 | — | — |
| **Total** | **$800 gross** | **$1k** | — | **≈ +$664 (≈66%)** |

**$10k account alternative (more institutional):** UAI aster→binance $5k (40.2% net @5k) + TAO dydx→binance $3k (28.4%) + FIL aster→binance $2k (3.9%) ≈ $2.94k/yr ≈ **29% on capital**, all three pairs inside measured caps with buffer.

Expected slippage on unwind is already in the model (exit = measured opposite-side walk). The dominant residual risks are **funding-regime change** (floor-parameter move — kill-switch monitor, Part-4 §6.5), **leg-in during a book hole** (mitigate: re-sample pre-entry, post-only entries), and **micro-cap idiosyncratic crash** (both legs crash together — cross-margin buffer sized for it).

---

## 5. Caveats

1. **One sampling window** (~100 s on 2026-09-01, single time of day). Books move; ZEN degraded within a day. Re-sample at 2–3 different hours before finalizing sizes.
2. **Exit book assumed = entry book.** Measured both sides at the same instant; real unwind slippage will differ, especially on micro-caps after adverse moves.
3. **HL `l2Book` returns 20 aggregated levels** — HL depth figures are lower bounds (only affected: ONDO dydx→hl). Bitget depth is aggregated (`merge-depth`).
4. **dYdX-leg pairs inherit the 42-day funding window** (indexer cursor bug, Part-4 §5.1); their apr30 basis is 42d-truncated.
5. **Maker variants assume a fill at touch** with no queue-position or adverse-selection penalty; Aster maker fee 0% and dYdX tier-0 maker=taker are as-published today.
6. Fees at tier-0; no VIP/VOL tiers, no funding-timing optimization (e.g., exiting just after settlement), no borrow/margin-interest modeling.
7. Capacity here is an **L2 instantaneous bound**; open-interest, venue position limits, and margin rules can bind earlier.

## 6. Next steps

1. **Re-run the sampler at 2–3 different times of day** (Asia/EU/US sessions) → stability distribution per leg; promote/demote pairs whose RT cost varies > 2× across sessions.
2. **Wire the depth gate into the v3 data layer** as a live pre-trade check (identity guard + depth gate + floor monitor = the three tripwires before any order).
3. **Paper-basket spec**: sizing ladder per pair_cap, maker-entry logic for wide-spread legs, unwind ladder, episode-end exit — then 2–4 weeks of paper fills vs modeled slippage.
4. Event playbook (E2 + E3 + E1r cap-slams) — unchanged, queued.
5. dYdX historicalFunding cursor fix for full-90d dydx-leg coverage.
