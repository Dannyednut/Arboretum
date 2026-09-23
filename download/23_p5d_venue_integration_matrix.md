# Part 23 — P5d Venue Integration Matrix

**Scope:** research every venue worth trading funding-carry on, so the desk's venue set
expands deliberately: which ones integrate as plain APIs, which ones need agent/wallet
credentials, what each costs in custody risk and effort, and in what order to onboard.
**Mode:** DRY_RUN throughout — nothing here requires keys; execution waves wait on the
user (P4b deferred, keys land in their environment).

**Date:** 2026-09-22 · **Phase:** P5d (per Part 22 §track map) · **Status:** research
delivered; data-layer waves are immediately actionable, execution waves gated.

---

## §0 TL;DR

1. **The "some APIs, some agent/wallet" split is real but softer than it sounds.** Of 13
   new venues surveyed, 10 perp-DEXes expose *wallet-derived API keys* (agent wallets,
   subkeys, linked signers) — integration effort converges to CEX-class once a scoped key
   is issued once via UI. Only the Solana/Cosmos natives (Drift, dYdX chain, Nado without
   a linked signer) require true keypair custody patterns.
2. **Every DEX surveyed offers a scoped-down credential** (no withdrawals): Hyperliquid
   API wallets, Paradex subkeys, Lighter maker-only keys (+10-year read-only tokens),
   Nado linked signers, Aster API keys. Custody risk is manageable if — and only if — we
   never hold main-wallet private keys in `.env`.
3. **Fee landscape is the real filter for a carry desk.** MEXC futures officially opened
   to API trading (Mar 31 2026) at ~1 bps maker / 5 bps taker — the cheapest verified
   schedule we've seen and a strong pairing leg for retail-heavy venues. Backpack
   (9.5/8.5 bps in our verified table) stays data-only: round-trip fees eat the carry.
4. **Funding cadence is shifting industry-wide toward hourly** (Lighter, Extended, dYdX,
   HL, Coinbase Intl, Kraken-EEA, Deribit-history; BloFin made settlement frequency
   dynamic since Dec 2025). Hourly venues compound carry faster *and* decay faster — our
   half-life/exit math (planned extension) matters more per venue added.
5. **Aster API change is P4b-relevant (verified on official repo):** V1 API-key issuance
   ended 2026-03-25; new integrations use V3; since 2026-09-01 authenticated endpoints
   require the main wallet to have completed a deposit (agent-wallet endpoints and public
   data unaffected). The user-side P4b Aster key must be a **V3** key.
6. **Recommended order:** data-layer first for ~8 key-free candidates (widens desk scan
   immediately, zero custody), then execution Wave 1 = Lighter + Paradex + Nado-exec +
   Aster-V3, Wave 2 = MEXC + BitMEX + KuCoin/Gate, Wave 3 = the rest (watch list with
   explicit blockers). Each wave enters through the existing ladder: data → DRY_RUN →
   testnet → $50/leg → $1k.

---

## §1 Why now

The VOOI scan (Part 21) showed the benchmark Arbitrage Desk runs 9+ venues. We run 11
registered (9 with native live funding, 2 data-only via Sharpe) — competitive on data
breadth, but execution breadth is zero until the user lands keys, and our candidate
universe is limited to venues whose *dislocations we can already see*. Venue expansion is
the highest-leverage remaining P5 item (Part 22 track: P5d), and it has a hard sequencing
property the user flagged: some venues take API keys, some need agent/wallet credentials —
those two families have different custody models, different ladder steps, and different
effort curves, so the research has to classify before the desk hard-codes any venue sets.

Secondary evidence that more venues = more carry (Q2 2026 BitMEX derivatives report):
Hyperliquid paid ~+7.17% more annualized funding than Binance on BTC and ~+5.31% on ETH,
one-sided 95%/89% of the time. Structural per-venue dislocation persists even on majors;
retail-heavy venues (BingX, Toobit, WEEX-class) dislocate far harder. A 2026 academic
two-tier funding study (MDPI) reaches the same conclusion across 26 exchanges.

## §2 Integration taxonomy (answers "APIs vs agent/wallet")

