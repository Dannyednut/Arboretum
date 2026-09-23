# Part 11 — New-Venue Depth Gates: INJ & LINK Enter the Basket, Two Blocks Found

*September 2, 2026 · Research-first phase · Sampler: `scripts/depth_newvenue.py` (3 passes, walk-the-book, reuses Part-5 metrics) · Analysis: `scripts/analyze_newvenue_gates.py` · Data: `download/data/l2_depth_samples_newvenue.csv` (21 rows, 7 legs × 3 passes, 0 failures), `newvenue_pair_gates.csv` · Probes: `probe_newvenue_l2.py` + rounds 2–9 · Predecessors: Part 9 (platform), Part 10 (floor scan).*

---

## 1. What this step did

Part 10 produced an honest new-venue cohort with a placeholder cost model (5bps unverified fees, flat 20bps slippage). This step replaced the placeholders with **measurements**: we probed and built native L2 fetchers for BingX, Backpack and Nado, re-derived the real fee schedules, re-walked every order book three times at the $0.5k–$25k notional ladder, and applied the identical Part-5 gate math (25bps-per-side, 4-side depth, net-APR after measured round-trip cost). One Sharpe-discovered venue (Orderly) turned out to be auth-gated at the book level — its flagship pair is reported as provisional rather than faked.

## 2. Connectivity and fee verification (the placeholder killers)

| Venue | L2 book (native) | Taker fee | Fee source | Verdict |
|---|---|---|---|---|
| BingX | `openApi/swap/v2/quote/depth`, 265–350 levels | **5.0bps** | published VIP0 schedule | usable |
| Nado | `gateway.prod.nado.xyz/v1/query` → `market_liquidity`, fixed-point 1e18 | **3.5bps** (maker 1.0) | **native** — `taker_fee_rate_x18` field per market, read live | usable |
| Backpack | `api.backpack.exchange/api/v1/depth` (~50 levels/side) | **9.5bps** (maker 8.5) | tier table (support pages; 3rd-party aggregation — needs app-level confirmation) | usable but expensive |
| Orderly | `/v1/orderbook` requires `orderly-key` header — **auth-gated** | ~3.0bps base (broker-configurable; some builders 1bp, maker rebates) | Orderly's own Perp-Anything docs | **discovery-only until key** |

Two connection notes for the v3 build: Nado is a Vertex-style typed-query gateway (the 422 error helpfully enumerates all valid `type` values — product symbols, product IDs and fees come from one `symbols` call), and Backpack caps book depth at ~50 levels per side, which is adequate at $1k–$10k but caps our view of their true top-of-book resilience.

## 3. The gate results: two new $10k-capable pairs

Median of 3 passes; fees = 2×(short+long taker); net APR = 60d-median spread − measured RT cost × 365/30.

| Pair | apr30 | RT slip @1k | RT cost @1k | **Net @1k** | Net @10k | Breakeven | Cap (short/long) | Verdict |
|---|---|---|---|---|---|---|---|---|
| **INJ BingX→OKX** | 18.7% | 18bps | 0.38% | **14.1%** | **13.8%** | 7.4d | ≥$25k / ≥$25k | **PASS 1k + 10k** |
| **LINK Nado→OKX** | 13.5% | 2bps | 0.21% | **11.1%** | **10.3%** | 5.3d | ≥$25k / ≥$25k | **PASS 1k + 10k** |
| AAVE Backpack→OKX | 16.6% | 7,216bps | — | — | — | — | 0 / $25k | **FAIL — hollow book** |
| XAG Orderly→dYdX | 22.3% | n/a | — | — | — | — | — | **PROVISIONAL — no native book** |

Leg-level detail worth keeping:

- **Nado LINK book is the tightest we have measured anywhere**: 0.9bps spread, 1.1bps slippage at $1k, 2.4bps at $25k, $183k within 25bps. Its book quality beats OKX's own LINK book (0.9bps spread but 3.0bps slip at $10k). For a "perp DEX floors are thin" prior, this is the counterexample that makes the S1 sleeve work.
- **BingX INJ is wide but deep**: 14.8bps spread, yet only 7.4bps walk slippage through $25k with $933k–$1.19M inside 25bps. The wide touch is a quoting style, not a thin book.
- **dYdX XAG (silver)**: 30–31bps spread, 15.7bps slip at $1k, cap $10k, depth25 $12k — tradable at $1k, marginal at $10k. Pass 1 caught a 188bps spread (WS snapshot timing) that passes 2–3 did not confirm — exactly why we median 3 passes.

## 4. The live-vs-median divergence board (why entry must be conditional)

Native guard probes captured live funding on every leg at sampling time (~01:40 UTC Sep 2) and compared it to the Sharpe 60d medians:

