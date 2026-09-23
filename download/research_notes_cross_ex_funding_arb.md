# Research Notes — Cross-Exchange Perp Funding Carry & V3 Multi-Strategy Arb Architecture

_Date: 2026-09-01 (UTC) · Project: YieldHarvester (code.zip) · Scope: strategy/alpha deep dive, live-soon, $1k–$10k capital_

---

## 1. Live Market Snapshot (2026-09-01 06:25 UTC, public APIs, no keys)

Instruments scanned: Binance USDT-M 686 · Bybit linear 619 · OKX SWAP 45 sampled · Hyperliquid 177

### Top positive net funding carry (long low-funding venue / short high-funding venue, taker in+out, 7-day hold)

| Pair (long/short) | Coin | Daily funding Δ | Gross APR | Round-trip cost | Net APR @7d hold |
|---|---|---|---|---|---|
| binance/bybit | KODEX20* | 0.4012% | 146.4% | 0.210% | 135.5% |
| bybit/hyperliquid | SPX | 0.2148% | 78.4% | 0.200% | 68.0% |
| binance/bybit | ION | 0.1612% | 58.8% | 0.210% | 47.9% |
| bybit/hyperliquid | OP | 0.1088% | 39.7% | 0.200% | 29.3% |
| bybit/okx | OP | 0.1058% | 38.6% | 0.210% | 27.7% |
| binance/bybit | STX | 0.0300% | 11.0% | 0.190% | 1.0% |

\* KODEX20, TENCEN, CSOPSKHYNIX2, LGELECTRONIC, SOX etc. are tokenized-equity perps — extreme funding but thin books + regional/KYC restrictions. Treat as a separate, higher-risk universe.

- Mirror case: deeply negative funding on one venue (e.g. MEITUA −0.53%/8h on bybit side) = opportunity in the opposite direction (long the negative-funding venue), but usually coincides with squeeze events → higher basis/liquidation risk.
- Realistic sweet spot at $1k–$10k: majors/alts like OP, SPX, STX, ION class — 10–40% net APR with real depth.

### Worked example — OP, $1,000 per leg, 7-day hold
- LONG bybit perp / SHORT hyperliquid perp, $1,000 notional each side
- Funding capture: $2.148/day → $15.04/week
- Fees: entry $1.00 + exit $1.00 (taker) → net ≈ $13.04 (≈1.30% per week on $1k margin per side)
- Maker entry could roughly halve fee drag (0.02% vs 0.05%+ taker).

## 2. Structural facts confirmed by research

1. **Funding cadence differs by venue**: Hyperliquid settles **hourly** (with a 4%/hour cap); Binance/Bybit/OKX mostly 8h, but **Binance now runs 442 of 767 symbols at 4h** and a few at 1h. → Annualization MUST read per-symbol interval; assuming 3 periods/day misprices yield ~2× on 4h coins.
2. **Fee schedules (base tier, taker)**: Binance 0.050%, Bybit 0.055%, OKX 0.050%, Hyperliquid 0.045%. Makers: 0.02% CEXs, 0.015% HL.
3. **Breakeven heuristics** (from Chainstack/industry guides): spot-perp on HL needs ≳0.11%/hour with maker orders; cross-ex funding arb needs the funding Δ to clear ~0.20–0.21% round-trip taker cost amortized over the intended hold.
4. **Sharpe API**: live at `sharpe.ai/api/v1/arbitrage/*`, returns structured 401 (Bearer or X-API-Key) — bot's integration is valid; key required.
5. **Primary risks** (industry consensus + Kraken guide): **rate flip**, **basis risk** (venue-specific price divergence, esp. during squeezes), **execution costs/legging**, plus liquidation-on-one-leg, counterparty/custody, and funding-timing mismatch across venues.

## 3. Net yield math (canonical form)

```
net_apr = (f_short_per_event × periods_short/day − f_long_per_event × periods_long/day) × 365
          − (fee_in + fee_out) × 365 / expected_hold_days
          − slippage_and_impact_drag
```

Edge cases:
- HL leg: periods = 24/day; rate is hourly.
- Funding flip monitoring: exit trigger when projected forward funding Δ < exit threshold (not when realized PnL dips).
- Basis drift P&L is separate from funding: |px_A − px_B| / px in a delta-neutral pair is unrealized but converges only if you hold to normalization.

## 4. Current codebase — key findings (from code + logs + DBs)

- Two generations coexist: legacy HL VWAP basis engine (worked in dry-run, 573 risk events, 553 blocked by "pair cooldown") and current Sharpe-driven `YieldHarvesterEngine`.
- Log evidence of parsing bug: harvested "futures_carry_OKX_BTC_2026-06-19T08:00:00+00:00 with 1122.67% Net APR" — expiry ISO timestamp embedded in symbol, APR annualization bogus.
- Unwind path uses `naive_size = ALLOCATION_PER_TRADE_USD` and `price=0.0` limit orders → would fail live.
- Exchange-name case mismatch (`Gate.io` vs `gate.io`) breaks router lookup for cross-venue opps.
- `active_opps` stores stale snapshots; Factor-2 convergence exit compares stale APRs.
- Persistence layer exists but is not wired into v2 engine.
- Sharpe APR fields differ per endpoint (`netApr` decimal vs `netAprPct` pct) — the `<10` heuristic is fragile.
- Fee drag commented out in legacy `NetYieldCalculator`.

## 5. V3 architecture sketch (build-parallel)

- `strategy/` plugins with a common `Signal` protocol: `CrossExFundingStrategy`, `SpotPerpBasisStrategy`, `DatedCarryStrategy`… each emits `Signal(legs, expected_hold, funding_stream, exit_rules)`.
- `data/` direct venue adapters (Binance/Bybit/OKX via CCXT-Pro; HL native WS) — self-computed funding differentials with per-symbol interval table; Sharpe demoted to "discovery overlay", not sole source of truth.
- `execution/` order lifecycle: IOC limit with price collar → partial-fill tracker → market fallback; per-leg fill ledger; atomicity = legging budget + auto-recovery (existing router idea, hardened).
- `risk/`: per-strategy caps + global caps, margin-ratio monitor per venue (liquidation distance), funding-flip kill switch, skew reconciler (reuse anti_skew), daily loss circuit breaker.
- `persistence/`: SQLite (existing schema fits) — signals, executions, funding_accruals (finally used), risk_events.
- `ops/`: Telegram/healthcheck alerts, live PnL attribution (funding vs fees vs basis).

## 6. Phased roadmap (live soon)

- **Week 1 — Safety rails on paper**: interval-aware funding monitor, signal logger to SQLite, OP/SPX/STX-class watchlist, fix name normalization + unwind sizing (as library, tested).
- **Weeks 2–3 — Micro-live**: $100–200 per leg on 1–2 majors; IOC-with-collar execution; funding accrual tracking; daily reconciliation vs `fetch_positions`.
- **Month 2 — Scale + second strategy**: raise to $500–1k per leg across 3–5 pairs; re-enable spot-perp basis (legacy code) as second plugin; maker-entry execution.
- **Quarter — Professionalize**: backtest harness on stored funding history; tokenized-equity universe as sandboxed strategy; alerting + dashboards.

## 7. Open questions / next iterations

- Confirm Bybit/OKX symbol support + margin tier for OP-class pairs at target size.
- Decide collateral split per venue (USDT on CEXs, USDC on HL) and top-up automation.
- Whether to run HL leg via API agent wallet (already coded) in live mode.
- Backtest window: fetch 30–90 days of funding history (Binance fundingRate history endpoint is public) to validate the ≥10–40% net APR bucket persistence.