| Class | Credential model | Custody exposure | Venues |
|---|---|---|---|
| **A — CEX API keys** | HMAC/ED25519 keys, permissions toggled at creation | Keys cannot withdraw if configured trade-only; IP allowlist | MEXC, BitMEX, KuCoin, Gate, HTX, Kraken, Deribit, BloFin, WhiteBIT, Toobit, WEEX, Coinbase Intl, Backpack |
| **B — DEX delegated keys** | Key derived/issued once from wallet signature, then API-shaped signing | Scoped: no withdrawals by design (verify per venue) | Hyperliquid (API wallet), Paradex (subkey→JWT), Lighter (API key idx), Aster (API key), Orderly (Orderly Key), ApeX Omni (API key), edgeX (L2 key), Extended (L2 key, verify), Nado (linked signer) |
| **C — Raw wallet signing** | Per-order signing with the actual wallet key | Full withdrawal power — needs burner-wallet policy | Drift (Solana keypair), dYdX v4 (deterministic chain key from EVM wallet), Nado *without* linked signer |
| **D — Aggregators** | Venue-native under the hood; aggregator wallet/session | Delegated to aggregator contract | VOOI (Part 21: free second data source now, optional unified exec at P4c) |

The punchline: **Class B dominates the DEX world.** The industry converged on
"sign once with the wallet → get a trade-only key." So the user's instinct is right at
the category level (DEX ≠ CEX), but at the implementation level 10 of 13 new venues are
still API-shaped — the difference is *how the key is born* and *what it can't do*, not
how requests are signed day-to-day.

## §3 Current coverage (verified from `v3_connectors.py`)

- **Native live funding (9):** binance, aster, okx, bybit, bitget, hl, dydx, bingx,
  backpack.
- **Data-only via Sharpe (2):** nado, orderly (native probes = open item Part 12 §6).
- **Verified fee table (taker/maker bps):** aster 4.0/0.0 · hl 4.5/1.5 · binance 5.0/2.0
  · okx 5.0/2.0 · dydx 5.0/2.0 · bybit 5.5/2.0 · bitget 6.0/2.0 · bingx 5.0/2.0 ·
  nado 3.5/1.0 · orderly 3.0/0.0 · backpack 9.5/8.5.
- **Execution:** none live (DRY_RUN stance). P4a adapter seam (protocol + paper +
  parity harness) is the single integration point every new venue plugs into — the
  research below assumes that architecture, not per-venue forks.
- Funding interval per venue is *inferred from history* (`_iv_from_hist`, median of
  observed gaps) — new venues inherit this, so cadence claims in the matrix are
  cross-checked automatically on ingestion.

## §4 Master matrix (23 venues)

Legend: **Class** §2 taxonomy · **Cad** funding settlement cadence (✓ = verified this
research, ~ = infer/verify at ingest) · **Data** what we have today · **Key-free data**
public endpoints usable now · **Testnet** · **Fees t/m** bps (✓ verified source / TBD =
fee_unknown gate at integration) · **Wave** §8.

