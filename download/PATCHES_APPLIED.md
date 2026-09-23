# PATCHES_APPLIED — Option 3: the four live-blocking v2 fixes

*Applied 2026-09-01 to `upload/code_extracted/` · All work documented in `03_venue_expansion_aster_dex.md` §10.*

## Summary

| # | Bug | Root cause | Status |
|---|-----|-----------|--------|
| 1 | Exit/entry orders at `price=0.0` + naive fixed size | `main.py` built `prices = {…: 0.0}` and `sizes = allocation` for both entry and unwind | **FIXED** (engine + both adapters) |
| 2 | Exchange-name case mismatch (`Gate.io` / `Gate.Io` / `gate`) | `ex.title()` produces `Gate.Io`; feed passed display names into the engine; router looks clients up by lowercase id | **FIXED** (`core/venues.py` + feed) |
| 3 | `active_opps` stored stale snapshots | Gate re-evaluated the harvest-time snapshot forever; convergence exit could never trigger on fresh data | **FIXED** (`HeldPosition` + freshness tracking) |
| 4 | Bogus "1122.67% Net APR" harvest lines | `net_apr*100 if net_apr < 10` mis-scaled percent-denominated fields (`netAprPct=11.2267 → 1122.67`) and all negatives | **FIXED** (strict `_parse_apr_pct` + guardrail) |

## Patch 1 — protected pricing & real sizes

- **`main.py`**: new `_protected_price()` / `_collar_price()` — every order price comes from a live top-of-book (`get_top_of_book`) collared by `MAX_SLIPPAGE_PCT`, falling back to the entry fill price ± collar. Never `0.0`; if no sane price exists the entry/unwind is **skipped and retried**, not sent blind.
  - Entry (`harvester_loop`): skip cycle when a leg cannot be priced.
  - Unwind (`_unwind_position`): sizes from the entry report's **actual `filled_size`** (fallback: allocation), close side inverted, price protected.
- **`core/interfaces/exchange.py`**: non-abstract `get_top_of_book()` default (`None`) so all adapters stay compatible.
- **`execution/ccxt_adapter.py`**: `get_top_of_book` via `fetch_order_book`; live orders with `price <= 0` refused (`error="invalid_price"`); **zero-fill orders are no longer booked as `"filled"`** (`error="zero_fill"`) — the phantom-filled bookkeeping that left naked legs after "Successfully Harvested" is gone.
- **`execution/hl_adapter.py`**: same price guard on both order paths; `get_top_of_book` from public `l2_snapshot`.

## Patch 2 — canonical venue ids

- **NEW `core/venues.py`**: `canonical_venue()` (Sharpe/display names → engine ids, `Gate.io → gate`) and `sharpe_display_name()` (engine ids → API display names; replaces `ex.title()` which produced the invalid `Gate.Io`).
- **`data/opportunity_feed.py`**: all outbound API params use `sharpe_display_name`; all inbound venue fields (`exchange`, `longExchange`, `shortExchange`, `buyExchange`, `sellExchange`, `spotVenue`, `buyVenue`, `sellVenue`) canonicalized — legs now always carry router-compatible ids.

## Patch 3 — freshness-tracked positions

- **`main.py`**: `HeldPosition(opp, entry_report, captured_at_ts, refreshed_at_ts)` replaces the raw opp in `active_opps`; harvester publishes `self._latest_opps = {id: (opp, ts)}` every cycle; monitor evaluates the gate against the **fresh** snapshot.
- New settings: `MAX_OPP_AGE_SECONDS` (90), `FORCE_UNWIND_IF_STALE_SECONDS` (900) — a position whose signal hasn't been seen fresh for 15 min is protectively unwound (dated/calendar positions exempt — they legitimately leave the live feed).

## Patch 4 — strict APR normalization

- **`data/opportunity_feed.py`**: `_parse_apr_pct(row, keys)` — fields ending in `pct` are already percent (used as-is); bare `apr`/`netApr` are decimals (×100); magnitude guardrail `MAX_PLAUSIBLE_APR_PCT = 1000` rejects mis-scaled rows (`return None` → row skipped, logged). Applied in all six mappers. **Root cause of the logged 1122.67% was `_map_futures_carry` reading `netAprPct` and re-multiplying** — covered by a dedicated regression test.

## Verification evidence

- `python3 -m py_compile` — clean on all 10 touched/new modules.
- `scripts/test_patches.py` — **23/23 pass**, including: `netAprPct=11.2267 → 11.2267%` (regression), `netRollApyPct=-3.2 → -3.2%`, `Gate.io → gate`, tob/entry-fill collar math, actual-fill unwind sizing (55 not 100), `price=0` refusal, zero-fill not booked as filled.
- Repo suite `pytest tests/ --ignore=tests/test_execution_parser.py` — **15/15 pass** (that file requires `eth_account`, unavailable in this environment; pre-existing, not patch-related).
- Dry-run behavior is unchanged in shape; in live mode the engine now refuses the previously-fatal patterns.

## Files touched

```
NEW   core/venues.py
MOD   main.py                          (bugs 1 & 3)
MOD   config/settings.py               (+2 staleness settings)
MOD   core/interfaces/exchange.py      (+get_top_of_book default)
MOD   data/opportunity_feed.py         (bugs 2 & 4)
MOD   execution/ccxt_adapter.py        (bug 1)
MOD   execution/hl_adapter.py          (bug 1)
```

## Remaining known gaps (deliberate, documented)

- Limit orders are fire-and-forget (IOC on HL; resting limits on CEX legs are only reconciled by the zero-fill guard). A fill-polling/cancel-replace layer remains v3 scope per the blueprint.
- `get_top_of_book` uses one snapshot per order; a dedicated market-data microservice is v3 scope.
- Sharpe's exact field semantics are only partially documented publicly — the parser is strict-by-rejection, so API changes fail safe (rows dropped) rather than fail loud (bogus trades).
