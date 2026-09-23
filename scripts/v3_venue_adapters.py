#!/usr/bin/env python3
"""v3_venue_adapters.py - P5d live-trade capability layer (Part 24).

Per Part 23: every researched venue gets a VENUE_SPEC (custody class, env
fields, signer family, REST/testnet URLs, docs, ladder ceiling).  Signers
implemented for the three families we can verify network-free (binance-style
HMAC, OKX, Bybit); every other venue carries signer="pending:<what>" and an
explicit ladder ceiling - support is declared, never faked.

Real placement is TRIPLE-GATED, mirroring the P4a LiveAdapter stance:
  1. EXEC_MODE=live and DRY_RUN=false
  2. YES_REAL=true (operator flip, after parity tally)
  3. venue credentials complete + venue spec exec_ready
Any gate unmet -> dry-run shadow row in the intent log, nothing sent.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import v3_exec_adapter as exa     # noqa: E402  (IntentLog)

# ------------------------------------------------------------- signatures


def _hmac_hex(secret, msg):
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def _hmac_b64(secret, msg):
    return base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()


class BinanceStyleSigner:
    """HMAC-SHA256 over the urlencoded query, hex digest, appended as
    signature=; api key travels in a per-venue header.  Covers binance,
    aster V3, mexc, toobit-style clones (canonical string per venue must
    be re-verified against live keys before Step 3 - parity drill does
    this automatically)."""

    def __init__(self, key_header="X-MBX-APIKEY"):
        self.key_header = key_header

    def sign(self, method, path, params, body, api_key, secret, ts):
        q = dict(params or {})
        q["timestamp"] = ts
        qs = urlencode(q)
        sig = _hmac_hex(secret, qs)
        return f"{qs}&signature={sig}", {self.key_header: api_key}, ""


class OkxSigner:
    """sign = b64(hmac(secret, ts + METHOD + requestPath + body))."""

    def sign(self, method, path, params, body, api_key, secret, ts,
             passphrase=""):
        qs = urlencode(params or {})
        req = f"{path}?{qs}" if qs else path
        msg = f"{ts}{method.upper()}{req}{body or ''}"
        headers = {"OK-ACCESS-KEY": api_key,
                   "OK-ACCESS-SIGN": _hmac_b64(secret, msg),
                   "OK-ACCESS-TIMESTAMP": ts,
                   "OK-ACCESS-PASSPHRASE": passphrase}
        return qs, headers, body or ""


class BybitSigner:
    """sign = hex(hmac(secret, ts + key + recvWindow + query|jsonbody))."""

    def sign(self, method, path, params, body, api_key, secret, ts,
             recv_window="5000"):
        payload = urlencode(params or {}) if not body else (body or "")
        msg = f"{ts}{api_key}{recv_window}{payload}"
        headers = {"X-BAPI-API-KEY": api_key,
                   "X-BAPI-SIGN": _hmac_hex(secret, msg),
                   "X-BAPI-TIMESTAMP": ts,
                   "X-BAPI-RECV-WINDOW": recv_window}
        return urlencode(params or {}) if not body else body, headers, body or ""


SIGNERS = {"binance": BinanceStyleSigner, "okx": OkxSigner,
           "bybit": BybitSigner}

# ------------------------------------------------------------- venue specs
# ladder ceiling: 0 data-only, 1 DRY_RUN capable, 2 testnet-capable,
# 3 $50/leg-capable, 4 full $1k (Part 23 s.7).  A ceiling is what the CODE
# supports today - promotion still requires the operator ladder.


def _s(name, custody, env, signer, rest, testnet, public, cadence, docs,
       note, ladder_max, exec_ready=False):
    return {"name": name, "custody": custody, "env": env, "signer": signer,
            "rest": rest, "testnet": testnet, "public": public,
            "cadence": cadence, "docs": docs, "note": note,
            "ladder_max": ladder_max, "exec_ready": exec_ready}


ORDER_PATHS = {   # signed order placement endpoint per implemented family
    "binance": ("POST", "/fapi/v1/order"),
    "okx": ("POST", "/api/v5/trade/order"),
    "bybit": ("POST", "/v5/order/create"),
}

VENUE_SPECS = {
    # ---- live-book venues (data native since P1-P4) -----------------------
    "binance": _s("binance", "A", ["BINANCE_API_KEY", "BINANCE_API_SECRET"],
                  "binance", "https://fapi.binance.com",
                  "https://testnet.binancefuture.com",
                  {"funding": True, "book": True}, "8h",
                  "https://binance-docs.github.io/apidocs/futures/en/",
                  "reference signer family; limiter required (429/-1003)",
                  4, True),
    "aster": _s("aster", "B", ["ASTER_API_KEY", "ASTER_API_SECRET"],
                "binance", "https://fapi.asterdex.com",
                "https://testnet.asterdex.com",
                {"funding": True, "book": True}, "1h",
                "https://github.com/asterdex/api-docs",
                "V3 ONLY (V1 key issuance ended 2026-03-25); auth endpoints "
                "need wallet deposit history since 2026-09-01",
                4, True),
    "okx": _s("okx", "A", ["OKX_API_KEY", "OKX_API_SECRET",
                           "OKX_PASSPHRASE"],
              "okx", "https://www.okx.com", None,
              {"funding": True, "book": True}, "8h",
              "https://www.okx.com/docs-v5/en/",
              "3-field credential; demo trading available",
              4, True),
    "bybit": _s("bybit", "A", ["BYBIT_API_KEY", "BYBIT_API_SECRET"],
                "bybit", "https://api.bybit.com",
                "https://api-testnet.bybit.com",
                {"funding": True, "book": True}, "8h",
                "https://bybit-exchange.github.io/docs/v5/intro",
                "testnet available", 4, True),
    "bitget": _s("bitget", "A", ["BITGET_API_KEY", "BITGET_API_SECRET",
                                 "BITGET_PASSPHRASE"],
                 "pending:bitget-hmac(3-field, prehash=ts+method+path+qs)",
                 "https://api.bitget.com", None,
                 {"funding": True, "book": True}, "8h",
                 "https://www.bitget.com/apiDoc/contract/intro",
                 "signer lands with its key cohort", 1, False),
    "bingx": _s("bingx", "A", ["BINGX_API_KEY", "BINGX_API_SECRET"],
                "pending:binance-family-canonical-string-verify",
                "https://open-api.bingx.com", None,
                {"funding": True, "book": True}, "8h",
                "https://bingx-api.github.io/docs/",
                "withdrawal-test subaccount pattern already on record",
                1, False),
    "hl": _s("hl", "B", ["HL_AGENT_ADDRESS", "HL_AGENT_PRIVATE_KEY"],
             "pending:hl-eip712-agent-signing",
             "https://api.hyperliquid.xyz", "https://api.hyperliquid-testnet.xyz",
             {"funding": True, "book": True}, "1h",
             "https://hyperliquid.gitbook.io/hyperliquid-docs/",
             "API wallet (agent) = no-withdrawal; HIP-3 markets need "
             "builder flag + fee verify", 2, False),
    "dydx": _s("dydx", "C", ["DYDX_MNEMONIC"],
               "pending:cosmos-grpc-tx-signing",
               "https://indexer.dydx.trade", "https://indexer.v4testnet.dydx.exchange",
               {"funding": True, "book": True}, "1h",
               "https://docs.dydx.xyz/",
               "isolated mnemonic (burner policy); derived chain key",
               2, False),
    "backpack": _s("backpack", "A", ["BACKPACK_API_KEY",
                                     "BACKPACK_API_SECRET"],
                   "pending:ed25519-header-signing",
                   "https://api.backpack.exchange", None,
                   {"funding": True, "book": True}, "8h",
                   "https://docs.backpack.exchange/",
                   "DATA-ONLY: 9.5/8.5 bps fee wall (Part 23 s.5)", 0, False),
    "nado": _s("nado", "B", ["NADO_API_KEY", "NADO_API_SECRET"],
               "pending:eip712-gateway-execute",
               "https://gateway.imperatorendex.com", None,
               {"funding": False, "book": True}, "8h",
               "https://docs.nado.xyz/developer-resources/",
               "linked signer = scoped address; verify signer withdrawal "
               "scoping before Step 3", 2, False),
    "orderly": _s("orderly", "B", ["ORDERLY_KEY", "ORDERLY_SECRET"],
                  "pending:ed25519-orderly-signature",
                  "https://api-evm.orderly.org", None,
                  {"funding": False, "book": False}, "8h",
                  "https://orderly.network/docs/",
                  "Orderly Key (ed25519); registration broker flow", 1, False),
    # ---- P5d Wave-0 (native data shipped this part) -----------------------
    "gate": _s("gate", "A", ["GATE_API_KEY", "GATE_API_SECRET"],
               "pending:gate-hmac(raw-body-signing)",
               "https://api.gateio.ws", "https://api-testnet.gateio.ws",
               {"funding": True, "book": True}, "8h",
               "https://www.gate.io/docs/developers/apiv4/",
               "native funding+books live (Part 24)", 1, False),
    "kucoin": _s("kucoin", "A", ["KUCOIN_API_KEY", "KUCOIN_API_SECRET",
                                 "KUCOIN_PASSPHRASE"],
                 "pending:kucoin-passphrase-hmac",
                 "https://api-futures.kucoin.com",
                 "https://api-sandbox-futures.kucoin.com",
                 {"funding": True, "book": True}, "8h",
                 "https://www.kucoin.com/docs-new/",
                 "native funding+books live; sandbox available", 1, False),
    "bitmex": _s("bitmex", "A", ["BITMEX_API_KEY", "BITMEX_API_SECRET"],
                 "pending:bitmex-hmac(api-expires-scheme)",
                 "https://www.bitmex.com", "https://testnet.bitmex.com",
                 {"funding": False, "book": False}, "8h",
                 "https://www.bitmex.com/api/explorer/",
                 "SYMBOL MIGRATION: XBTUSDT settled 2026-09-16, underscore "
                 "naming rolling out; Sharpe-fed until listings stabilize",
                 1, False),
    "deribit": _s("deribit", "A", ["DERIBIT_CLIENT_ID",
                                   "DERIBIT_CLIENT_SECRET"],
                  "pending:deribit-hmac-bearer",
                  "https://www.deribit.com", "https://test.deribit.com",
                  {"funding": True, "book": True}, "1h",
                  "https://docs.deribit.com/",
                  "majors only (BTC/ETH); ticker funding_1h; cross-check "
                  "quarantines expected vs Sharpe 8h snapshots", 1, False),
    "htx": _s("htx", "A", ["HTX_ACCESS_KEY", "HTX_SECRET_KEY"],
              "pending:htx-hmac(path-joined-scheme)",
              "https://api.hbdm.com", None,
              {"funding": True, "book": False}, "8h",
              "https://huobiapi.github.io/docs/usdt_swap/v1/en/",
              "funding-only (book endpoint unverified from sandbox)", 1, False),
    "paradex": _s("paradex", "B", ["PARADEX_PRIVATE_KEY"],
                  "pending:starknet-subkey-jwt",
                  "https://api.prod.paradex.trade",
                  "https://api.testnet.paradex.trade",
                  {"funding": False, "book": True}, "8h",
                  "https://docs.paradex.trade/",
                  "subkey = no withdraw/transfer; funding via Sharpe until "
                  "JWT path ships; continuous multi-venue funding", 2, False),
    # ---- researched, data via Sharpe / probe-pending (Part 23) ------------
    "mexc": _s("mexc", "A", ["MEXC_API_KEY", "MEXC_API_SECRET"],
               "binance", "https://contract.mexc.com", None,
               {"funding": False, "book": False}, "8h",
               "https://mexcdevelop.github.io/apidocs/contract_v1_en/",
               "WAF-blocked from sandbox (403) - Sharpe-fed; fees 1/5bps "
               "official 2026-03-31; KYC required", 1, True),
    "lighter": _s("lighter", "B", ["LIGHTER_API_PRIVATE_KEY",
                                   "LIGHTER_ACCOUNT_INDEX",
                                   "LIGHTER_API_KEY_INDEX"],
                  "pending:lighter-l2-sdk-signing",
                  "https://mainnet.zklighter.elliottech.org", None,
                  {"funding": False, "book": False}, "1h",
                  "https://apidocs.lighter.xyz/",
                  "host unreachable from sandbox (connection reset); "
                  "maker-only keys + 10y read-only tokens when live", 1, False),
    "extended": _s("extended", "B", ["EXTENDED_API_KEY",
                                     "EXTENDED_API_SECRET"],
                   "pending:extended-l2-key-verify-issuance",
                   "https://api.extended.exchange", None,
                   {"funding": False, "book": False}, "1h",
                   "https://docs.extended.exchange/",
                   "hourly funding, 8h realization; issuance flow verify",
                   1, False),
    "edgex": _s("edgex", "B", ["EDGEX_L2_PRIVATE_KEY"],
                "pending:starkex-l2-ecdsa",
                "https://pro.edgex.exchange", None,
                {"funding": False, "book": False}, "8h",
                "https://edgex-1.gitbook.io/",
                "BLOCKER: 1,000 EDGE stake for API whitelist", 0, False),
    "apex": _s("apex", "B", ["APEX_API_KEY", "APEX_API_SECRET"],
               "pending:apex-hmac-with-account-index",
               "https://api.pro.apex.exchange", None,
               {"funding": False, "book": False}, "8h",
               "https://api-docs.pro.apex.exchange",
               "needs account/index fields alongside key pair", 1, False),
    "drift": _s("drift", "C", ["DRIFT_KEYPAIR_B58"],
                "pending:solana-keypair-tx",
                "https://rpc.drift.trade", None,
                {"funding": False, "book": False}, "1h",
                "https://docs.drift.trade/",
                "burner keypair mandatory; RPC dependency", 1, False),
    "kraken": _s("kraken", "A", ["KRAKEN_API_KEY", "KRAKEN_API_SECRET"],
                 "pending:kraken-futures-api-scheme",
                 "https://futures.kraken.com", None,
                 {"funding": False, "book": False}, "8h/1h",
                 "https://docs.kraken.com/api/docs/futures-api/",
                 "regional cadence split (8h US / 1h EEA)", 1, False),
    "blofin": _s("blofin", "A", ["BLOFIN_API_KEY", "BLOFIN_API_SECRET",
                                 "BLOFIN_PASSPHRASE"],
                 "pending:blofin-hmac",
                 "https://api.blofin.com", None,
                 {"funding": False, "book": False}, "dynamic",
                 "https://docs.blofin.com/",
                 "WAF-blocked from sandbox; dynamic funding cadence since "
                 "2025-12 - never hardcode interval", 1, False),
    "whitebit": _s("whitebit", "A", ["WHITEBIT_API_KEY",
                                     "WHITEBIT_API_SECRET"],
                   "pending:whitebit-hmac",
                   "https://api.whitebit.com", None,
                   {"funding": False, "book": False}, "8h",
                   "https://docs.whitebit.com/",
                   "API exposes next settlement timestamps", 1, False),
    "toobit": _s("toobit", "A", ["TOOBIT_API_KEY", "TOOBIT_API_SECRET"],
                 "pending:toobit-binance-family-verify",
                 "https://api.toobit.com", None,
                 {"funding": False, "book": False}, "8h",
                 "https://api-docs.toobit.com/",
                 "watch list: counterparty risk checklist first", 0, False),
    "weex": _s("weex", "A", ["WEEX_API_KEY", "WEEX_API_SECRET"],
               "pending:weex-scheme",
               "https://api-contract.weex.com", None,
               {"funding": False, "book": False}, "8h",
               "https://www.weex.com/api-doc/",
               "watch list: counterparty risk checklist first", 0, False),
    "cbintl": _s("cbintl", "A", ["CBINTL_API_KEY", "CBINTL_API_SECRET"],
                 "pending:coinbase-intl-hmac",
                 "https://api.international.coinbase.com", None,
                 {"funding": False, "book": False}, "1h",
                 "https://docs.cdp.coinbase.com/exchange/docs",
                 "jurisdiction eligibility verify", 1, False),
    "vooi": _s("vooi", "D", [],
               "pending:aggregator-venue-native",
               "https://api.vooi.io", None,
               {"funding": False, "book": False}, "per-venue",
               "https://docs.vooi.io/",
               "aggregator: free second data source now; exec path = "
               "P4c option (Part 21)", 0, False),
}

LADDER_NAMES = {0: "data-only", 1: "DRY_RUN-capable", 2: "testnet-capable",
                3: "$50/leg-capable", 4: "$1k-capable"}


def spec_for(venue):
    return VENUE_SPECS.get(venue)


def adapter_status(settings=None):
    """Per-venue capability/readiness summary (the 'what's left' surface)."""
    if settings is None:
        from v3_settings import get_settings
        settings = get_settings()
    st = settings.credentials_status()
    out = {}
    for name, sp in VENUE_SPECS.items():
        out[name] = {"custody": sp["custody"], "signer": sp["signer"],
                     "exec_ready": sp["exec_ready"],
                     "creds": st.get(name, False),
                     "ladder_max": sp["ladder_max"],
                     "ladder": LADDER_NAMES[sp["ladder_max"]],
                     "public": sp["public"]}
    return out


class SignedRestClient:
    """Signed-REST request builder per venue spec.

    dry_run=True (default): nothing leaves the process; the signed request
    is fully materialized (query/headers/body) for tests + parity, and a
    row lands in the intent log.  Real send requires the triple gate above
    and an implemented signer; it reuses the caller's aiohttp session.
    """

    def __init__(self, venue, settings, dry_run=True, ilog=None):
        sp = spec_for(venue)
        if sp is None:
            raise ValueError(f"no venue spec for {venue!r}")
        if sp["signer"] not in SIGNERS:
            raise NotImplementedError(
                f"{venue}: signer not shipped yet ({sp['signer']}). "
                f"See Part 24 integration guide, section {venue}.")
        self.venue, self.sp = venue, sp
        self.signer = SIGNERS[sp["signer"]]()
        self.settings = settings
        self.dry_run = dry_run
        self.ilog = ilog or exa.IntentLog()

    def _require_real(self):
        if self.dry_run:
            return
        if getattr(self.settings, "DRY_RUN", True):
            raise RuntimeError(f"{self.venue}: DRY_RUN is on - real send "
                               "refused")
        if not getattr(self.settings, "YES_REAL", False):
            raise RuntimeError(f"{self.venue}: YES_REAL=false - real send "
                               "refused")
        if self.sp["ladder_max"] < 3:
            raise RuntimeError(f"{self.venue}: ladder_max="
                               f"{self.sp['ladder_max']} - execution path "
                               "not shipped for this venue")

    def _cred_pair(self):
        """(key, secret, extras) from the CredentialSet; fields[0]/[1] hold
        key/secret for every implemented-signer venue (okx extras carry the
        passphrase)."""
        creds = self.settings.require_credentials(self.venue)
        env = self.sp["env"]
        if len(env) < 2:
            raise RuntimeError(f"{self.venue}: spec expects 2+ fields")
        sv = lambda n: creds.fields[n].get_secret_value()  # noqa: E731
        extras = {}
        if self.sp["signer"] == "okx":
            extras["passphrase"] = sv("OKX_PASSPHRASE")
        if self.sp["signer"] == "bybit":
            extras["recv_window"] = "5000"
        return sv(env[0]), sv(env[1]), extras

    def build(self, method, path, params=None, body=None):
        """Materialize the signed request (network-free)."""
        key, secret, extras = self._cred_pair()
        ts = str(int(time.time() * 1000))
        q, headers, body_out = self.signer.sign(
            method, path, params, body, key, secret, ts, **extras)
        return {"method": method, "path": path, "query": q,
                "body": body_out, "headers": headers, "ts": ts}

    async def request(self, session, method, path, params=None, body=None,
                      note=""):
        b = self.build(method, path, params,
                       json.dumps(body) if isinstance(body, dict) else body)
        q, headers, body_out = b["query"], b["headers"], b["body"]
        full = f"{self.sp['rest']}{path}?{q}" if q else \
            f"{self.sp['rest']}{path}"
        self.ilog.write({"mode": "venue_client", "venue": self.venue,
                         "op": f"{method} {path}", "dry_run": self.dry_run,
                         "note": note})
        self._require_real()
        if self.dry_run:
            return {"dry_run": True, "url": full, "headers": headers,
                    "body": body_out}
        async with session.request(method, full, headers=headers,
                                   data=body_out) as r:
            return {"status": r.status, "json": await r.json()}


def _selftest():
    """Network-free smoke: registry integrity + signer vectors."""
    assert len(VENUE_SPECS) >= 28, len(VENUE_SPECS)
    for n, sp in VENUE_SPECS.items():
        assert sp["custody"] in "ABCD" and sp["docs"].startswith("http")
        assert 0 <= sp["ladder_max"] <= 4
    sg = BinanceStyleSigner()
    q, h, b = sg.sign("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"},
                      None, "K", "S", "123")
    assert q.startswith("symbol=BTCUSDT&timestamp=123&signature=") and \
        h["X-MBX-APIKEY"] == "K" and b == ""
    so = OkxSigner()
    q, h, b = so.sign("POST", "/api/v5/trade/order", {}, '{"a":1}',
                      "K", "S", "123", passphrase="P")
    assert h["OK-ACCESS-SIGN"] == _hmac_b64(
        "S", "123POST/api/v5/trade/order" + '{"a":1}')
    assert h["OK-ACCESS-PASSPHRASE"] == "P"
    sb = BybitSigner()
    q, h, b = sb.sign("POST", "/v5/order/create", None, '{"a":1}',
                      "K", "S", "123")
    assert h["X-BAPI-SIGN"] == _hmac_hex("S", "123K5000" + '{"a":1}')
    print("v3_venue_adapters selftest OK")


if __name__ == "__main__":
    _selftest()