| Venue | Class | Auth / custody | Cad | Data now | Key-free data | Testnet | Fees t/m | Wave |
|---|---|---|---|---|---|---|---|---|
| Binance | A | HMAC key, trade-only + IP allowlist | 8h~ | native | ✓ | — | 5.0/2.0 ✓ | live |
| OKX | A | HMAC key, trade-only | 8h~ | native | ✓ | demo | 5.0/2.0 ✓ | live |
| Bybit | A | HMAC key, trade-only | 8h~ | native | ✓ | testnet | 5.5/2.0 ✓ | live |
| Bitget | A | HMAC key, trade-only | 8h~ | native | ✓ | — | 6.0/2.0 ✓ | live |
| BingX | A | HMAC key, trade-only | 8h~ | native | ✓ | — | 5.0/2.0 ✓ | live |
| Hyperliquid | B | API wallet (agent, no-withdrawal), EVM | 1h ✓ | native | ✓ | testnet | 4.5/1.5 ✓ | live |
| dYdX v4 | C | deterministic chain key from EVM wallet sig | 1h ✓ | native | ✓ | testnet | 5.0/2.0 ✓ | live |
| Aster | B | API key via wallet connect — **V3 only now** | ~ | native | ✓ | **testnet ✓** | 4.0/0.0 ✓ | live-data; exec W1 |
| Backpack | A | ED25519 API key | ~ | native | ✓ | — | 9.5/8.5 ✓ | data-only (fee wall) |
| Nado | B | linked signer (delegated addr); "no API keys" | 8h~ | Sharpe | ✓ | — | 3.5/1.0 ✓ | exec W1 |
| Orderly | B | Orderly Key (ed25519) | 8h~ | Sharpe | ✓ | testnet | 3.0/0.0 ✓ | exec W2 |
| **Lighter** | B | API key idx 0–254; **maker-only keys**; secure-withdrawal-only constraint | **1h ✓** | — | ✓ (public funding endpoints) | — | TBD | **W1** |
| **Paradex** | B | **subkey → JWT** (subkey: no withdraw/transfer) | cont. accrual, 8h pay ✓ | — | ✓ | **testnet ✓** | TBD | **W1** |
| edgeX | B | L2 (StarkEx) key; **API gated behind 1,000 EDGE stake** | 8h~ | — | ✓ | — | TBD | W3 |
| Extended | B | L2 key (StarkEx family; verify issuance flow) | **1h ✓** (8h realization) | — | ✓ | — | TBD | W2 |
| ApeX Omni | B | system-issued API key + secret | 8h~ | — | ✓ | — | TBD | W3 |
| Drift | C | Solana keypair (burner policy) | 1h~ | — | ✓ | devnet | TBD | W3 |
| VOOI | D | wallet session; venue-native under the hood | per-venue | — | ✓ (docs/fps) | — | venue-native | data now; exec P4c option |
| **MEXC** | A | HMAC key (futures API official since 2026-03-31; KYC) | 8h~ | — | ✓ | — | **1.0/5.0 ✓** | **W2** |
| BitMEX | A | permanent API key | 8h~ (some 1h/4h) | — | ✓ | **testnet ✓** | TBD | **W2** |
| KuCoin | A | HMAC key | 8h~ | — | ✓ | — | TBD | W2 |
| Gate | A | HMAC key (v4) | 8h ✓ | — | ✓ | — | TBD | W2 |
| HTX | A | HMAC key | 8h~ | — | ✓ | — | TBD | W3 |
| Kraken | A | HMAC key | **8h US / 1h EEA ✓** | — | ✓ | — | TBD | W3 |
| Deribit | A | HMAC key | hourly hist ✓; per-instr settle | — | ✓ | testnet | TBD | W3 |
| BloFin | A | HMAC key | **dynamic since 2025-12 ✓** | — | ✓ | — | TBD | W3 |
| WhiteBIT | A | HMAC key (API exposes next settlement ts) | 8h~ | — | ✓ | — | TBD | W3 |
| Toobit | A | HMAC key | ~ | — | ✓ | — | TBD | watch |
| WEEX | A | HMAC key | ~ | — | ✓ | — | ~6/2 (3rd-party) | watch |
| Coinbase Intl | A | HMAC key; eligibility varies by jurisdiction | **1h ✓** | — | ✓ | — | TBD | W3 |

Wave logic and per-venue detail follow.

## §5 Deep notes — what integration actually looks like

### §5.1 DEX venues (Class B/C)

**Lighter (zk-rollup perp DEX) — Wave 1.**
API keys are per-account indexes (0–254; 0–3 reserved for their front-ends), each with
its own public/private key and nonce; requests are signed off-chain (Go/Python SDKs).
Custody is unusually well-specified: API keys *can* process withdrawals, but only
"secure withdrawals" (funds can only ever return to the L1 address that created the
account); fast withdrawals/transfers to other addresses require the L1 wallet key, which
we never hold. Read-only auth tokens (up to 10-year expiry) give the desk an
auth-gated-data path with zero trading power — a clean fit for `show`/`history` depth.
**Maker-only API keys** (premium accounts, up to 251 keys) restrict the key to
post-only/ALO orders, modifies and cancels on the 0 ms speed-bump path — i.e. a key that
*cannot* take liquidity, which is exactly our carry entry profile and doubles as a
self-imposed rule check. Funding pays at each hour mark with the premium component
spread over 8h (CEX-aligned). Fees: TBD (verify schedule at integration; expected
low-single-digit bps). Effort M: SDK-based signing, no gas, testnet unclear.

