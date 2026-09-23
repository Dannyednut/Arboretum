# Part 2 — Trading System Blueprint (research-driven v3)
**Companion to:** `market_study_all_arbs_2026-09.md` · **Status:** design, pre-build · **Capital target:** $1k–$10k, phased

---

## 1. Design principles forced by the research

1. **The scanner is the alpha.** The engine itself is commodity plumbing; edge = correctly ranked, persistence-filtered candidate pairs. Everything in the architecture serves measurement quality (identity, intervals, staleness).
2. **Funding is a cash-flow stream, not a state.** All APR math uses per-event settlement streams with per-contract intervals. The three data landmines we hit (symbol truncation, ticker collision, interval assumption) each get a dedicated, testable guard component.
3. **Research code and trading code share one data layer.** The 90-day persistence scanner we already built becomes a module inside the system, so paper results and live decisions come from the same math.
4. **Every trade is born with its exit rules.** Persistence score decay, funding-flip detection, hold-time caps — attached at entry, not improvised later.
5. **Human-approved autonomy.** At $1k–$10k with 2–5 person-days of ops per week, the system proposes, a rules gate disposes; full autonomy comes only after a quarter of clean paper + micro-live logs.

---

## 2. Architecture (five layers)

```
┌─────────────────────────────────────────────────────────────────┐
│ L5  PORTFOLIO & RISK  — allocation, correlation caps, kill switch│
├─────────────────────────────────────────────────────────────────┤
│ L4  EXECUTION       — protected legs, legging recovery, BBOM     │
├─────────────────────────────────────────────────────────────────┤
│ L3  SIGNAL          — persistence ranking, entry/exit rules      │
├─────────────────────────────────────────────────────────────────┤
│ L2  ANALYTICS       — interval math, APR engines, persistence    │
│        scanner (the research code, productized)                  │
├─────────────────────────────────────────────────────────────────┤
│ L1  DATA            — collectors, identity guard, SQLite lake,   │
│        schema-drift alarms, staleness stamps                     │
└─────────────────────────────────────────────────────────────────┘
```

### L1 — Data layer
- **Collectors** (async, per venue): funding+interval+mark (5 venues), spot mids, book depth snapshot at scan time, funding history backfill. Reuse `snapshot_v2.py` fetchers nearly verbatim — they already survived the UA/auth/schema pitfalls (browser UA for Bybit, signed-header note for Gate, `fundingRateTimestamp` rename, `fundingInfo` intervals).
- **Identity guard:** cross-venue same-asset requires mark agreement within 5%; venue symbol maps (raw↔normalized, 1000×/k× multipliers) generated at startup and cached; any new symbol requires explicit approval before trading (stock-perp vs memecoin collisions make this a hard gate).
- **Storage:** SQLite (WAL) with append-only ticks + a `pair_metrics` table written by the analytics layer; the research CSV/schema we already ship is the seed format.
- **Health:** staleness stamps on every row; schema-drift alarm on any missing/renamed field (loud failure, per the Bybit/Gate incidents); collector heartbeat dashboard (terminal or simple web page later).

### L2 — Analytics layer
- **Interval-correct APR engine** — the union-of-settlement-hours cash-flow model from `funding_history_scan.py` (`spread_metrics`), factored into a shared library. No forward-fill, ever.
- **Persistence scanner** — hourly job: recompute 7/14/30/90d APR, positive-day fraction, max-DD per (coin, venue-pair, direction); writes `pair_metrics`; exposes a ranked table.
- **Basis & stock-perp monitors** — same math, different legs (spot-perp same venue; xStock-vs-stock-perp cross product).

### L3 — Signal layer
Entry rule (all must hold):
1. 90d APR in candidate direction ≥ threshold_core (8%) or ≥ threshold_event (25% with event tag);
2. positive-day fraction ≥ 85% (core) with 14d APR ≥ 0;
3. max funding-income DD ≤ 0.5% of notional;
4. identity guard green on both venues, both marks fresh ≤90s;
5. round-trip cost / expected 14d capture ≥ 4:1;
6. combined 24h perp volume ≥ $10M (liquidity floor for our size).

Exit rules (attached at entry): funding-flip (14d rolling score < 0 for 3 consecutive days), persistence collapse (positive-day % falls under 70%), hold-time cap (core 45d, event 7d), or risk-layer kill.

