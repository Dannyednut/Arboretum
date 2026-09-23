# Part 7 — Multi-Session Stability: Order Books Across Trading Sessions

*September 2026 · Research-first phase · Companion to Part 5 (`05_l2_depth_gate_paper_basket.md`). Sampler: `scripts/depth_multisession.py` · Analysis: `scripts/analyze_stability.py` · Data: `download/data/l2_depth_samples_ms.csv`, `stability_legs.csv`, `stability_pair_gates.csv`.*

**STATUS: FINAL — 8 windows, all sessions covered (US ×3, Asia ×2, EU ×3).** Daemon deaths cost the 04:01/08:01 UTC windows (sandbox kills background jobs between chats); manual foreground sweeps `W05b_09u_eu` (09:16), `W06_12u_eu` (13:00), `W07_16u_us` (15:49) replaced them. **Reduced-window finalization rationale:** the verdict counts stabilized at 12/18/39 across three consecutive re-runs (6→7→8 windows) with zero verdict changes from the 8th window; all three macro sessions are covered at least twice; the pre-registered demotion rule has fired on every borderline pair. Additional windows have diminishing information value.

---

## 2a. FINAL verdicts — 8 windows (Sep 2 16:00 UTC)

Verdicts over 8 windows (P5, W1now, W02, W03, W03b, W05b, W06, W07): **12 STABLE / 18 FLAKY / 39 FAIL** — identical to the 7-window run; the W07 US-afternoon window produced zero demotions and zero promotions. The pre-registered demotion rule fired on every borderline pair:

- **TAO dydx→{binance, bybit, bitget, aster} — FLAKY (5/8 windows).** Per-window RT@1k: `33|40|49|50|91|57|46|41` bps — the 91 bps blowout was *episodic* (W03b Asia window), not session-systematic. Verdict: conditional entry only, live depth re-sample mandatory, no standing size.
- **BTW aster→bybit and RIVER aster→bybit — STABLE → FLAKY (7/8)**: one window each where the bybit leg's 25-bps capacity dipped below size. The bybit legs are the weakest of the CEX unwind legs; bitget/binance legs hold 8/8.
- **XMR dydx taker rows FAIL 0/8.** Per-window RT@1k: `129|234|187|187|360|250|184|~190` bps — the dydx XMR book is structurally ~50–100 bps wider than when Part 5 sampled it (129 bps was the best window ever seen). Maker-entry nets still hold ~76%, but the ledger's live depth gate (walks the unwind side) kept deferring the row all day (148→78→128→120 bps sell slip at $400 across four retries).
- **The 12 STABLE (8/8 windows each), all aster-short microcaps:** BTW→bitget, MAGMA→{bitget, bybit, binance}, UAI→{binance, bitget, bybit}, RIVER→{bitget, binance}, KITE→{binance, bitget}, USELESS→binance.
- **pass_10k_stable = 0.** No unconditional $10k pair; the $10k tier rests on gated Part-11 rows (INJ bingx→okx, LINK nado→okx) + conditional TAO.
- **Like-for-like check (§5 pre-registered):** W06 (13:00 UTC) vs P5 (12:22 UTC) broadly agree — aster microcaps tight in both (BTW 34 vs 44 bps), dydx legs wide in both (XMR 181 vs 129 bps). Session time-of-day is **not** the dominant driver; intraday regime noise is → the worst-case swing gate, not session timing, is the correct defense.
- Micro-cap sleeve swing ≤ 1.5× across 8 windows; UAI trace `23|22|23|15|23|15|20|28` is the tightest book in the study; BTW→bitget trace `44|36|32|29|36|43|35|34` never left the 29–44 bps band.

---

## 1. Why this matters

Part 5's entire cost table came from ~100 seconds on one afternoon (12:22 UTC, Sep 1). ZEN's book degraded within *one day* of a Part-4 snapshot, so a single window could be flattering or slanderous. This part re-samples the same 93 legs / 69 pairs at different sessions and asks three questions:

1. Do Part-5 passers stay passers at other times of day? (**stability of the pass set**)
2. How much does a leg's slippage swing between sessions? (**swing ratio** = worst-window / median-window RT cost at $1k; > 2× = unstable book)
3. Which pairs survive a **worst-case gate** — pass in *every* window, not just on average?

## 2. Windows collected so far

| Window | UTC time | Session context |
|---|---|---|
| P5 | Sep 1 12:22–12:24 | EU morning, pre-US open (Part 5 baseline) |
| W1now | Sep 1 13:37–13:39 | US equity open (9:37 ET), 75 min after P5 |
| W02–W07 | Sep 1 16:01 → Sep 2 12:01 | daemon: US afternoon, US late, Asia ×2, EU open, EU mid |

Each window = 3 passes ~45 s apart, 93 legs, walk-the-book VWAP ladder $0.5k–$25k per side, dYdX via `v4_orderbook` WebSocket. W1now: 279/279 samples ok.

## 3. Early snapshot (P5 + W1now, kept for the record)

**Headline at the time: zero demotions.** All 25 of Part-5's $1k passers passed again in the US-open window → 25 STABLE / 0 FLAKY / 44 FAIL. This optimistic count was later cut in half as more sessions exposed capacity dips — the reason worst-case gating exists.