**Paradex (Starknet app-chain) — Wave 1.**
Two credential types: JWTs (permissions-carrying, per-request) and private keys used to
mint JWTs and sign orders. The main private key can withdraw — never store it. The
**subkey** is the designed alternative: a scoped private key with no withdrawal, no
transfer, no sensitive account settings. Python (`paradex-py`) and JS SDKs; JWT flow is
documented step-by-step; testnet exists (funded via Starknet Sepolia). Funding is the
interesting part: **multi-venue impact-based continuous funding** — accrues
continuously, pays periodically (~8h default), designed to track perp-vs-index across
venues. Our APR math handles continuous accrual fine, but payment timing and
`min_f1h/24h` flip stats must be computed from realized-payment history, not the
instantaneous rate — worth a dedicated parser test. Fees TBD. Effort M.

**Nado (Ink-chain perp/spot) — Wave 1 (exec), already in fee table.**
"No API keys: your wallet's private key IS your authentication" — EIP-712 typed-data
signing per execute. But the **linked signer** mechanism designates a separate address
allowed to sign executes on behalf of a subaccount — that converts Nado to Class B:
fund the main account from the main wallet, link a burner signer, hold only the signer
key in `.env`. Withdrawals are gateway executes — a linked signer's withdrawal power
must be verified before Step 3 (their `withdraw-collateral` doc + signer scoping).
Sharpe already covers data; native funding/book probes are the open item. Fees
3.5/1.0 in our table. Effort M.

