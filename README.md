# Arboretum

*a garden of ARBs — every venue a tree, onboarded up the trust ladder.*

Arboretum is a **multi-venue funding-carry arbitrage system** for crypto perpetuals:
it finds, prices, paper-trades and (behind hard gates) executes delta-neutral
long/short positions that harvest funding-rate differentials between exchanges.
The product surface is a **headless arbitrage desk** — the VOOI Arbitrage Desk
concept, self-hosted and extended (Part 21 competitive scan).

Everything currently runs **paper / DRY_RUN** on a $1k virtual book.
Real orders are *impossible by construction* until three independent gates flip
together (see [Safety model](#safety-model)).

---

## Repo layout

| Path | What it is |
|------|------------|
| `scripts/v3_desk.py` | Headless desk surface: `scan` / `show` / `history` / `basis` / `open` |
| `scripts/v3_exec.py` | Paper execution engine (windows, tripwires, kill switch, desk-pin mode) |
| `scripts/v3_connectors.py` | Native funding + book connectors (18 execution-sphere venues) |
| `scripts/v3_venue_adapters.py` | All-venue capability layer: custody specs, signers, signed-request client |
| `scripts/v3_scanner.py` | Cross-venue funding scanner (interval-correct, identity-guarded) |
| `scripts/v3_portfolio.py` / `v3_events.py` / `v3_tripwires.py` | Portfolio, event log, tripwire chain |
| `scripts/v3_settings.py` | Credential/mode config (SecretStr, loud partials, validators) |
| `scripts/test_*.py` | Network-free regression suites (300+ assertions, all green) |
| `.env.example` | Full credential template — every venue, permission contract, warnings |
| `download/` | Build log Parts 01–24 (design docs, research, guides), data artifacts, DB backups |
| `worklog.md` | Lab notebook: every task, decision, bug, and incident on record |

## The desk

```bash
python scripts/v3_desk.py scan --min-apr 0.10     # cross-venue strategy table
python scripts/v3_desk.py show INJ                # per-coin deep dive (funding, books, P-spread@size)
python scripts/v3_desk.py history INJ --days 7    # per-venue funding series + CSV
python scripts/v3_desk.py basis --min-fapr 0.10   # spot-perp basis scanner (P5c)
python scripts/v3_desk.py open --coin INJ         # gated on-demand entry (preflight → pin → engine)
python scripts/v3_exec.py --pin-desk              # engine window honoring a desk pin
```

The desk reads **key-free public APIs**. `open` never bypasses gates — the pin
only adds a candidate; the engine's tripwire chain can still refuse it (and has,
live, by design).

## Coverage

- **Scanner (execution sphere):** binance, aster, okx, bybit, bitget, hyperliquid,
  dydx, bingx, backpack, gate, kucoin, bitmex, deribit, htx, paradex (+ Sharpe
  data-only: nado, orderly, gate_io alias…)
- **Credential surface:** 29 venues specified with env fields, custody class,
  signer family, REST/testnet hosts and trust-ladder ceiling in
  `scripts/v3_venue_adapters.py`
- **Custody taxonomy (Part 23):**
  `[A]` CEX API keys (trade-only) · `[B]` DEX scoped/delegated keys (Hyperliquid
  API wallet, Paradex subkey, Lighter maker-only key, Nado linked signer…) ·
  `[C]` raw-wallet signing (burner policy; dYdX/Drift) · `[D]` aggregators (VOOI)
- **Fee schedule:** only verified-official rows enter the model (e.g. MEXC 1/5 bps,
  Gate 5/2, KuCoin 6/2, BitMEX 7.5/−2.5 maker rebate)

## Trust ladder

Every venue climbs the same ladder — data → `DRY_RUN` → testnet → **$50/leg**
→ **$1k book** — and can be pinned at any rung. A venue is either
`ready`, or it declares `pending: <the exact missing piece>`.
Support is declared, never faked.

## Safety model

1. **Triple gate** — a signed order leaves the machine only when
   `DRY_RUN=false` **and** `YES_REAL=true` **and** the venue's ladder step was
   operator-promoted. Default is off; validators enforce it.
2. **Permission contract** — every key is trade-only: withdrawals disabled, IP
   allowlisted, per-venue sub-accounts where supported.
3. **Engine defenses** — identity guard (marks must agree ≤5%), interval-correct
   funding math, tripwire chain (TW1–TW3), per-venue kill switch and freezes,
   pair cooldowns, filled-size-truthful unwinds.
4. **Durability** — state snapshots backed up as git-tracked SQL dumps
   (`download/db_backup/`); five rollbacks survived to date.

## Docs (Part index, `download/`)

- **01–05** — research: strategy deep dive, all-arb market study, Aster/DEX
  expansion, 90-day persistence distributions, L2 depth gate → paper basket
- **06–11** — floors, multisession stability, trader-identity strategy book,
  Sharpe integration, new-venue gates
- **12–18** — v3 system spec, then build parts: data layer (P1/M1), tripwire
  chain (P2), exec engine (P3), day-7 review, fix queue
- **19–20** — execution adapter scope + P4a adapter seam
- **21–22** — VOOI competitive scan, headless desk scope (P5a read / P5b gated
  open / P5c basis)
- **23–24** — 23-venue integration matrix (custody classes, waves) and the
  **per-venue integration guide** (what key, what permission, what ladder step)

## Quick start

```bash
cp .env.example .env    # fill in ONLY what your ladder step needs; chmod 600
python scripts/v3_desk.py scan                # no keys needed
python scripts/test_venue_specs.py            # sanity: capability layer green
```

No `.env` is required for the read surface — the desk and scanner run fully
key-free. Keys enter only when you choose to climb the ladder.

---

*Status: paper book live (position #5 INJ bingx→okx under monitoring).
Build narrative, incidents and decisions: `worklog.md`.*
