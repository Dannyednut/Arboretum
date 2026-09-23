# Part 20 — P4a Execution Adapter Seam: Implementation Report
*2026-09-16 · implements Part 19 §3/§7 phase P4a · status: SHIPPED, tagged `p4a`*

## 0. Session context (why this landed today)

Two sandbox rollbacks in two days (Sep 14 → Sep 15, and another before Sep 16
06:31Z) destroyed `exec_run_22.log` and the evidence DB `v3.db`. The DB was
restored from the `/tmp` snapshot (integrity ok; tripwire 2,492 / fills 16 /
positions 4 / kv 85 preserved; freeze KVs intentionally stale). Durability
now rides on the git repo (Part 19 mandate): code + docs + worklog tracked;
DBs/logs gitignored by design.

Window 23 (restored-DB shakedown) turned into an operational event: the
restored stale freeze KVs were TTL-cleared by the fixed entry path (third
live validation of fix3), and the engine opened **position #5** — INJ
bingx->okx $250/leg, both taker fills @ 06:37:50Z, entry RT 0.128% vs
median APR 0.187. The spread-inversion regime has ended for INJ at least;
entries are re-armed and merit-gated again.

## 1. What shipped (P4a)

New module `scripts/v3_exec_adapter.py` (~300 lines):

- **`ExecutionAdapter`** protocol: `place_taker / place_maker /
  on_maker_fill` + P4b surface (`order_status / cancel /
  position_reconcile / balance_reconcile`) — exactly the Part 19 §3
  contract. P4b methods raise `NotImplementedError`.
- **`PaperAdapter`**: the legacy sim behavior moved behind the protocol —
  walk-the-book via `ds.walk`, fees from `pl.TAKER`. Silent (no intent
  rows) so paper mode stays bit-identical to pre-P4a.
- **`LiveAdapter` (DRY_RUN only)**: same walk via an internal
  PaperAdapter (parity baseline), then exchange-rule checks
  (`qty_step / min_qty / min_notional / px_sanity ±2% / fee_disagree
  pl-vs-vc`), then an append-only **intent row** (JSONL,
  `download/data/exec_intents.jsonl`), then returns the paper result
  UNCHANGED. Real placement refuses with RuntimeError until P4b.
- **`IntentLog`**: append-only JSONL, never clobbers across instances.

Engine wiring (`v3_exec.py`, minimal diffs):

| Seam point | Change |
|---|---|
| `take_taker` | walk/fee math moved to adapter; engine keeps funding-interval, `write_fill`, cost_bps |
| `unwind_tranche` taker branch | same; **C1 post-walk clamps stay engine-side** (position-state logic does not belong in an exchange adapter) |
| `post_maker` | engine keeps exec_orders row + touch-price rule; adapter call added after |
| `order_fill` | engine keeps polling-based fill detection (book-crossed); adapter call added after |
| `main` | `--mode paper\|live` (default paper); live prints DRY_RUN banner |

