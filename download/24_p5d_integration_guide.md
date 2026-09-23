# Part 24 — P5d Venue Integration Guide (user-facing)

**Purpose:** everything the operator needs to turn on any venue, in one place:
what to create on each exchange, which env vars to fill, what the system
already supports, and what still gates live trading. Read with Part 23
(the research matrix) for the why; this doc is the how.

**Date:** 2026-09-22 · **Stance:** DRY_RUN everywhere. No code path in this
repo can place a real order until `DRY_RUN=false` **and** `YES_REAL=true`
**and** the venue's signer is shipped **and** you promote its ladder step.

---

## §1 Status at a glance (29 credentialed venues)

Data columns: **F** = native live funding, **B** = native book (both
key-free, already flowing into `v3_desk scan/show/history`); venues without
them are Sharpe-fed until their probes clear. **Signer** = signed-REST
implementation status. **Ceiling** = highest ladder step the code supports
today (0 data, 1 DRY_RUN, 2 testnet, 3 $50/leg, 4 $1k) — promotion is
always an operator decision (Part 23 §7).

| Venue | Class | F | B | Signer | Ceiling | Env fields |
|---|---|---|---|---|---|---|
| binance | A | ✓ | ✓ | **shipped** | 4 | `BINANCE_API_KEY/SECRET` |
| aster | B | ✓ | ✓ | **shipped** (V3 only) | 4 | `ASTER_API_KEY/SECRET` |
| okx | A | ✓ | ✓ | **shipped** | 4 | `OKX_API_KEY/SECRET/PASSPHRASE` |
| bybit | A | ✓ | ✓ | **shipped** | 4 | `BYBIT_API_KEY/SECRET` |
| bitget | A | ✓ | ✓ | pending | 1 | `BITGET_API_KEY/SECRET/PASSPHRASE` |
| bingx | A | ✓ | ✓ | pending (canonical-string verify) | 1 | `BINGX_API_KEY/SECRET` |
| hl | B | ✓ | ✓ | pending (agent EIP-712) | 2 | `HL_AGENT_ADDRESS`, `HL_AGENT_PRIVATE_KEY` |
| dydx | C | ✓ | ✓ | pending (cosmos gRPC) | 2 | `DYDX_MNEMONIC` |
| backpack | A | ✓ | ✓ | pending — **data-only by policy** | 0 | `BACKPACK_API_KEY/SECRET` |
| nado | B | Sharpe | ✓ | pending (EIP-712 gateway) | 2 | `NADO_API_KEY/SECRET` |
| orderly | B | Sharpe | Sharpe | pending (ed25519) | 1 | `ORDERLY_KEY/SECRET` |
| gate | A | ✓ | ✓ | pending | 1 | `GATE_API_KEY/SECRET` |
| kucoin | A | ✓ | ✓ | pending | 1 | `KUCOIN_API_KEY/SECRET/PASSPHRASE` |
| bitmex | A | Sharpe* | — | pending | 1 | `BITMEX_API_KEY/SECRET` |
| deribit | A | ✓ (1h) | ✓ | pending | 1 | `DERIBIT_CLIENT_ID/SECRET` |
| htx | A | ✓ | — | pending | 1 | `HTX_ACCESS_KEY`, `HTX_SECRET_KEY` |
| paradex | B | Sharpe | ✓ | pending (subkey→JWT) | 2 | `PARADEX_PRIVATE_KEY` |
| mexc | A | Sharpe | Sharpe | **shipped** (binance-style) | 1 | `MEXC_API_KEY/SECRET` |
| lighter | B | Sharpe | — | pending (SDK) | 1 | `LIGHTER_API_PRIVATE_KEY`, `_ACCOUNT_INDEX`, `_API_KEY_INDEX` |
| extended | B | Sharpe | — | pending | 1 | `EXTENDED_API_KEY/SECRET` |
| edgex | B | — | — | pending | 0 (EDGE stake) | `EDGEX_L2_PRIVATE_KEY` |
| apex | B | — | — | pending | 1 | `APEX_API_KEY/SECRET` |
| drift | C | — | — | pending (Solana) | 1 | `DRIFT_KEYPAIR_B58` |
| kraken | A | Sharpe | — | pending | 1 | `KRAKEN_API_KEY/SECRET` |
| blofin | A | Sharpe | — | pending | 1 | `BLOFIN_API_KEY/SECRET/PASSPHRASE` |
| whitebit | A | Sharpe | — | pending | 1 | `WHITEBIT_API_KEY/SECRET` |
| toobit | A | Sharpe | — | pending | 0 (watch) | `TOOBIT_API_KEY/SECRET` |
| weex | A | Sharpe | — | pending | 0 (watch) | `WEEX_API_KEY/SECRET` |
| cbintl | A | Sharpe | — | pending | 1 | `CBINTL_API_KEY/SECRET` |
| vooi | D | — | — | n/a (aggregator) | 0 | none |

