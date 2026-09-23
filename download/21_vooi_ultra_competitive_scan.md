# Part 21 — Competitive Scan: VOOI Ultra (ultra.vooi.io)

Date: 2026-09-21 · Trigger: user flagged "ultra.vooi.io is doing most exactly what you're building"
Sources: ultra.vooi.io (SPA), docs.vooi.io (`llms.txt`, Ultra Overview, Arbitrage Desk, Funding Arbitrage Bot Example, Ultra Trading Fees), vooi.io/perps-api, X/GitHub snippets.

## 1. What VOOI Ultra actually is

VOOI is a **non-custodial, venue-native execution layer** across perp venues. It does not run its own
book: each venue keeps its order book, margin accounts, fees, and risk logic; VOOI standardizes access
on top. Products:

| Component | What it does |
|---|---|
| Ultra app (ultra.vooi.io) | One UI across all supported venues |
| Perps API | One API: unified orderbooks / funding / balances, smart order routing, cross-venue margin transfer (~15s). Not public-standalone — access via Ultra account token |
| MCP server | Agent-first access (Claude Code / Codex / Cursor configs in docs) |
| Ultra Tools | Funding-rate analysis, venue comparison, margin transfers |
| **Arbitrage Desk** | Cross-venue funding-arb scanner + one-click dual-leg open |
| Funding Arbitrage Bot | Open-source educational example bot on top of the API |

Supported venues (9+): Ondo, Bybit, Binance Futures, Extended, Hyperliquid (+ HIP-3 groups
trade.xyz, Kinetiq), Lighter (Robinhood Chain), MEXC, Robinhood, Aster.

## 2. Head-to-head vs our v3 engine

| Capability | VOOI Arbitrage Desk | Our v3 |
|---|---|---|
| Cross-venue funding scanner | Yes — filters: venue, token, min Volume, min OI, min P-spread, min F-spread 8h | Yes — sweep + TW1–TW6 tripwires + merit gates |
| Book-aware entry estimate | "P Spread @ Size" (depth-aware) | ds.walk book-walk at entry (same idea, ours priced into RT) |
| Spread history context | Max F / Max P over 1h / 24h columns | TW persistence checks + spread-history KV |
| Entry economics | Entry Economics panel: costs, funding income, break-even time, margin, liq prices | entry RT cost model + break-even horizon (same math, ours gates sizing) |
| Paired position lifecycle | Open both legs, monitor, exit on spread decay/reversal/max-holding/risk limits | position monitor + exit triggers + kill switch |
| Execution auditability | none shown in bot example | **intent log (JSONL) + 1:1 fills↔intents parity drill (P4a)** |
| Order-rule validation | not shown | DRY_RUN rule checks: qty_step residuals, min_qty/notional, px sanity ±2%, fee-table disagreement |
| Resilience engineering | not shown | freeze/cooldown TTL self-healing (3 live validations), venue freeze on stale data |
| Trust ladder | "start dry-run, small balances first" (advice) | **enforced**: paper → DRY_RUN → $50/leg pilot gates → $1k |
| Venue breadth | 9+ venues (incl. HIP-3, Lighter, Extended, Ondo) | 5 (binance, aster, okx, bingx, nado) |
| Margin mobility | cross-venue transfer ~15s | none (capital parked $250/leg per venue) |
| Ops model | MCP for agents (same philosophy as our ping→window ops) | agent-run windows, human ping |

Their own bot doc lists the exact risk register we've been engineering against: funding spread
reversal (INJ window), one leg filling while the other fails, venue outages, slippage, liquidation.

## 3. Fees (venue-native)

Per their fees doc, all fees are determined by the venue — VOOI adds no extra trading fee on Ultra:
Ondo promo 1/2.5 bps (std 2/5), Bybit non-VIP 2/5.5, MEXC API 4/6, Binance per account tier.
⇒ Routing through VOOI is cost-neutral vs direct venue access on those venues (verify per venue at P4c time).

## 4. Read: what this means for v3

1. **Thesis validated.** A funded team (RootData lists financing) is building a product category
   around exactly our strategy: cross-venue perp funding carry with paired legs. We are not
   chasing a mirage; we are early on the same curve.
2. **The hard parts remain hard for everyone.** Their educational bot + docs admit the risks we
   already hit live (reversal, one-leg fill, outages). Their product is discovery+execution
   convenience; discipline (parity, gates, staged trust) is still the operator's job. Our edge
   stays process, not discovery.
3. **Commoditization pressure.** One-click arb + agent routing means spreads get arped faster and
   carry windows shrink. Supports our conservatism: min-APR gates, fee-aware RT, merit sizing —
   expect fewer, shorter windows, not more.
4. **P4b decision unchanged.** VOOI Perps API is an attractive *future* path for DEX legs (one
   integration vs N SDKs, margin mobility), but it is account-gated, adds a third party, and our
   P4b pilot scope (binance+aster direct signed REST) remains the simpler first live step.
   Revisit as P4c option alongside nado.
5. **Cheap data win.** Arbitrage Desk APR/F-spread/P-spread across 9+ venues is a free cross-check
   source for our scanner coverage (read-only, no keys) — candidate for P5 data diversification.

## 5. Action items taken / proposed

- [x] Competitive scan logged (this doc) — no code or roadmap changes forced
- [x] P4b scaffold confirmed intact (Task 38: .env.example + v3_settings.py, 24/24)
- [ ] Optional: bookmark Arbitrage Desk as second-source data in scanner backlog (P5)
- [ ] Optional: revisit VOOI Perps API as DEX-leg execution path at P4c scoping
- P4b gates unchanged: user trade-only keys (binance+aster first), BingX $100 withdrawal test,
  3 organic DRY_RUN parity windows tally
