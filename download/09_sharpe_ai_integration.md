# Part 9 — Sharpe AI Platform: Preprocessed Data Landscape & Integration Map

*September 2026 · Research-first phase · Probes: `scripts/probe_sharpe_v2.py`, `probe_sharpe_v3.py` · OpenAPI: `scripts/sharpe_openapi.json` · Census: `download/data/sharpe_venue_floor_census.csv` · Task-1 predecessor: `probe_sharpe.py` (v1 auth-gated, now superseded by free-tier discovery).*

---

## 1. What the platform is

Sharpe Terminal (sharpe.ai) is a crypto derivatives intelligence platform with a documented REST API: **46 versioned endpoints** (`/api/v1/*`, Bearer key, RFC 9457 errors, `{data, meta}` envelope) plus a **free tier** (`/api/*` — the same endpoints powering their frontend, no key, CORS-open, edge-cached 1 min–1 h, raw JSON). Public OpenAPI 3.0 spec at `https://www.sharpe.ai/openapi.json`. They also ship a CLI, an MCP server ("agent-ready"), and a **Data Ocean** (governed dataset queries via `/v1/analytics/query` + catalog at `/v1/meta/datasets` — the two `meta` endpoints are documented as auth-free but 404'd on the free mirror during probing; they appear to need the v1 key path or a different free route).

Coverage claim, verified live: **funding rates across 33 perpetual venues** (CEXs and perp DEXs), **futures analytics across 10 exchanges** (OI, volume, long/short, basis; liquidations on the top four), heatmaps, correlations (crypto + TradFi), narratives, listings, and preprocessed arbitrage tables — with **provenance headers on every row** (`X-Data-As-Of`, `X-Data-Stale`, `X-Freshness-SLA`, `X-Data-Source`) and `age_seconds`/`is_stale` fields inside payloads. That provenance discipline matters to us: it plugs directly into our staleness-quarantine rule from Part 3.

## 2. What we verified live (no key)

| Endpoint (free) | Status | What it gave us |
|---|---|---|
| `/api/funding/rates?type=current` | 200 | 5,000+ rows, 33 venues, per-row `interval_hours`, `margin_type`, `mark_price`, `open_interest`, `predicted_rate` |
| `/api/funding/rates?type=history&coin=XMR&days=1095` | 200 | rows back to **2025-03** — the 3-year lookback is real (paged; free page capped, cursor needed for full depth) |
| `/api/funding/rates?asset_class=equity` | 200 | **3,319 equity-perp rows across 26 venues** (BingX 464, Gate 375, MEXC 342, Bitget 226…); commodity 231, fx 86, index 80 rows |
| `/api/funding/coins` | 200 | 1,856 distinct base coins in the funding book |
| `/api/funding/settlement` | 200 | **dollars actually exchanged at settlement** (`fee_usd = OI × rate`), long_paid/short_paid/net per 1d/3d/7d windows |
| `/api/rwa-perps/rates` | 200 | **1,825 rows across 19 registry-driven venues**, interval-true `funding_apr`, borrow APR kept separate, HIP-3 builder markets (Paragon `para:10Y`), `preipo` asset class |
| `/api/arbitrage/cross-exchange` | 200 | 1,024 pre-ranked pairs: gross + fee-adjusted `netApr`, `spreadRate` **from real order books** (`spreadSource=book`), `executableDepthUsd`, `holdingDays`, `executionStatus` |
| `/api/futures/data?chart=crowding-risk` | 200 | 20k rows/coin (funding history, OI, L/S) — kill-switch feed |
| `/api/listings/recent` | 200 | 90-day listing feed with narrative slugs (caught BTW's Aug-24 Gate listing — our own micro-cap star) |
| v1-only during probe | 404/401 | `meta/coverage`, `meta/datasets`, `market/derivatives-overview` |

Auth model confirmed: free tier needs nothing; v1 needs `Authorization: Bearer` / `X-API-Key` (our Task-1 probe hit exactly this wall).

## 3. The two big discoveries

### 3.1 The funding floor is an industry convention, not a DEX quirk

We computed each venue's modal funding rate (interval-true annualization) and the share of its book pinned at that modal value:

| Venue | Type | Modal APR | % of book at modal | Cadence | We cover it? |
|---|---|---|---|---|---|
| BitMEX | CEX | +10.95% | 93% | 8h | NO |
| **Pacifica** | perp DEX | +10.95% | **85%** | 1h | NO |
| Hyperliquid | perp DEX | +10.95% | 84% | 1h | yes |
| dYdX | perp DEX | 0% | 81% | 1h | yes |
| WhiteBIT | CEX | +10.95% | 52% | 1/4/8h | NO |
| Backpack | perp DEX | +10.95% | 50% | 1h | NO |
| KuCoin | CEX | +10.95% | 48% | 1/4/8h | NO |
| Nado | perp DEX | +10.95% | 44% | 1h | NO |
| Variational | perp DEX | +10.95% | 44% | 1/4/8h | NO |
| **Extended** | perp DEX | **+11.39%** | 44% | 1h | NO |
| MEXC / BingX / Bitunix / LBank / edgeX / ApeX / Orderly | mixed | +10.95% | 25–41% | 1/4/8h | NO |
| **Lighter** | perp DEX | **+3.50%** | 35% | 1h | NO |
| **tradeXYZ** | perp DEX | **+5.48%** | 28% | 1h | NO |
| GRVT | perp DEX | 0% | 35% | 4/8h | NO |
| **Kraken** | CEX | **−10.22%** | 1% | 1h | NO |

Three readings: (1) the +10.95% APR modal is the **de-facto industry neutral-rate default** (0.00125%/h, 0.005%/4h, 0.01%/8h are the same number) — E1's edge is that DEX floors *pin* there while CEX defaults merely *anchor* there; proving pin-vs-anchor per venue is exactly a persistence-scan job. (2) **Pacifica (85% pin, hourly) and Extended (+11.39%, hourly) are the top new E1-short candidates**; Lighter (+3.5%) and tradeXYZ (+5.5%) are *low-floor* variants — weaker short-side carry but possibly excellent cheap long legs. (3) **Kraken's hourly book has a negative modal (−10.2%)** — a candidate *paid-to-long* venue: short-Aster + long-Kraken would collect on **both legs** if persistence holds.

### 3.2 The RWA/stock-perp universe is 25× bigger than our Part-2/3 view

Our E2/E3 work covered Aster's ~121 stock bases. Sharpe's registry shows **1,825 RWA-perp markets across 19 venues** — Gate (375), Bitget (304), Binance (181), Bybit, OKX, Aster, Hyperliquid HIP-3 (103), Lighter, GRVT, **Ostium** (fx/commodity), Orderly, ApeX, Coinbase, Avantis, **Paragon** (builder-set treasury markets), Pacifica, Kraken, Kinetiq — including `preipo` (SpaceX-type), `etf`, `commodity`, `fx`, `index` classes, with interval-true APR annualization (they handle the settlement-interval trap the same way we do) and borrow-based venues correctly split out (`borrow_apr_annual` never mixed into funding).

## 4. Integration map into our edge framework

| Our need | Sharpe product | Integration play |
|---|---|---|
| **E1 venue expansion** (25 uncovered venues) | funding/rates current+history, 3y lookback | New scan: `sharpe_floorscan.py` — persistence distributions per venue×coin from Sharpe history (pin-vs-anchor test), feeding new candidate legs into our depth gate. No connector engineering: one API replaces ~25 probes; MEXC/Lighter/edgeX were unreachable from our sandbox — Sharpe bypasses that for *discovery* |
| **E2/E3 event playbook** | listings/events (34 venues, prelaunch/delisting/suspension lifecycle), rwa-perps history+stats | Automated trigger feed + a monitor we lacked: **delisting alerts on held legs** (legging-risk event) |
| **Floor-regime kill-switch** | 1,095-day funding history | Backtest floor stability across a full cycle: has Aster's floor parameter ever moved? When Extended changed +11.39%? This *is* the Part-4 §6.5 monitor, pre-populated with 3 years of evidence |
| **Sleeve selection quality** | funding/settlement (dollar-weighted) | Rank candidates by harvestable dollars (`OI × rate`), not rate alone — filters out thin books with pretty APRs |
| **Candidate lead-gen + benchmark** | arbitrage/cross-exchange (1,024 rows, book spreads, executable depth) | Daily diff vs our scanner: their finds → our persistence+depth gates; our finds → names their no-persistence screen missed (their `netApr=0` rows are no-book conservatisms, e.g. ONG 40.1% net with books is live *now* but likely an unproven 1-day spike) |
| **Portfolio layer** | correlation/matrix (+TradFi), futures OI/liquidations/crowding | Cross-margin correlation inputs, crowding kill-switch |
| **Pre-trade pre-filter** | `executableDepthUsd`, `spreadSource=book` | Cheap first-pass before our walk-the-book sampler (which remains mandatory — Sharpe gives one depth number, not a size-ladder VWAP) |

**What Sharpe does NOT replace:** L2 depth ladders at our exact size (Part-5/7 methodology), execution/connectivity (still need direct Aster/Binance/dYdX/HL sessions to trade), identity guarding (we cross-check Sharpe rates against venue-native rates — e.g. Sharpe's Aster floor 1.25e-5/h matched our direct measurement exactly), and our own 90d scan (Sharpe history becomes a second, independent source to validate it).

## 5. Recommended plan

- **D0 (free tier, this week):** build `sharpe_floorscan.py` — pull current book (33 venues) + 1,095-day history for the top ~200 candidates; run the Part-4 persistence distribution machinery on the new venues; produce the "new venue E1 cohort" shortlist for Part-10 depth gating. Add a daily arb-table diff and the delisting monitor to the research cron.
- **D1 (API key):** register on their key dashboard → v1 access for `meta/datasets` + `analytics/query` (Data Ocean), full `funding/settlement`, `listings/events` streaming. Evaluate tiers against rate limits (free tier is edge-cached 1–60 min — fine for research, maybe tight for pre-trade checks).
- **D2 (v3 build):** Sharpe becomes the **upstream discovery aggregator** in the data layer (one connector instead of ~33), with direct venue connections reserved for execution and pre-trade verification. The three-tripwire rule stays: identity guard (Sharpe vs venue rate agreement), live depth gate (our own walk), floor monitor (Sharpe 3y history + venue-native parameter check).

## 6. Caveats

1. Free-tier rate limits are undocumented in what we probed; edge caching (1–60 min) makes it research-grade, not execution-grade.
2. Free history pages are capped (~5,000 rows) and appear oldest-first — full-depth pulls need cursor pagination (v1 documents it; free tier behavior needs one more test).
3. Modal-rate pinning ≠ structural floor: the 90d/3y persistence scan must confirm pin-vs-anchor per venue before any new venue enters the basket.
4. Their arb table ranks by gross APR with conservative `netApr=0` when books are missing — treat it as a candidate feed, never a signal source.
5. One snapshot day; venue coverage and payload shapes can change (they ship a changelog — worth watching).