\* bitmex: XBTUSDT settled 2026-09-16 and underscore symbols are rolling
out — native probe auto-recovers when an open USDT perp exists.

## §2 How a venue goes live (the ladder — same for every venue)

1. **Data (done for the ✓ columns):** public funding/books feed the desk.
2. **DRY_RUN:** fill env vars → `SignedRestClient` materializes signed
   requests and intent-logs them; nothing is sent. Run a parity window —
   `python3 -u scripts/v3_exec.py run --minutes 8 --every 20 --mode live`
   stays the engine surface; the parity tally must line up intent↔fills.
3. **Testnet** (where the venue offers one): re-run the same drills against
   the `testnet` URL in the venue spec — order lifecycle without custody.
4. **$50/leg pilot:** operator flips `EXEC_MODE=live`, keeps
   `DRY_RUN=true`, then `YES_REAL=true` **only** after the parity tally is
   green and the venue's fee/symbol checks from Part 23 §9 pass. CEX venues
   additionally get the owner-side withdrawal probe (BingX-$100 pattern).
5. **$1k book:** only after 3 clean pilot windows.

Ceiling column in §1 = which steps are *possible today* per venue; steps
never auto-advance.

## §3 Class A — CEX venues: what to create

One template everywhere: on the exchange, create an API key with **futures
trading** permission, **withdrawals disabled**, **IP allowlist** bound to
the operator IP; prefer a dedicated sub-account. Then fill the env fields
from §1 and `chmod 600 .env`.

- **binance** — reference venue; the adapter ships a token-bucket limiter
  and 429/`-1003` backoff *before* the first signed call (sandbox IPs get
  banned). USDT-M futures host `fapi.binance.com`; testnet
  `testnet.binancefuture.com`.
- **aster** — ⚠️ V1 API-key creation ended 2026-03-25: create the key via
  the **V3** flow; authenticated V3 endpoints also require the main wallet
  to have completed a deposit (since 2026-09-01). Same signer family as
  binance; testnet `testnet.asterdex.com`.
- **okx** — three fields (key, secret, passphrase generated with the key);
  demo trading available.
- **bybit** — v5; testnet `api-testnet.bybit.com`.
- **mexc** — futures API official since 2026-03-31, KYC required;
  announced maker 0.01% / taker 0.05% (cheapest verified) — re-verify at
  key time.
- **gate / kucoin** — standard keys (kucoin adds a passphrase); both have
  native data flowing already; kucoin has a sandbox.
- **bitmex** — check listing state at key time (migration, see §1 note);
  full testnet.
- **deribit** — API page issues a client id/secret pair (OAuth-style);
  majors only (BTC/ETH).
- **htx** — access key + secret key for the USDT-M linear swap API.
- **kraken** — futures API key; confirm the funding cadence for *your*
  region (8h US / 1h EEA) — the engine infers it from history regardless.
- **blofin** — three fields; cadence is dynamic, never hardcode.
- **whitebit / cbintl** — standard; cbintl needs a jurisdiction check.
- **toobit / weex** — watch-list: complete the Part 23 §9 counterparty
  checklist before promoting past data/DRY_RUN.
- **backpack** — keys exist but the venue stays data-only under the
  9.5/8.5 bps fee wall; keys are optional.

## §4 Class B — DEX scoped credentials: what to create

The iron rule: **`.env` holds the scoped credential, never the main wallet
key.** Fund each account from your main wallet; issue the delegate; hold
only the delegate.

