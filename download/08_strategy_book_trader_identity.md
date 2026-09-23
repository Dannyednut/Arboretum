# Part 8 — The Strategy Book: What v3 Trades, and What Kind of Trader It Is

*September 2026 · Written while the multi-session daemon runs · Synthesizes Parts 1–7 into the system's strategy stack and trading identity. This is the seed spec for the v3 execution build.*

---

## 1. The strategy stack (what the system actually implements)

The system runs a **barbell of delta-neutral funding-carry strategies**, each with its own sizing, entry protocol and kill conditions. Every position is a pair: short the hot-funding venue, long the cheap venue, price-neutral, collect the funding differential.

### Strategy 1 — E1 Micro-cap Floor Sleeve (the growth engine)
- **Mechanism:** Aster's hourly-settled contracts pin funding at a structural floor (+10.95% APR: 225/338 hourly contracts at exactly 0.000125%/h). Micro-caps on top of the floor run 25–110% gross. Short Aster, long Binance/Bybit/Bitget.
- **Universe:** Part-4 persistent cohort (pos_day_frac ≥ 95% over 90d), depth-gated (Part 5/7). Current passers: 龙虾, BTW, MAGMA, UAI, POWER, RIVER, KITE (+ rotating).
- **Sizing:** ≤ 0.7 × pair_cap (stable-cap, worst window); $500–$3.5k/leg typical.
- **Entry:** taker both legs when combined RT cost ≤ ~25 bps/side; maker on the Aster leg (0% fee) when the book allows standing at touch.
- **Exit:** episode rule — long-leg funding meets the floor / spread compresses for 2+ settlements; or kill conditions (book re-sample fails, floor parameter moves).
- **Expected:** 25–100% net APR per position, breakevens 2–7 days, capacity-limited. This sleeve cannot scale past ~$10–25k — that's a feature, not a bug.

### Strategy 2 — dYdX Maker Short (the opportunistic satellite)
- **Mechanism:** dYdX periodically runs extreme positive funding (XMR ~+100% APR, positive 98% of days, 42d+ episodes). Its books are wide (XMR 125 bps spread vs 0.19 on Binance), so the short leg is entered **post-only at touch** — never pay the spread.
- **Universe:** dYdX-short pairs net > 15% after measured costs; currently XMR, TAO, ONDO, ZEC, RUNE.
- **Sizing:** $400–$2k/leg; TAO is the only one that scales toward $10k.
- **Entry:** maker on dYdX (willing to wait hours; no chasing), taker on the CEX leg. If the maker doesn't fill within the session, skip — the trade repeats tomorrow.
- **Expected:** 30–87% net APR, breakeven 5.6 days (XMR), low correlation to Strategy 1 timing.

### Strategy 3 — Floor Majors Parking Sleeve (the bond)
- **Mechanism:** same E1 plumbing on liquid names (FIL, FET, KAS, AR, ICP): ~9–11% gross floor carry, 100% positive days over 90d.
- **Sizing & fees:** only viable with Aster maker entries (0% fee) and/or larger notionals; FIL 5–8% net, breakevens 12–28 days.
- **Expected:** T-bill-plus. Low variance, no growth. This is where capital sits between opportunities and where a larger account parks.

### Strategy 4 — E1r Reverse Cap-Slam (event-flip, alert-driven)
- **Mechanism:** when a floor venue's funding slams to its **negative** cap (LA, PROM hit −2%/interval), the trade inverts: long Aster, short the CEX. Per-interval payments are large but episodes are short.
- **Mode:** monitor-only until triggered; small fixed size per event; exit when funding normalizes. No standing inventory.

### Strategy 5 — E2/E3 Event Playbook (speculative satellite, last)
- **Mechanism:** tokenized-stock/RWA perps and fresh listings produce two-sided funding spikes (SAMSUNG ±0.6%/4h; SpaceX pre-IPO; HK listings). Median carry is zero — the edge is catching spike *episodes*, not holding.
- **Mode:** alert-driven, pre-planned entry checklist (identity guard + depth gate + funding sign + open-interest check), tight caps, fast exits. Paper-only until it proves itself.

### Explicitly excluded (the negative space)
Cross-exchange spot arb (0.01–0.02% spreads vs 0.2% costs), quarterly basis (4.5%/yr — loses to T-bills), same-venue basis, chasing live >100% APR prints (mean-revert to 6–30%), anything directional, anything requiring speed.

