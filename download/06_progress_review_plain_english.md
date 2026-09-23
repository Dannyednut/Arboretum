# Part 6 — Progress Review: What We've Found, In Plain English

*September 2026 · A plain-English summary of Parts 1–5: the journey, the edges, the dead ends, the opportunity space, and what's still unknown. Written before multi-session re-sampling.*

---

## 1. How far we've come (the short version)

We started with an uploaded trading bot (YieldHarvester) whose logs claimed a 1122% APR — which turned out to be a math bug, not money. Since then we've gone from "one snapshot of funding rates on 4 exchanges" to a **measured, 90-day-tested, order-book-verified opportunity map across 8 venues**, ending in a paper basket where every candidate trade has a real measured cost, a size cap, a breakeven date, and a pass/fail flag. The gap between "research says this looks good" and "we know we can actually trade it" is closed on paper. Nothing is live; nothing has paper-traded yet — that's the next phase.

## 2. The core trade, in one paragraph

Perpetual futures charge a "funding rate" every few hours: when a coin's perp trades rich, longs pay shorts. If funding on Exchange A is persistently much higher than on Exchange B, you can short the coin on A and buy the same-size position on B. Price moves cancel out between the two legs — you're left collecting the funding difference, minus trading fees and slippage. Everything we've done is answering three questions about that idea: *where is the gap persistent? how much do the costs really eat? and how big can we size before the order book pushes back?*

## 3. The edges, ranked by importance

**E1 — the DEX funding floor (the structural one).** Hyperliquid and Aster have a built-in *minimum* funding rate — shorts collect roughly +11% APR even when nothing exciting is happening, because of how the venue's rate formula works. Regular exchanges have no such floor; their funding hovers near zero. Short the floor venue, long the CEX, pocket the difference ~365 days a year. Verified over 90 days: FIL, FET, KAS, AR, ICP paid positive every single day, with flat (non-decaying) slopes — it's a settings parameter, not a fad. Two tiers exist:
- **The boring floor basket** (~9% gross, ~4–8% net with maker entries): real but only worth harvesting with patience and low fees — a "T-bill-plus" parking sleeve for larger capital. At $1k with taker fees the costs eat it alive (breakevens of 12–28 days).
- **The micro-cap sleeve** (25–100%+ net APR at $1k): same mechanism on small coins where funding runs hot — BTW 79%, MAGMA 52%, 龙虾 104%, UAI 44% net. Capacity is the catch: each pair caps out between $2.5k and $25k, and thin books punish oversizing brutally (POWER's cost went from 64 bps at $1k to 2,198 bps at $10k).

**dYdX short skew (the opportunistic one).** dYdX is a second DEX that sometimes runs extreme positive funding on specific coins — XMR held ~+98–105% APR with positive funding 98% of days. Its order books are wide (XMR spread is 125 bps vs 0.19 bps on Binance), so taker entries are expensive — but enter as maker and the carry crushes the cost: XMR dydx→binance nets ~80–87% at $1k with a 5.6-day breakeven. 18 such pairs found; 14 survive with net >15% if the dYdX leg is always maker. TAO is the standout: tight enough to pass at $10k.

**E1r — reverse cap-slams (new, queued).** When a floor venue's funding slams to its *negative* cap (LA, PROM hit −2% per interval repeatedly), the trade flips: long the DEX, short the CEX. Rare, but it's a collectable event with a rule we can automate.

**E2 — tokenized-stock / RWA perps: refuted as carry, kept as an event trade.** Samsung, SpaceX pre-IPO, gold and friends looked juicy in snapshots, but 90-day data shows their funding is zero most of the time with two-sided spikes — median realized carry ≈ 0%. Not an income stream; an alert-driven playbook (E3) for listing days and spike episodes.

**What's dead (documented so we never revisit):** cross-exchange *spot* arb (spreads 0.01–0.02% vs ~0.2% costs), quarterly futures basis (4.5%/yr — loses to T-bills), same-venue basis, and any giant snapshot APR you see live (mean-reverts to 6–30% within days).

## 4. The traps the research caught

Five coins that ranked highly on funding data alone had hollow order books — SPX would have cost 1,865 bps round-trip, ZEN 2,124 bps on one leg pair, despite 98–100% positive funding days. ZEN degraded *within one day* of our snapshot. That's why the depth gate exists, and why it must run **live before every single entry**, never as a one-off check. This is the single most important process lesson of the whole study.

## 5. The opportunity space today

- **8 venues live-pollable** (Binance, Bybit, OKX, Gate, Bitget, Hyperliquid, Aster, dYdX), 3,576 perp instruments, 1,455 clean cross-venue pairs scanned over 90 days.
- **Funnel:** 69 finalists → depth-tested with 279 book samples → **25 pass at $1k, 5 pass at $10k** (UAI aster→binance; TAO dydx→binance/bybit/bitget/aster).
- **The $1k picture:** XMR dydx(maker)→binance + BTW aster→bitget, ~$400/leg each at ~2.5x leverage with a 30–50% margin buffer ≈ **$664/yr ≈ 66% on capital** — with measured, not guessed, costs.
- **The $10k picture:** UAI $5k + TAO $3k + FIL $2k ≈ **29% on capital**, all inside measured caps.
- **Tooling built along the way:** persistence scanner (7 venues, interval-correct annualization), L2 depth sampler (incl. dYdX WebSocket books), cost/capacity gate, identity guard, and the four live-blocking bugs in the old bot are patched and regression-tested.

## 6. What we still don't know (honest gaps)

1. **One sampling window.** All depth numbers come from ~100 seconds on one afternoon. Books differ by session — that's exactly what multi-session re-sampling (next step) fixes.
2. **Exit book = entry book** was assumed; real unwind slippage after adverse moves will be worse on micro-caps.
3. **dYdX funding history is 42 days**, not 90 (indexer cursor bug) — its stats are truncated.
4. **Maker fills assumed at touch**, no queue or adverse-selection penalty.
5. **The floor is a settings file.** If Aster/HL change their funding parameter, the whole E1 edge re-prices — the kill-switch monitor from Part 4 matters.
6. **Nothing has been paper-traded.** Every number so far is a measurement or a model, not a fill log.

## 7. The road from here

1. **Multi-session re-sampling** — books at Asia/EU/US hours → per-leg stability distributions; promote/demote pairs whose costs swing >2×.
2. **Wire the tripwires into the v3 engine** — identity guard + live depth gate + floor monitor must all pass before any order.
3. **Paper-basket execution spec** — sizing to pair_cap×0.7, maker entries on wide legs, unwind ladders — then 2–4 weeks of paper fills vs modeled slippage.
4. **Event playbook** (E2 + E3 + E1r) spec.
5. **dYdX history fix** for full-90d coverage.

*Bottom line: we found one structural edge (the DEX funding floor), one opportunistic edge (dYdX shorts), one event edge in the making, and we know exactly which coins, which venues, what sizes, and what costs — because we measured them. The next phase is proving stability over time, then paper fills.*