- **hyperliquid** — create an *API wallet* (agent) on the account page;
  approve it for trading; it cannot withdraw. Store the agent private key
  in `HL_AGENT_PRIVATE_KEY` and the **main account address** in
  `HL_AGENT_ADDRESS`. Testnet exists.
- **paradex** — export a **subkey** from the wallet tab (not the main
  private key): subkeys cannot withdraw/transfer. JWTs are minted from it
  per session.
- **lighter** — create an API key at an unused index (avoid 0–3, reserved
  for their front-ends). Keys can only "secure-withdraw" back to the same
  L1 address; fast withdrawals need the L1 key we never hold. Optional
  hardening: mark the key **maker-only** (post-only path) once available
  on the account tier. Account index + API key index are integers stored
  as strings.
- **nado** — wallet-key-is-auth: link a **burner signer** address to the
  subaccount and store only signer material when Step 3 approaches;
  verify the linked signer's withdrawal scoping first (open item).
- **orderly** — register and mint an Orderly Key (ed25519) pair.
- **extended / edgex** — Extended: verify the issuance flow at key time.
  edgeX is blocked on the 1,000-EDGE API whitelist stake — a priced
  decision, not an accident.
- **apex** — system-issued key/secret; account/index fields arrive with
  the signer cohort.

## §5 Class C — raw-wallet venues: burner policy

- **dydx v4** — the dYdX-chain key is *derived deterministically* from the
  mnemonic here; it is a full-withdrawal key. Use an isolated mnemonic
  holding only the working float.
- **drift** — base58 keypair of a dedicated Solana burner; devnet for
  rehearsal. RPC reliability is part of the ops checklist.

## §6 What the engine does with these keys

- `v3_settings.py` validates completeness (partial fills fail loud),
  keeps everything SecretStr (log/repr-safe), and refuses `YES_REAL=true`
  unless `EXEC_MODE=live` **and** `DRY_RUN=false`.
- `v3_venue_adapters.py` holds the per-venue spec (custody class, signer
  family, REST/testnet hosts, docs, ladder ceiling) and the signers:
  **binance-style** (binance, aster, mexc), **okx**, **bybit** — each
  signature path is unit-tested network-free. Every other venue declares
  `pending:<exact missing piece>` rather than pretending.
- `SignedRestClient` in DRY_RUN materializes the full signed request
  (query, headers, body) and intent-logs it — that is what the parity
  harness compares against fills. Real send requires the triple gate from
  §2; the engine-facing surface remains the P4a `ExecutionAdapter` seam
  (LiveAdapter), so no engine change is needed per venue.
- Desk-facing data (`v3_desk scan/show/history/basis`) needs no keys at
  all and now covers the Wave-0 additions natively.

## §7 Verify-at-key-time checklist (per venue, before Step 4)

1. Fee schedule re-verified from the venue's official page (enter into
   `v3_connectors.FEES` with a source comment; `fee_unknown` otherwise).
2. Funding cadence from 30+ live observations vs the cadence hint in §1.
3. Signed-request shape: one real read-only call (balance or positions)
   compared against the venue's canonical-string doc — this retires every
   "canonical-string verify" pending marker.
4. Symbol/precision: min notional + qty step per market into the P4b
   live-exchangeInfo cache (replaces the static VENUE_RULES subset).
5. Rate limits per IP **and** per key; window poll is 20 s.
6. For DEX venues: linked-signer/agent/subkey permission surface
   re-audited (withdraw? transfer? settings?).
7. CEX only: owner-side withdrawal probe on a separate subaccount.

## §8 Operational note — sandbox rollback #5 (2026-09-22)

Before this build, the sandbox had rolled back untracked files again: both
`v3.db` and `paper_v3.db` were 0-byte shells. Recovery from the git-tracked
dumps (`v3_state_20260921T0807.sql.gz`, `paper_v3_state_20260921T0807.sql.gz`)
restored authoritative state: pos #5 open (INJ bingx→okx), 22 fills, 2753
tripwires, kv 93, integrity ok. **Lost:** the 1.58M-row `funding_obs`
history layer (the Sep-14 mirror did not survive this time) — desk
`history`/Max-F context rebuilds via `funding_history_scan_v2.py`
(open item, network-heavy). Backup at every window end remains the rule.