| Leg | Live (annualized) | Sharpe 60d median | Reading |
|---|---|---|---|
| BingX INJ | **+17.9%** (0.000164/8h) | +10.3% | elevated, aligned with pair |
| OKX INJ | −1.2% | −8.4% | negative side weakened; spread still **+19.1% live** |
| OKX LINK | +8.9% | −2.5% | **sign flip — live spread compressed to ~+2%** |
| Backpack AAVE | −2.0%/h regime | +8.0% | inverted; and the bid book is hollow (see below) |
| OKX AAVE | +10.9% | −8.5% | **inverted — the pair currently costs ~13% to hold** |
| Orderly XAG | +10.95% (floor, 5e-5/4h) | +22.3% leg | floor intact ✓ |
| dYdX XAG | **+44.8%** (5.11e-5/h next) | 0.0% | premium event on the long leg — spread −33.8% live |

Three lessons, all feeding the v3 spec: (1) **INJ is live-aligned right now** — the only pair of the four where entry would be profitable today; (2) LINK's long leg flipped sign since the Sharpe window — the 8.6% net30 is a *distribution*, entered only when the live spread check passes; (3) XAG/AAVE live spreads are negative *because individual legs spike* — the same cap-event dynamics that create our E1r playbook operate on the long legs too. **The pre-trade live spread check (tripwire #1b) is now a measured necessity, not a formality.**

## 5. The AAVE hollow book, caught live

Backpack's AAVE book currently shows best bid **$58.57** against best ask **$125.80** while OKX trades at $125.78 — a 73% internal gap from stale far-below-market bids. Shorting $1k into those bids would have cost ~3,600bps of entry slippage. The Sharpe funding claim (Backpack pins AAVE near its floor, 16.6% median spread) is untouched by this; the *book* is the problem, and the walk-the-book gate caught it in one pass. Disposition: AAVE Backpack→OKX moves to the event monitor list, re-gated live if the bid side recovers. Note also that Backpack's real fee (9.5bps taker) would raise the pair's RT fees from the 20bps stress assumption to ~29bps even if the book heals — the pair was already the weakest of the four.

## 6. Basket impact (worst-case framing, pending Part-7 finalization)

- **$10k tier now has three candidates** instead of one: TAO dYdX→Binance (Part 7), **INJ BingX→OKX (13.8% net@10k)**, **LINK Nado→OKX (10.3% net@10k)**. Even if Part-7 stability finalization downgrades TAO, the basket survives on INJ+LINK (~$12k/yr combined at $10k×2 legs if both run, i.e. ~60–70% blended before diversification haircuts).
- **$1k tier gains two rows** with the best breakevens measured so far (5.3–7.4 days vs 8–11 for the incumbents).
- **Venue-risk note for v3**: INJ parks $5–10k of short collateral on BingX — a lower-tier CEX. This is the first pair where *counterparty* risk, not market microstructure, is the binding constraint; the v3 portfolio layer needs a per-venue exposure cap (suggest ≤$5k on venues outside the top-4 trust tier until withdrawal tests are run).
- New-venue rows entering the basket inherit a stricter tripwire set until they accumulate 30 days of live paper fills: identity guard (BingX/OKX INJ marks ≤5% deviation ✓ observed), live depth re-walk, and now a **live spread-vs-median check** (reject entry if live spread < 40% of the 60d median).

## 7. What's blocked and the unblock path

1. **Orderly book (blocks XAG fully and CL/TSLA partially)**: the REST book needs `orderly-key`. Unblock = D1 API key registration (their frontends operate with broker keys; partner-broker signup is the documented path). Until then Sharpe's `executableDepthUsd` remains the only depth signal for Orderly legs and they stay paper-only. XAG itself remains attractive (floor verified live at exactly +10.95% APR, OI ~$10M) — this is an access problem, not an edge problem.
2. **Nado native funding feed**: `market_liquidity` gives the book; funding rate history needs their indexer (`indexer.prod.nado.xyz`) — one more probe in the next session so LINK's long leg can be monitored natively instead of through Sharpe.
3. **Backpack fee confirmation at app level** (9.5bps is tier-table sourced): resolve when anyone opens a Backpack account; does not block the current basket since AAVE is out anyway.

## 8. Next steps

1. **Part-7 finalization** remains queued on the multisession windows (W04–W06 target times 04:01/08:01/12:01 UTC today — the background daemon keeps being killed by sandbox restarts, so windows are collected with foreground sweeps when the session is active; the append-only CSV preserves everything already collected).
2. **Nado indexer probe** → native funding for LINK (and the other Nado legs: PENG-PERP vs PENGU-PERP symbol ambiguity from Part 10 resolves to two distinct markets — Sharpe's "PENG" row needs re-checking against both).
3. **INJ + LINK paper entries**: record both in the paper-basket ledger at the next aligned live-spread window, with maker/taker fill comparison (Nado maker at 1bp makes a post-only short-leg entry the default execution there).
4. **Orderly D1 key** → XAG Orderly→dYdX native gate (the single highest-net candidate still unverified: 17.5% net30, 99% positive days).