## 2. The operating loop (how it trades, day to day)

1. **Scan continuously:** funding persistence distributions (90d), refreshed; candidate queue ranked by net-after-measured-cost.
2. **Three tripwires before ANY order:** identity guard (same asset both legs, marks agree ≤5%) → **live depth gate** (walk-the-book on both legs, both sides, at intended size — ZEN/SPX/POWER taught us this must be re-run every time) → floor monitor (venue rate parameter unchanged).
3. **Size to the book:** ≤ 0.7 × measured cap, 30–50% of account as margin buffer, total leverage ≤ ~2.5×.
4. **Enter patiently:** maker on wide/illiquid legs, taker only on liquid ones; never leg in during a book hole; both legs within seconds of each other.
5. **Harvest & monitor:** funding drips in per settlement (hourly on Aster/dYdX); monitor episode health, floor parameter, book depth at position size.
6. **Exit by rule:** episode end, cost-basis breach (spread collapses so re-entry is better than holding), or kill-switch. Unwind via ladder, never one market order into a thin book.

## 3. What kind of trader is it?

**Archetype: a carry desk in a vending-machine business.** If the system were a person, it's an actuary who owns vending machines: it measures foot traffic for 90 days before buying a machine (persistence scan), checks each machine before refilling it (live depth gate), never stocks more than the shelves hold (pair_cap), keeps a third of its cash unspent (margin buffer), and doesn't chase rumors of better locations (no APR chasing). It collects coins hourly. It does not gamble on the weather.

Concretely, its personality traits:

- **Delta-neutral landlord, not a directional trader.** It has no opinion on whether XMR goes up or down; it rents out the funding differential. Its equity curve is a **staircase of small hourly drips** with sawtooth teeth at entries/exits (costs), not a rollercoaster.
- **A taker of structure, not of risk.** Its edge is a venue's rate formula (the Aster floor, dYdX skew) — something that persists 90+ days and re-sets hourly — plus small size and discipline. Not speed, not information, not prediction.
- **Patient to a fault.** Maker orders that may wait hours. If the fill doesn't come, it shrugs — the same trade usually exists tomorrow. Its holding periods are days-to-weeks (median episodes 5–33 days), not minutes.
- **Paranoid by protocol.** Re-verifies the world before every single order (identity, depth, floor). It assumes every book is lying until walked. It has pre-signed kill rules for the day the venue changes its funding parameter — because the floor is a settings file, and settings files get edited.
- **Boring on purpose.** It watched a 1122% APR print in its own ancestor's logs and called it a math bug. It refuses 340% live APRs because it knows they mean-revert. Its best week looks like +1.5% and its worst week like −0.5% plus a cost bleed — and it likes it that way.
- **Small and proud.** Its capacity ceiling (~$10–25k across the whole opportunity set at current book depths) is a moat: it operates exactly where large funds can't be bothered and degens get chopped up by spreads. If you hand it $1M it will honestly say: I can only responsibly deploy ~$25k of this; the rest belongs in the floor-majors sleeve or elsewhere.
- **Comparables:** closest to a fixed-income relative-value / carry desk at a small prop shop; second cousin to a market-maker (it uses maker orders but doesn't quote both sides); nothing like an HFT, nothing like a directional crypto fund, and absolutely nothing like a degen yield farmer it competes with for the same venues.
- **Risk profile in one line:** steady positive carry; worst realistic day is an exit cost bleed or a funding flip caught by the kill-switch; tail risk is a micro-cap idiosyncratic crash hitting both legs — mitigated by the 30–50% buffer and pair caps, never fully eliminated.

**Expected P&L shape at $1k (from measured numbers):** roughly $1.2–1.8/day of funding drips on the working basket (≈ $450–650/yr, 45–65% on capital), minus entry/exit sawtooth, with flat spells between episodes. At $10k: ~$2.5–3k/yr (25–30%) dominated by TAO + conditional UAI + the FIL parking sleeve.

## 4. Design implication for the v3 build

The system is best understood as **four modules and a portfolio layer**: (1) scan/persist, (2) tripwires (identity + depth + floor), (3) execution styles (maker/taker per leg, unwind ladder), (4) event monitors (E1r, E2/E3) — all under a portfolio risk layer that enforces caps, buffers, and correlation limits (e.g., never two micro-caps whose legs share the same venue-pair failure mode). Every strategy above is already specified in measured numbers; the build is wiring, not discovery.