### L4 — Execution layer
- IOC limit orders with price collar (max slippage vs mid at decision time); no market orders on xStocks ever.
- **Legging protocol:** leg 1 fills → arm timer (3s CEX, 5s HL); if leg 2 not filled, retry once inside collar, else unwind leg 1 and count a legging-fault (fault counter feeds the risk layer). This generalizes the v1/v2 `HedgeLeg` recovery idea with explicit budgets.
- Idempotent order client (clientOrderId = hash(strategy, pair, ts-bucket)), reconciliation sweep on restart — direct answer to v2's `price=0.0` close bug: **positions close at recorded fills, never at assumed marks**.

### L5 — Portfolio & risk layer
- Allocator: core basket 70–80% (8–10 HL-carry pairs, equal-weight $300–500/leg at $5k), satellite ≤20% (stock-perp carry), event reserve 0–10% (manually released).
- Correlation/exposure caps: ≤2 pairs sharing a venue-pair; gross notional ≤ 2× equity; per-venue margin utilization ≤ 40%; global kill switch on (a) venue staleness >60s, (b) daily loss > 2% equity, (c) legging-fault rate > 3/day, (d) funding-flip on >30% of open pairs.
- Counterparty hygiene: profit sweep to cold/self-custody weekly; max balance per venue capped.

---

## 3. Build plan (research-first, per your direction)

| Phase | Deliverable | Exit criterion |
|---|---|---|
| **P0 (now)** — data foundation | L1+L2 as a long-running collector + scanner writing `pair_metrics` hourly; daily persistence report | 2 weeks of clean, gap-free history on all 5 venues |
| **P1** — paper engine | L3 rules + simulated fills on real books; log every decision + counterfactual | 4 weeks paper: realized simulated net APR ≥ 6%/yr pace, zero identity/staleness faults |
| **P2** — micro-live | $100–200/leg on 3 core pairs, IOC+collar execution, human approves each entry | 4 weeks: legging-fault rate < 1/wk, realized funding matches prediction ±15% |
| **P3** — scale & satellite | $500–1k/leg core basket; stock-perp sleeve after its own 2-week paper; event alerts live | portfolio net APR ≥ 8% pace with max drawdown < 3% equity |
| **P4** — review | first quarterly attribution: carry vs events vs costs; decide stat-arb backlog | written memo, recalibrated thresholds |

Explicitly deferred: stat-arb/pairs (Family F), any latency-sensitive strategy, OKX/Gate as trading legs (keep as data venues until collector track record is clean).

---

## 4. What carries over from the existing codebase

- Reusable as-is (after audit): `HedgeLeg` dataclass & legging concepts, `anti_skew` idea, pair-cooldown concept (repurposed as the identity/approval gate), Sharpe API discovery integration (keep as discovery feed only).
- Replaced wholesale: NetYieldCalculator (8h assumption), naive close-at-mark sizing (`price=0.0`), snapshot-based opportunity scoring, exchange-name matching (superseded by L1 symbol maps).
- v1's 573 risk events — 553 blocked by pair-cooldown — is actually evidence the risk-manager shape was right; the inputs (stale prices, wrong math) were wrong. v3 keeps the shape, fixes the inputs.

---

## 5. Open questions to resolve during P0/P1 (research backlog)

1. **Time-of-cycle bias:** live predicted rates read ≈0 right after settlements — does directional ranking degrade in the first hour of a funding cycle? (Measure: rerun scanner at different hours; P1 logs will answer.)
2. **Maker-entry economics:** can leg 1 rest as a post-only order and cut the hurdle from 0.19–0.21% to ≈0.10% without legging-fault explosion? (Measure fault-rate vs fill-rate curve in P1 paper.)
3. **XMR-class premium persistence:** is the 13×-floor XMR premium crowding-driven and mean-reverting, or structural (privacy-coin venue scarcity)? Watch weekly; it decides whether premium names get a dedicated sleeve.
4. **Stock-perp weekend basis:** quantify the Fri-close→Mon-open xStock-vs-perp gap distribution over 8–12 weekends before enabling the satellite with real size.
5. **Ethena-style compression watch:** if CEX-vs-HL differential narrows as basis capital reaches HL (or HL adds VIP tiers), the core edge shrinks — track the basket's 30d APR as the canary; trigger = 30d basket APR < 4%.