Failure prints preserved verbatim ("no book, deferring" / "empty book
side") so log diffs stay meaningful.

## 2. Verification evidence

| Suite | Result |
|---|---|
| `test_adapter.py` (NEW, 33 checks) | 33/33 — protocol/selection, parity invariant (live≡paper on 3 orders + 2 failure paths + walk-math + fee-table identity), intent log (silent paper, append-only, maker ops, fee fields), rule checks (qty_step, min_notional, min_qty, px_sanity, fee_disagree, unknown-venue note, advisory-only), source-level wiring (all 4 seam points, no leftover engine-side walk/fee math) |
| `test_fixqueue.py` | 33/33 (see §4 time-bomb fix) |
| `test_v3_portfolio.py` | 32/32 |
| Window 23 (paper, 9 ticks) | restored-DB shakedown: freeze TTL re-clear, position #5 opened via adapter-wired `take_taker`, zero behavior drift |
| Window 24 (live DRY_RUN, 9 ticks) | quiet monitoring, banner correct, no seam traffic, exit 0 |
| Window 25 (live DRY_RUN, 9 ticks) | position #5 monitored, no triggers, real ledger untouched by drill, exit 0 |
| `parity_drill.py` (deterministic) | **DRILL PARITY: PASS** — temp-DB copy, forced unwind of #5 through 2 real ticks + direct `post_maker`/`order_fill`: 3 fills vs intents **3:3 matched, 0 unmatched, 0 leftover**; rule flags fired in real traffic |

The drill replaces "wait for organic fills" as P4a parity evidence: it
drives the actual engine code paths (not mocks) against live books, on a
throwaway DB. `parity_report.py` is the generic join tool reused at P4b
cutover (`--db --intents --since-fid`).

## 3. P4b handoff notes (findings that already matter)

1. **qty_step residual (bingx)**: real order for 46.2706 INJ rounds to
   46.3 → 0.0294 INJ unhedged. P4b adapter must round qty DOWN to step
   and the engine must carry the residual (entry_leg_qty = actually
   filled, not intended). Affects every venue; bitget/aster/nado
   placeholders live in `VENUE_RULES` until exchangeInfo fetch lands.
2. **Fee tables disagree on maker side** (Part 19 §5.2 quantified):
   binance/okx/dydx pl.MAKER 5bps vs vc 2bps. Real fills will report
   exchange fees; drift/assumed-RT math (C2) must switch to
   exchange-reported costs at cutover, not pl tables.
3. **binance -1003 IP ban persisted across sandbox IP change** (8.212.x
   → 47.57.x): the token-bucket limiter + backoff remains a P4b
   prerequisite BEFORE the first signed call.
4. Funding-interval lookup after the tick's aiohttp session closes logs
   a benign "Session is closed" (falls back to default interval) —
   pre-existing; worth a lazy-session fix in P4b.

## 4. Maintenance notes

- `test_fixqueue.py` had a time-bomb: hard-coded "fresh" freeze timestamp
  `2026-09-15T00:00:00Z` aged past the 24h TTL and failed fz_iso_fresh +
  cascaded into fz_expire_event (event count 2 vs 1). Fixed to
  time-relative (`now - 1h`). Lesson: freeze/cool-down fixtures must
  never hard-code dates.
- Adapter default at import time is `make_adapter("paper")` so test
  suites that call `unwind_tranche`/`take_taker` directly never see
  `ADAPTER=None` (caught when 30/33 fixqueue went red pre-fix).

## 5. Acceptance vs Part 19 §7

> P4a: Adapter protocol + PaperAdapter refactor + DRY_RUN LiveAdapter +
> parity harness — acceptance: 3 windows parity run: DRY_RUN intents
> match paper fills ±model diff log; 33+32 tests green; git tag.

- Protocol + refactor + DRY_RUN + harness: **shipped**.
- Tests: **33 + 33 + 32 green** (adapter suite adds 33).
- Parity: deterministic drill **PASS (3:3)**; organic-traffic windows 24/25
  ran clean but produced no seam traffic (merit-gated regime). The
  3-organic-window parity tally continues across ops pings and is a
  **P4b precondition checklist item** (drill + any organic fills must
  stay 1:1 in `parity_report`).
- Git tag: `p4a` on commit.

## 6. What P4b needs from the user (unchanged gates)

1. Trade-only API keys (no withdrawal, IP-allowlisted) — binance+aster
   first (shared signer), then okx/bingx/nado.
2. BingX $100 withdrawal test (separate withdrawal-permissioned
   subaccount, never used by the engine).
3. Decisions per Part 19 §8: pilot $50/leg at P4b, $1k at P4c; nado
   taker-only start (signing later if maker savings justify it).
   Defaults assumed per Part 19; say the word to change them.

## 7. Addendum (same day): P4b config scaffold — `.env.example` + settings layer

User green-lit the key-configuration step, shipped immediately after the tag:

- **`.env.example`** (committed template): per-venue key slots
  (binance/aster/okx+passphrase/bingx/nado) with the permission contract
  spelled out — trade-only, withdrawals DISABLED, IP-allowlisted; the only
  withdrawal-permissioned key ever allowed is the separate BingX test
  subaccount, which is deliberately absent from this file. Mode flags
  (EXEC_MODE/DRY_RUN/YES_REAL) and pilot sizing gates included.
- **`scripts/v3_settings.py`**: pydantic-settings + dotenv port of the
  reference pattern, hardened for v3: SecretStr everywhere (repr/log-safe
  by construction), loud partial-credential errors naming the missing
  fields and the .env path, okx 3-field contract, mode-consistency
  validator (YES_REAL requires live + DRY_RUN=false — real orders stay a
  P4b decision, never implicit), `credentials_status()` readiness map for
  ops, template-drift guard (`missing_from_example`).
- **Hygiene fix**: a placeholder `.env` from the initial commit was still
  git-tracked (gitignore does not apply to tracked files). Untracked it;
  historical content verified harmless (one DATABASE_URL line, no secrets).
  `.gitignore` now covers `.env`/`.env.*` with a `!.env.example` exception.
- **Adapter hook**: `LiveAdapter(settings=...)` + `credentials_ready(venue)`
  — DRY_RUN needs no keys and parity is untouched; real placement still
  refuses (P4b).

Tests: NEW `test_settings.py` 24/24; full regression stays green
(adapter 33/33, fixqueue 33/33, portfolio 32/32).

To fill in: `cp .env.example .env && chmod 600 .env`, paste trade-only
keys, then `python3 scripts/v3_settings.py` shows a per-venue readiness
map without ever printing a secret.