| Stability metric (RT cost @ $1k) | Value |
|---|---|
| Median swing across 69 pairs | **1.10×** |
| Max swing | **1.3×** (龙虾 aster→binance) |
| Pairs swinging > 2× | **0** |
| Verdict counts | 25 STABLE / 0 FLAKY / 44 FAIL |
| Pairs passing $10k in EVERY window | **4** — TAO dydx→{binance, bybit, bitget, aster} |

**What the second window already caught (the point of the exercise):**

- **UAI aster→binance — capacity is session-sensitive.** Its $25k cap from Part 5 dropped to **$10k** at US open (RT slippage at $10k slipped past 25 bps/side) and worst-side 25-bps depth dipped to $9.4k. Its $1k profile is rock solid (26–28 bps, swing 1.1×, net 43–45%). Practical read: UAI stays a $1k-workhorse and a *conditional* $10k position — size $10k only after a live pre-trade re-sample, exactly as the Part-4 rule says.
- **Micro-cap sleeve costs are low-variance.** 龙虾/BTW/MAGMA/UAI/POWER/RIVER/KITE all swung ≤ 1.3× between sessions. The books that looked real in Part 5 keep being real.
- **dYdX wide books are a market-structure constant.** XMR RT @1k: 155–184 bps in both windows (swing 1.2×); the maker-entry net worst-case holds at **72.6–76.2%** across XMR's four pairs. ONDO 30–38% maker-worst, consistent.
- **Floor majors unchanged:** FIL aster→{binance, bybit, bitget} RT @1k 16–20 bps both windows, net 5.1–7.8% — confirmed as the patient capital-parking sleeve.

## 4. FINAL worst-case paper basket

Construction rules as Part 5 (≤ 0.7 × pair_cap, 30–50% margin buffer), sized to `cap_stable` = worst window, STABLE verdicts only:

- **$1k core (2 rows, session-proof):**
  - **BTW aster→bitget $400/leg** — net worst **78.8%** (swing 1.2×, cap stable $5k, 8/8)
  - **UAI aster→binance $400/leg** — net worst **43.1%** (swing 1.3×, tightest book in study, cap stable $5k, 8/8)
  - Worst-case carry ≈ 400×0.788 + 400×0.431 = **$487/yr ≈ 49% on $1k** at 1.6× gross leverage — down from Part 5's 63% (which relied on XMR) but now provable across every session.
- **$1k satellites (entry-gated, Part-11 rows — the ledger enforces the live-spread rule):** INJ bingx→okx (net 14.1% @1k, currently OPEN in the paper ledger) + LINK nado→okx (11.1% @1k, spread currently inverted). Adding both when gates pass lifts the sleeve to ≈ **$540/yr ≈ 54%**.
- **XMR dydx→binance — DEMOTED to watchlist.** Maker-entry economics survive on paper (76% worst), but the unwind-side depth cannot currently be proven within 25 bps (0/8 windows taker; live re-walks failed all day). The paper ledger re-checks it every cycle; it re-enters the basket the day its gate passes.
- **$10k tier — conditional only:** TAO dydx→binance (net worst 27.9%, swing 1.9×, 5/8) requires a live pre-trade depth re-sample; INJ/LINK at $10k size (13.8%/10.3% net, Part-11 caps ≥$25k); UAI→binance conditional at $10k (its one soft window was US-open); FIL aster→binance as the floor-parking sleeve (net ~4.5–7%, FLAKY only on capacity dips, 10k cap stable).

## 5. Pre-registered checks — outcomes

- *Any pair failing in ≥1 window → FLAKY → demoted:* **fired** — 13 pairs demoted from Part-5's 25-passer set, including the entire TAO dYdX cohort and both bybit-leg pairs.
- *cap_stable may only shrink:* confirmed — every cap_stable ≤ Part-5 cap; UAI bybit cap fell to $1k.
- *Swing > 2× list:* 龙虾 aster→binance (3.4×), ONDO cohort (3.2–3.5×), RUNE cohort (3.5–4.1×) — all were already FAIL/conditional; no STABLE pair exceeded 1.5×.
- *Like-for-like W06-vs-P5:* broadly agree → time-of-day is not the driver; intraday regime noise is → swing gate over session clocks (validated).

## 6. Caveats

1. Final on 8 windows, not the planned 11: daemon instability cost 04:01/08:01 UTC; three manual foreground sweeps substituted. Verdict convergence (3 consecutive identical re-runs) is the documented rationale; the monitor keeps collecting on future pings and any new demotion would re-open this note.
2. Same method caveats as Part 5 §5 (exit book = entry book, HL/Bitget aggregated depth, maker-at-touch assumption, dYdX 42d funding window, L2 instantaneous capacity).
3. apr30 carry is held constant across windows from the Part-4 scan; funding regime drift is a separate monitor (Part-4 §6.5 kill-switch).
4. Verdicts use the strict 25 bps/side gate; the wide-but-executable dYdX group is tracked by its maker-variant net (`net1k_maker_worst`), which is how they'd actually be traded.
