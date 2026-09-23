# Part 19 — P4 Execution Adapter Scope
*2026-09-15 · scoped after fix-queue restoration · status: design, awaiting user API keys*

## 0. Incident that shaped this scope: sandbox rollback (Sep 14 16:25Z → Sep 15 03:34Z)

The sandbox rolled back to a ~Sep 8 snapshot: the entire Task-33 fix-queue implementation, `test_fixqueue.py`, Parts 17/18 docs and three log files were lost while `v3.db` (evidence DB) and most of the worklog survived. Window 22 caught it live: freeze KVs sat unexpired past their TTL because the running (old) code checked freezes via raw `kv_get` with no TTL logic.

**Consequences / mandates:**
1. Fix queue fully re-implemented + re-tested (33/33) and live-verified 2026-09-15 04:41Z — stale aster/bitget freezes auto-expired via the *wired* entry path (the original Task-33 implementation had the TTL logic but the re-implementation confirmed the call-site wiring was never correct).
2. Worklog Tasks 32–34 restored as `(RESTORED after rollback)` entries; Parts 17/18 recreated (marked as such).
3. **Durability mandate for P4**: the repo is now git-tracked (`git init` + initial commit on restoration). Every P4 milestone must be committed before the ops session ends — code recovery can no longer depend on sandbox persistence.

## 1. Goal

Swap simulated fills for real order placement on the same engine, without touching the state machine, gates, tripwires, kill switch or drift accounting. Capital: start ≤ $1k pilot.

**Non-goals for P4:** new strategies, new venues, latency-sensitive quoting, sub-tick execution. The edge (funding carry) is minute-scale; the adapter must be *correct and reconciled*, not fast.

## 2. The seam (why this is tractable)

Every simulated fill of every type funnels through **one write function and three primitives** in `v3_exec.py`:

| Primitive | Line | Used by | Real-mode replacement |
|---|---|---|---|
| `take_taker(con, pos, leg, venue, side, usd, book, note)` | ~389 | entry tt both legs, legging retries, force-taker, unwind tranches incl. emergency | signed market/IOC order sized to `remain_q` (C1 qty-first), report real avg px / filled qty / fee; keep `ds.walk` as *pre-trade impact estimate* only |
| `post_maker(...)` + fill detection (`advance_resting` / `progress_unwind`) | ~348 / ~830 | maker entry (nado/aster), maker-first unwind | place real limit order, persist exchange order id, replace "book crossed through price" polling with real order-status queries (REST or user stream) |
| `order_fill(con, pos, o, px, note)` | ~330 | converts resting order → fill row | "on confirmed exchange fill" handler; real fee, real px |

What stays untouched: book/funding data layer (all public endpoints), entry state machine (`seq tt` / `sm_lt`), unwind ladder, triggers (compression/floor/listings/decay), M5 gate + kill, tripwires, drift accounting.

## 3. Adapter design

```
class ExecutionAdapter (protocol):
    place_taker(venue, symbol, side, qty)        -> {oid, avg_px, filled, fee}
    place_maker(venue, symbol, side, qty, px)    -> oid
    order_status(venue, oid)                     -> {status, filled, avg_px, fee}
    cancel(venue, oid)                           -> None
    position_reconcile(venue)                    -> [{symbol, qty, side}]
    balance_reconcile(venue)                     -> {asset: free/locked}

PaperAdapter: current sim behavior (walk-the-book, touched-price maker rule)
LiveAdapter:  signed REST per venue; DRY_RUN flag logs intent w/o placing
```

- Engine calls the adapter at exactly the three primitives above; a `--mode live|paper` CLI flag selects the implementation.
- **Paper-parity harness (P4a acceptance)**: LiveAdapter in DRY_RUN shadows every paper fill for N windows; differences (slip model vs real rejection, qty rounding, min-notional) are logged and triaged before any real order.

## 4. Per-venue integration matrix (5 basket pairs)

| Venue | Signed API | Notes |
|---|---|---|
| binance (fapi) | HMAC REST | **-1003 IP bans endured today, not handled** → adapter ships with token-bucket limiter + 429/-1003 exponential backoff BEFORE first live leg; weight budget per tick |
| aster | binance-compatible REST | same signer as binance |
| okx | HMAC + passphrase | |
| bingx | HMAC REST | $100 withdrawal test venue |
| nado | signing required (native taker/maker fees known) | maker leg of LINK pair; if signing is heavy, run nado pairs taker-only first |

Reference implementations exist outside the v3 engine (`upload/code_extracted/`): `ccxt_adapter.py` (real create_order, DRY_RUN short-circuit, testnet switch), `hl_adapter.py` (SDK + wallet, refuses live without key), `router.py` + `core/interfaces/exchange.py` (clean adapter contract, legging-mismatch emergency-close collar). Port the **settings pattern** (pydantic-settings + dotenv, keys never in code) — keys arrive as trade-only, no-withdrawal, IP-allowlisted.

## 5. Schema / accounting changes

1. `exec_orders` + `exchange_oid TEXT`, `exec_positions` + `exch_synced_ts`; `paper_fills` + `pos_id INT` proper FK (currently a notes-string join — fragile, found during mapping) + `simulated INT` flag so paper and real fills share one ledger.
2. Fee tables: `pl.TAKER/MAKER` (sim) vs `vc.FEES` (verified Part-12) currently disagree; real fills report the **exchange-reported fee**, tables become estimates only.
3. `exec_positions.qty` holds short-leg qty only; long qty lives in `flags.long_qty` — reconciliation needs both (C1 snapshot already computes entry qty per leg; reuse it).
4. Reconcile the two books inside M5 before capital: legacy P0 book (`paper_v3.db`) still feeds venue caps/leverage gates — freeze it as read-only at P4 cutover.

## 6. Safety (all existing mechanisms carry over)

- Kill switch: 4 sources unchanged; add source #5 `recon_mismatch` (adapter position vs engine position divergence > qty tolerance → halt).
- Venue freezes + 24h TTL (fix3) apply to real venues identically; TW5 held-probe unwind becomes a real immediate close — C2 cost class already models it.
- Drift ledger: realized RT from real fills; OVER > 1.3x still verdicts, immediate class excluded from kill counter.
- DRY_RUN default ON; live mode requires explicit `--mode live --yes-real` + kill file halt=false + fresh heartbeat.
- Rate limiter + backoff is a **prerequisite, not a follow-up** (binance bans observed live today).

## 7. Phasing & gates

| Phase | Deliverable | Acceptance |
|---|---|---|
| P4a | Adapter protocol + PaperAdapter refactor + DRY_RUN LiveAdapter + parity harness | 3 windows parity run: DRY_RUN intents match paper fills ±model diff log; 33+32 tests green; **git tag** |
| P4b | LiveAdapter, binance+aster only (signer shared), $50 pilot per leg | 1 full round-trip per primitive; reconcile exact; drift row written; kill/freeze fire in real mode (test via forced param) |
| P4c | okx/bingx/nado + full $1k pilot | BingX withdrawal test done; 7-day auto-run like P3 |

**User gates unchanged**: trade-only API keys (no withdrawal, IP-allowlisted) + BingX $100 withdrawal test. Nothing in P4a needs keys — work can start immediately.

## 8. Open decisions for the user

1. Pilot size per leg ($50 P4b / $1k P4c) — confirm or adjust.
2. Nado: sign up for API (maker savings on LINK) vs taker-only start.
3. Withdrawal-permissioned subaccount for the BingX test only (never used by the engine).
