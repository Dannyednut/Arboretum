# Part 10 — Sharpe Floor Scan (D0): New-Venue E1 Cohort + Verification Verdicts

*September 2, 2026 · Research-first phase · Scanner: `scripts/sharpe_floorscan.py` (checkpoint-resumable) · Analysis: `scripts/analyze_floorscan.py` · Verification: `scripts/verify_sharpe_claims.py`, `verify_sharpe2.py` · Data: `download/data/sharpe_newvenue_legs.csv` (1,882 legs), `sharpe_newvenue_pairs.csv` (1,822 pairs) · Checkpoint: `scripts/sharpe_hist_checkpoint.json` (179 coins) · Predecessor: Part 9 (platform landscape).*

---

## 1. What ran

This is the D0 step from Part 9: turn Sharpe's 25 uncovered venues from a *snapshot claim* (the floor census) into *persistence distributions* using the Part-4 machinery, then verify the headline claims venue-natively before anything enters a candidate basket.

Pipeline, end to end:

1. **Current book** — 5 asset-class slices of `/api/funding/rates?type=current` → 8,545 rows, 33 venues (25 new-to-us), with `base_coin`, `interval_hours`, `open_interest` per row.
2. **Candidate selection** — SHORT candidates: coins on new venues pinned at ≥0.8× their venue's modal floor (floor ≥5% APR), top 12/venue by OI×APR. LONG-COLLECT candidates: legs with APR ≤ −5% (paid-to-long, the Kraken pattern from Part 9). Result: 19 short-candidate venues, 25 long-collect venues, 202 unique coins → capped to 170 by cross-venue frequency.
3. **History pulls** — free-tier constraint discovered by `probe_sharpe_v4.py`: `exchange`/`offset`/`start_time`/`end_time` params are **silently ignored**; only `coin`/`days`/`limit` work; rows oldest-first with a hard 5,000-row cap. Workaround: a **two-slice strategy** — `days=60` (old sample; capped for dense coins) + `days=8` (recent sample, always fits) — yielding two disjoint windows per coin. Checkpointed per coin so the run survives sandbox kills.
4. **Leg metrics** — per (venue, coin): pos_frac, exact-pin fraction, near-pin fraction, median/mean interval-true APR, p10/p90, OLS slope.
5. **Pair construction** — short new-venue leg × long direct-venue leg (our 90d cache first, Sharpe slices as fallback), on a **union-hour grid with per-hour carry ffill** (`rate/interval_hours` — converting raw rates instead would have re-created the 8h-cadence over-count trap from Part 1; Binance's 0.01%/8h floor would have annualized at 87.6% instead of 10.95%).
6. **Cross-validation** — Sharpe's direct-venue series vs our own 90d cache: HYPE/Aster 79.8% exact / 98.4% near-agree, HYPE/Binance 69.8%/99.2%, HYPE/HL 76.0%/86.8% over 129–517h overlaps. Sharpe data is real; rounding/timing explains the residual.

Two bugs were caught and fixed in-flight (both now regression-documented in the scanner): a tuple-shape mismatch in the history cache, and a **units bug in the net-APR math** (round-trip cost in bps was treated as a fraction against percent-point APRs — inflating net30 by ~10×; e.g. LTC showed 52.9% net instead of the correct ~48% before further corrections below).

## 2. The verification triangle: two confirmations, one refutation

Sharpe is a single source; before trusting any of its claims we ran venue-native spot-checks against APIs reachable from the sandbox.

| Claim (from Sharpe history) | Native check | Verdict |
|---|---|---|
| Backpack pins LTC at the industry floor (+10.95% APR) | `api.backpack.exchange/api/v1/fundingRates?symbol=LTC_USDC_PERP` → `0.0000125`/h — **exactly** 1.25e-5, the industry floor | **CONFIRMED** (exact to the digit) |
| Kraken runs a broad negative-funding regime on mid-caps (COTI −344% APR, KAITO −186%, BIO, MEW, KAIA, MINA, CKB…) | `futures.kraken.com/derivatives/api/v3/tickers` → 30+ perps negative **right now** (BNB, LTC, INJ, GMX, AR, MORPHO, ORCA, ZEN…), including index/equity perps (US100, MSTRX, SPYX) at extreme values | **CONFIRMED in sign & breadth**; magnitudes are cap-event-like and the ticker unit is ambiguous → treat as event material, not basket carry |
| Gate.io LTC persistently ≈ −42% APR for 60d (making "short Backpack → long Gate" a 53%-median double-collect) | `api.gateio.ws/api/v4/futures/usdt/contracts/LTC_USDT` → funding −0.0001%/8h ≈ **0% right now** | **REFUTED as persistent** → all Gate-counter pairs **quarantined** (same missing-data pathology we found natively in Part 3; Gate history is auth-gated to us) |
| Crypto.com two-sided wild funding (CELR −284%, INJ +88%…) | v1 public tickers lack funding fields; history endpoint 404s | **PENDING** (needs their v2 authed API) |

The Gate refutation changed the results materially: with Gate excluded as a counter-leg, LTC's headline 53% median spread collapses to ~6% net. The "best counter-leg" logic had been selecting the *most negative* venue — and the most negative leg was also the least trustworthy one. Lesson recorded: **counter-leg trust must be ranked before spread size**.

## 3. The honest new-venue E1 cohort (after quarantine, after the units fix)

Stress cost used until the depth gate runs: 2×(5bps unverified taker + 5bps direct) + 20bps slip ≈ 0.40% round-trip; `net30 = median_spread − 0.40%×365/30`.

| Coin | Short (new venue) | Long (direct) | Med spread | Net@30d | Pos days | Window | Breakeven | Reachable? |
|---|---|---|---|---|---|---|---|---|
| **XAG (silver)** | Orderly | dYdX | 22.3% | **17.5%** | 99% | 41.6d | 6.5d | both yes |
| AAVE | ApeX | OKX | 19.5% | 14.6% | 93% | 59.7d | 7.5d | ApeX blocked |
| **INJ** | BingX | OKX | 18.7% | **13.9%** | 94% | 59.7d | 7.8d | both yes |
| AAVE | Backpack | OKX | 16.6% | 11.7% | 88% | 59.7d | — | both yes |
| LINK | Nado | OKX | 13.5% | 8.6% | 92% | 59.7d | 10.8d | both yes |
| BNB | Orderly / Backpack | OKX | 11.4% | 6.5% | 88% | 59.7d | — | both yes |
| **CL (crude oil)** | Orderly | OKX | 11.0% | 6.1% | 89% | 59.9d | — | both yes |
| **TSLA (stock perp)** | Orderly | Bybit | 11.0% | 6.0% | 89% | 59.0d | — | both yes |
| PENG | Nado | Binance | 10.9% | 6.1% | 97% | 33.0d | 13.3d | both yes |
| HBAR | ApeX | dYdX | 10.9% | 6.1% | 93% | 41.7d | — | ApeX blocked |
| LTC (floor-only) | Backpack/Pacifica/ApeX/Orderly/KuCoin | Binance/Bybit/OKX | ~11% | ~6% | 90-94% | 59.7d | — | mixed |

Readings:

- **Orderly is the standout discovery**: reachable from the sandbox, and it hosts crypto *and* commodity *and* stock perps — XAG, CL, TSLA all pinned at the ~11% floor while the direct-venue legs sit at ~0. That extends E1 from a crypto edge to an **RWA-carry edge**: the DEX floor convention applies to their entire universal book. (Caveat: Orderly legs are Sharpe-sourced history; native L2 + native funding history must confirm before any sizing — Orderly's own API answered during reachability probes.)
- **XAG Orderly→dYdX at 17.5% net30 with 99% positive days and a 6.5-day breakeven is the single best new candidate** — a silver perp short-carry vs a dYdX leg that sat at exactly 0. If it survives the depth gate it slots straight into the S1 sleeve at $1k sizing.
- **The floor-majors sleeve gains five venues** (Backpack, Orderly, BitMEX, KuCoin, WhiteBIT + blocked-but-listed Pacifica/ApeX): BNB/LTC/LINK pin at 10.95–13.4% across all of them, confirmed by the Backpack native check. This diversifies the S3 "parking" strategy away from Aster/HL-only floor risk.
- **INJ BingX→OKX (13.9% net30) is doubly collect**: OKX's INJ leg was itself negative (−8.4% median) — the "double collect" pattern inside our direct venues, no new-venue data risk on the long side.
- **LTC's phantom 53% is dead**; floor-only LTC nets ~6% — still a valid floor-majors row, but the earlier number was a Gate artifact.
- Blocked-venue floors (Pacifica 85% pin, Extended +11.39%, ApeX) remain paper-only until we get network access; they stay in the census, not the basket.

## 4. The negative-funding event cohort (E1r-grade, playbook only)

1,620 pairs have a new-venue leg that *pays longs* — concentrated on Kraken (62 legs, med-of-med spread 14.7%), Crypto.com (81), Variational (93), HTX (88), Extended (70). Headlines: COTI long-Kraken −344% APR leg, CELR long-Crypto.com −284%, AAVE long-HTX −126% (paired with our own aster/HL shorts at ~+11% — both sides collect).

These are exactly the LA/PROM cap-slam pattern from Part 4: violent, mean-reverting, sometimes capped. With Sharpe magnitudes unverified natively (Kraken's ticker unit ambiguous; Crypto.com pending), this cohort goes to the **E1r event playbook** (S4) as *names to monitor*, not basket rows. The actionable piece: Kraken and Crypto.com and HTX are all reachable natively — a daily negative-funding sweep on their live tickers (sign-only, unit-free) is a cheap S4 trigger feed.

## 5. Method and trust notes

- **Free-tier limits**: ~4–5s per history pull → 170 coins ≈ 20 min wall clock (checkpointed across sandbox kills); `days` windows are anchored to now, so the two-slice coverage has a gap for dense coins (244 pairs <10d coverage — flagged in `days` column; 1,073 pairs have 50–60d).
- **Fees for new venues are unverified** (5bps/side placeholder) and slippage is a flat 20bps stress — both get replaced by the Part-5 walk-the-book measurement before anything is tradable. Breakevens here are optimistic bounds.
- **Identity guard not yet run across new venues**: BitMEX rows include inverse contracts (duplicate BNB legs with different margining), and symbol conventions differ. Pre-trade tripwire #1 applies as always.
- **What Sharpe changed in our funnel**: it replaced ~25 venue probes for *discovery*, surfaced 4 actionable pairs + a 5-venue floor-majors expansion in one afternoon, and caught the Gate pathology early because its history let us compute a median that the live check could refute. Discovery and verification are now separate, checkable steps.

## 6. Next steps

1. **Depth-gate the four actionable pairs** (XAG Orderly→dYdX, INJ BingX→OKX, AAVE Backpack→OKX, LINK Nado→OKX) with the Part-5 walk-the-book sampler — all venues reachable; Orderly/BingX/Nado/Backpack L2 endpoints need one probe each.
2. **Fee verification** for Backpack/Orderly/BingX/Nado taker (docs or fee endpoints) to replace the 5bps placeholder.
3. **Native history pulls** for Orderly XAG/CL/TSLA and Backpack (confirm the 60d Sharpe view leg-by-leg), and Kraken `historicalfunding` for 2–3 of the negative names to resolve the unit question.
4. **S4 trigger feed**: daily sign-scan of Kraken/Crypto.com/HTX live funding (reachable) for negative cap-slams on coins we can short elsewhere.
5. **Part 7 finalization** remains pending on the multisession windows (W03 asia-night collected; W04–W06 attempts ongoing) — unchanged plan: rerun `analyze_stability.py` when windows are in.