**Aster — Wave 1 exec (data already native).** ⚠️ **Verified on official repo:**
V1 API-key creation ended 2026-03-25 (existing keys keep working); new integrations use
**V3** (futures + spot + **testnet** docs published); from 2026-09-01 all authenticated
V3 endpoints require that the main wallet linked to the account has completed a deposit
(`-5050` otherwise) — agent-wallet and public endpoints unaffected. Direct P4b
consequence: the user's Aster key must be created via the V3 flow, and their wallet
needs deposit history (they have it if they've ever funded Aster). Fees 4.0/0.0
verified. Effort S for exec (connector data layer already done; V3 shape ≈ Binance-style
REST).

**Hyperliquid — live; two extensions.** (1) Exec path via **API wallets** ("agent
wallets"): approved to act for an account, explicitly **without withdrawal permission**;
the account's real address stays the public identity. This is the reference
implementation of Class B and the pattern we copy in the custody model (§6).
(2) **HIP-3 builder-deployed markets** ride the same API — new perp markets (including
non-standard collateral/market rules) appear in our existing HL connector's listings;
impact is *data hygiene* (flag builder markets, per-builder fee schedules →
`fee_unknown` until verified, separate min-OI gates) rather than new code. Effort S each.

**edgeX (StarkEx) — Wave 3.** L2 ECDSA (Stark curve) signing; Go signature lib exists.
**Blocker: API access requires staking 1,000 EDGE** for the API whitelist — a capital
lockup + token-exposure cost to price in before W2. Funding cadence verify.

**Extended (StarkEx family) — Wave 2.** Funding charged **every hour**, rate realized
over an 8h window, zero interest-rate component — clean hourly-carry venue with
CEX-alignment. API-key issuance flow from wallet not fully verified this pass
(docs section not indexed in llms.txt) → verify-at-integration item, plus jurisdiction
eligibility language on their pages. Effort M.

**dYdX v4 — live data; exec Wave 2/3.** Chain key is **deterministically derived** from
an EVM wallet signature (v4-client-js) — one signing ceremony yields a Cosmos-chain key;
after that it's gRPC/Indexer like any API. Custody: treat the derived key as a Class C
keypair (it *can* withdraw) → isolated mnemonic, working-capital-only. Testnet exists.
Effort M (cosmos tooling is the cost, not auth).

**Drift (Solana) — Wave 3.** TS SDK + Solana keypair; devnet supported; "Swift" order
path for faster execution. True Class C: the keypair is the wallet. Burner-keypair
policy required; RPC reliability is an ops dependency. Cadence ~1h (verify). Effort L.

**ApeX Omni — Wave 3.** System-issued API key/secret pair for all trading endpoints
(separate RWA-market key noted in docs); official Python SDK (`apexpro-openapi`).
Standard Class B; priority low simply because other venues price better.

**Backpack — stays data-only.** Fees 9.5/8.5 bps verified: a 18 bps round trip before
slippage is fatal at our typical F-spreads. Revisit only if fee tiers change.

**VOOI — Class D.** No keys of its own (wallet sessions; venue-native execution under
the hood, no fee markup per Part 21). Role unchanged: **free second data source** for
desk cross-checks now; optional unified DEX-leg execution path revisited at P4c.

### §5.2 CEX venues (Class A)

All Class A venues share one template: HMAC key (+secret), enable *futures trading
permission only*, never enable withdrawal permission, bind IP allowlist. Ladder: data
(now, key-free) → DRY_RUN → testnet where available → $50/leg. Differences worth noting:

**MEXC — Wave 2, headline venue.** Futures API officially opened 2026-03-31: any KYC'd
user can apply for futures-permission API keys; announced schedule **maker 0.01% /
taker 0.05% (1/5 bps)** — cheapest verified fee schedule in the matrix, and maker-heavy
fits our entry style. Before this change, futures API access was unofficial/community
(older SDKs warn about it) — the official opening also removes the ToS ambiguity.
Caveat: verify fees at key creation (promo schedules drift), confirm per-symbol funding
interval at ingest (varies).

**BitMEX — Wave 2.** Permanent API keys, mature REST/WS, **full testnet** — the cheapest
full-fidelity rehearsal environment among CEXs. Funding 8h on majors; several alt markets
run 1h/4h (our interval inference handles). Fee TBD (historically maker-rebate
structure — worth verifying, a maker rebate would be a carry edge).

**KuCoin / Gate — Wave 2.** Both vanilla Class A with full public funding endpoints;
Gate v4 public rate limits generous (300 r/s public); KuCoin exposes an actual-fee
endpoint (pull per-account fees at DRY_RUN time rather than assuming the table).

**HTX — Wave 3.** USDT-M swap API mature (official API refs), funding-methodology
adjustments documented; nothing unique — scheduled by effort budget, not blocker.

**Kraken — Wave 3, one flag.** Funding interval is **regional**: 8h for US clients, 1h
for EEA and other regions. Our interval inference will just report what it sees, but
APR math and half-life planning must not assume a fixed cadence for this venue; also
confirm derivatives availability for the operating jurisdiction at key time.

**Deribit — Wave 3.** Hourly funding-rate history endpoints; premium-based mechanics
with ±0.5% cap; majors-focused (BTC/ETH + USDC alts). Dislocations are rarer than on
retail-heavy venues, but deep majors liquidity makes it a good *anchor leg* candidate
when a retail venue spikes. Testnet available.

**BloFin — Wave 3, one flag.** Since 2025-12-09 funding settlement frequency is
**dynamically adjusted** per contract — do not hardcode cadence; rely on observed
history only (our inference rule). API rate limits modest (trading APIs ~30 req/10s).

**WhiteBIT — Wave 3.** Futures API publishes *next settlement timestamps* per market —
friendlier than average for our scheduler (no inference needed for the payment clock).

**Toobit / WEEX — watch list.** Retail-heavy venues = historically rich dislocations
(the BingX effect), both have public funding APIs and community Python SDKs, but they
are smaller names: withdrawal-counterparty risk and fee schedules need the full
verify-at-integration checklist before any live step. Keep at data/DRY_RUN only.

**Coinbase Intl — Wave 3.** Hourly funding ✓, institutional-grade API, but
eligibility varies by jurisdiction (verify for our operating region) and fee schedule
TBD. Include in desk data early anyway — its hourly majors funding is a good
cross-check source.

## §6 Custody & security model (per class)

**Class A (CEX keys).** Create the key with *futures-trade permission only*; withdrawal
permission stays off; bind the server IP in an allowlist; prefer a dedicated sub-account
per venue so blast radius is one leg. Keys live in `.env` behind `v3_settings.py`
(SecretStr, loud-partial validation — already scaffolded in P4b, commit `ab33481`).
Venue onboarding into the trust ladder includes one small withdrawal probe *by the
account owner* (the BingX-$100 pattern) — this tests the fiat ramp out, not the API,
and is a user-side step.

**Class B (DEX delegated keys).** The rule that matters: **the key in `.env` must be
the scoped credential, never the main wallet key.** Concretely — HL API wallet (not the
main EVM key), Paradex subkey (not the account private key), Lighter API key at an index
we control (optionally maker-only), Nado linked signer (not the main wallet), Aster V3
API key, Orderly Key, ApeX API key. Residual exposure to verify per venue before Step 3:
what *can* the scoped key do besides trade (Lighter: secure-withdraw-only-to-same-L1;
Paradex subkey: no withdraw/transfer; HL agent: none; Nado linked signer: verify;
others: verify at integration). DRY_RUN uses read-only tokens wherever offered
(Lighter 10-year read-only tokens are ideal for the desk data layer).

**Class C (raw wallet signing).** Policy, not code: a **dedicated burner wallet**
per venue (Drift keypair, dYdX derived chain key), funded with working capital only,
never reused across venues, never the main wallet. A drained burner costs at most its
float; a drained main wallet is a catastrophe. This is the same trade-only principle as
Class A, implemented by funding isolation instead of permission scoping.

**Cross-cutting.** No key material in code, logs, or artifacts; `.env` stays
git-ignored (the Task-38 lesson); every key-issuance event gets an audit note in the
worklog with date + scope; key rotation is a manual runbook step per venue at ladder
promotion.

## §7 Trust-ladder mapping (unchanged gates, new venue classes)

| Step | What ships | Class A | Class B | Class C |
|---|---|---|---|---|
| 0 — data (now) | public funding + books → desk scan/show/history | ✓ | ✓ | ✓ |
| 1 — DRY_RUN | adapter-seam paper fills on real books, parity drill | ✓ | ✓ | ✓ |
| 2 — testnet | API-shape + order-lifecycle rehearsal | where offered (BitMEX, Deribit, Bybit, OKX) | where offered (Aster V3, Paradex, HL, Orderly, dYdX) | devnet (Drift) |
| 3 — $50/leg | live pilot, 3-window parity tally, per-venue BingX-style ramp probe | user keys | scoped key | burner funded |
| 4 — $1k | full allocation, TW1–TW6 + merit gates + kill switch apply unchanged | ✓ | ✓ | ✓ |

The ladder is per-*venue*, not per-system: MEXC can be at Step 3 while Drift sits at
Step 0. The engine's kill switch and freeze list already key on venue strings, so a
per-venue ladder needs no engine changes — only venue-registry entries with a
`ladder_step` field.

## §8 Wave plan & desk impact

**Wave 0 — data layer (actionable now, key-free, no custody):** add public funding +
book connectors for Lighter, Paradex, Extended, MEXC, BitMEX, KuCoin, Gate, Toobit/WEEX
(any order of cheap wins) following the existing `v3_connectors.py` pattern
(`_f_<venue>` + `_iv_from_hist` interval inference + cross_check vs Sharpe where
overlapping). Impact: desk `scan`/`show`/`history` immediately widen; new fee rows
enter `FEES` only with a verified source (fee table stays a verified table); candidates
gate on min-OI and depth as always. This is the only wave consistent with today's
DRY_RUN stance and needs no user action.

**Wave 1 — DEX exec (when the user opts in):** Lighter (maker-only key if premium;
else normal key), Paradex (subkey), Nado (linked signer), Aster V3 (testnet first —
the only DEX in the set with a published V3 testnet). All four are Class B with scoped
credentials; all four plug into the P4a adapter seam with one `ExecutionAdapter`
implementation each.

**Wave 2 — CEX exec, cheap fees first:** MEXC (1/5 bps verified), BitMEX (testnet
rehearsal), KuCoin, Gate, Extended (after issuance-flow verify). Class A template from
§6; each is a standard HMAC adapter.

**Wave 3 — watch list with explicit blockers:** edgeX (1,000 EDGE stake), Kraken
(regional cadence + jurisdiction), Deribit, HTX, BloFin (dynamic cadence), WhiteBIT,
ApeX, Drift (Class C effort), Coinbase Intl (eligibility), dYdX exec (cosmos tooling),
Toobit/WEEX (counterparty risk). Each blocker is priced, not forgotten.

**Backpack:** data-only permanently unless fees drop (9.5/8.5 bps is a round-trip wall).

**Desk implementation notes:** (1) venue registry gains `ladder_step` + `custody_class`
fields so `scan` can filter by what's actually tradable at the current stance;
(2) `show` should render scoped-credential venues identically to CEX venues — the
adapter seam makes that true at the code level; (3) HIP-3 builder markets need a
`hip3`/builder flag with separate fee verification, else they pollute HL candidate
rows with unknown-fee economics; (4) Paradex/BloFin/Kraken-style non-fixed cadences
reinforce the existing rule: never hardcode intervals, always infer from history.

## §9 Verify-at-integration checklist (per venue, before any Step ≥ 2)

1. Fee schedule from an official page (enter into `FEES` with source; else
   `fee_unknown` blocks the candidate).
2. Funding settlement cadence: 30+ observations, `_iv_from_hist` median, cross-checked
   vs any published claim (BloFin/Paradex/Kraken lessons).
3. Scoped-credential audit: exact permission surface of the key we'd hold (withdraw?
   transfer? settings?) — documented in worklog before Step 3.
4. Rate limits per IP **and** per key (HL publishes both; others vary) — engine windows
   poll every 20s, so per-key limits matter more than per-IP.
5. Jurisdiction/eligibility for the operating region (Kraken, Coinbase Intl, Extended).
6. Testnet fidelity check where offered (does testnet funding/books match prod shape?).
7. Min notional, precision, and post-only support (our maker entries depend on it;
   Lighter ALO and CEX post-only flags cover this).
8. For DEX venues: gas model (none for zk/StarkEx L2s; Solana fees+RPC for Drift),
   and deposit/withdrawal rails actually usable from the user's region.

## §10 Sources (key pages, verified this session)

- Lighter: `apidocs.lighter.xyz/docs/api-keys` (indexes, withdrawal constraint,
  read-only tokens, maker-only keys); `docs.lighter.xyz` funding (hourly payments).
- Paradex: `docs.paradex.trade/api/general-information/api-authentication` (JWT vs
  private keys, subkeys); funding-mechanism page (continuous, multi-venue impact).
- Nado: `docs.nado.xyz` core concepts (wallet-as-auth), linked-signers + signing pages.
- Aster: `github.com/asterdex/api-docs` README (V1 key sunset 2026-03-25; V3
  recommended; 2026-09-01 deposit requirement; testnet docs).
- Hyperliquid: gitbook HIP-3 page (builder-deployed perps, 500k HYPE deploy stake);
  `app.hyperliquid.xyz` API-wallets page (agent = no withdrawal); rate-limits page.
- edgeX: `pro.edgex.exchange` API page (1,000 EDGE stake whitelist); GitBook API V2;
  Stark-signature reference implementation (community).
- Extended: `docs.extended.exchange` funding-payments (hourly charge, 8h realization).
- MEXC: official announcement "Introducing API Futures Trading" (2026-03-31; KYC;
  maker 0.01% / taker 0.05%).
- BitMEX: REST API docs (permanent keys) + testnet explorer.
- Kraken: kraken.com perps guide (8h US / 1h EEA funding intervals); developers docs.
- Deribit: `docs.deribit.com` funding-rate-history (hourly) + funding specifications
  (premium − 0.025%, ±0.5% cap).
- BloFin: official notice (2025-12-09 dynamic funding settlement frequency); API docs.
- WhiteBIT: futures markets API (next funding settlement timestamps).
- Coinbase Intl: help center (funding interval 1h) + developer docs (PERP instruments).
- Strategy context: BitMEX Q2-2026 derivatives report (HL vs Binance funding
  differential); MDPI 2026 two-tier funding-structure study.

