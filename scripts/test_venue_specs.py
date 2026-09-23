#!/usr/bin/env python3
"""test_venue_specs.py - P5d capability layer, network-free.

Covers: VENUE_SPECS integrity + settings cross-consistency, signer signature
vectors (independent recomputation), credential completeness semantics,
SignedRestClient dry-run materialization + triple-gate refusals, .env.example
template parity, connector registry additions (dispatch + FEES + scanner).
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import v3_settings as vs          # noqa: E402
import v3_venue_adapters as va    # noqa: E402
import v3_connectors as vc        # noqa: E402
import v3_exec_adapter as exa     # noqa: E402

N = [0, 0]


def ck(name, cond):
    N[0] += 1
    if cond:
        N[1] += 1
        print(f"  ok  {name}")
    else:
        print(f"FAIL  {name}")


def expect_err(name, fn, *frag):
    N[0] += 1
    try:
        fn()
    except Exception as e:
        ok = all(f in str(e) for f in frag)
        if ok:
            N[1] += 1
            print(f"  ok  {name}")
        else:
            print(f"FAIL  {name}: wrong error: {e}")
    else:
        print(f"FAIL  {name}: no error raised")


def sig_hex(secret, msg):
    return hmac.new(secret.encode(), msg.encode(),
                    hashlib.sha256).hexdigest()


def sig_b64(secret, msg):
    return base64.b64encode(hmac.new(secret.encode(), msg.encode(),
                                     hashlib.sha256).digest()).decode()


# ------------------------------------------------ 1. registry integrity
print("[1] VENUE_SPECS integrity")
ck("registry size >= 28", len(va.VENUE_SPECS) >= 28)
bad = []
for name, sp in va.VENUE_SPECS.items():
    if sp["custody"] not in "ABCD" or not sp["docs"].startswith("http"):
        bad.append(name)
    if not (0 <= sp["ladder_max"] <= 4):
        bad.append(name)
    if sp["signer"] in va.SIGNERS and not sp["exec_ready"]:
        bad.append(name + ":shipped-not-ready")
    if sp["signer"].startswith("pending") and sp["exec_ready"]:
        bad.append(name + ":pending-but-ready")
ck("all specs valid custody/docs/ladder/ready-consistency", not bad)
mismatch = [n for n, sp in va.VENUE_SPECS.items()
            if n in vs.VENUE_FIELDS
            and list(sp["env"]) != list(vs.VENUE_FIELDS[n])]
ck("spec env fields == VENUE_FIELDS for all shared venues", not mismatch)
ck("every settings VENUES has a spec",
   all(v in va.VENUE_SPECS for v in vs.VENUES))
ck("implemented-signer venues have order paths",
   all(sp["signer"] in va.ORDER_PATHS
       for sp in va.VENUE_SPECS.values() if sp["exec_ready"]))
ck("backpack ceiling 0 (fee wall)",
   va.VENUE_SPECS["backpack"]["ladder_max"] == 0)
ck("edgex ceiling 0 (EDGE stake)",
   va.VENUE_SPECS["edgex"]["ladder_max"] == 0)
ck("aster spec documents V3-only",
   "V3" in va.VENUE_SPECS["aster"]["note"])
ck("adapter_status shape", set(
    va.adapter_status(settings=vs.V3Settings(_env_file=None))["binance"]
) >= {"custody", "signer", "exec_ready", "creds", "ladder_max", "ladder",
      "public"})

# ------------------------------------------------ 2. signer vectors
print("[2] signer signature vectors (independent recomputation)")
sg = va.BinanceStyleSigner()
q, h, b = sg.sign("POST", "/fapi/v1/order", {"symbol": "BTCUSDT",
                                             "quantity": "0.01"},
                  None, "KEY1", "SEC1", "111")
want_q = ("quantity=0.01&symbol=BTCUSDT&timestamp=111" if
          list({"symbol": "BTCUSDT", "quantity": "0.01"})[0] == "symbol"
          else None)
ck("binance: hex hmac over urlencoded query + ts",
   q.endswith("&signature=" + sig_hex("SEC1", q.split("&signature=")[0]))
   and "timestamp=111" in q and h["X-MBX-APIKEY"] == "KEY1" and b == "")
so = va.OkxSigner()
q, h, b = so.sign("get", "/account/balance", {"ccy": "USDT"}, None,
                  "KEY2", "SEC2", "222", passphrase="PP")
ck("okx: b64 hmac over ts+METHOD+path+query",
   h["OK-ACCESS-SIGN"] == sig_b64("SEC2", "222GET/account/balance?ccy=USDT")
   and h["OK-ACCESS-TIMESTAMP"] == "222"
   and h["OK-ACCESS-PASSPHRASE"] == "PP")
sb = va.BybitSigner()
q, h, b = sb.sign("POST", "/v5/order/create", None, '{"sym":"BTC"}',
                  "KEY3", "SEC3", "333")
ck("bybit: hex hmac over ts+key+recv+body",
   h["X-BAPI-SIGN"] == sig_hex("SEC3", "333KEY35000" + '{"sym":"BTC"}')
   and h["X-BAPI-TIMESTAMP"] == "333")

# ------------------------------------------------ 3. credentials semantics
print("[3] credentials completeness (SecretStr-safe)")
S = vs.V3Settings
full = S(_env_file=None, BINANCE_API_KEY="k", BINANCE_API_SECRET="s")
ck("complete -> CredentialSet with SecretStr fields",
   full.credentials("binance") is not None
   and full.credentials("binance").fields["BINANCE_API_KEY"]
   .get_secret_value() == "k")
ck("repr never leaks", "k***" in repr(full.credentials("binance"))
   or "k" not in repr(full.credentials("binance"))[10:])
part = S(_env_file=None, BINANCE_API_KEY="k", BINANCE_API_SECRET="")
expect_err("partial -> loud error naming the missing field",
           lambda: part.credentials("binance"), "BINANCE_API_SECRET")
none_ = S(_env_file=None)
ck("unset -> None", none_.credentials("binance") is None)
ck("status: unset venue False, complete venue True",
   none_.credentials_status()["binance"] is False
   and full.credentials_status()["binance"] is True)
ck("status covers all 29 VENUES",
   len(none_.credentials_status()) == 29)
mne = S(_env_file=None, DYDX_MNEMONIC="w1 w2 w3")
ck("single-field venue (dydx mnemonic) complete",
   mne.credentials("dydx") is not None)

# ------------------------------------------------ 4. SignedRestClient gates
print("[4] SignedRestClient dry-run + triple gate")
settings = S(_env_file=None, BINANCE_API_KEY="k", BINANCE_API_SECRET="s",
             EXEC_MODE="live", DRY_RUN=True, YES_REAL=False)
async def _client_checks():
    ilog = exa.IntentLog(os.path.join(os.path.dirname(exa.DEFAULT_INTENT_LOG),
                                      "test_vspec_intents.jsonl"))
    c = va.SignedRestClient("binance", settings, dry_run=True, ilog=ilog)
    b = c.build("POST", "/fapi/v1/order",
                {"symbol": "BTCUSDT", "side": "BUY"})
    ck("dry-run build materializes signed query",
       "signature=" in b["query"] and b["headers"]["X-MBX-APIKEY"] == "k")
    r = await c.request(None, "POST", "/fapi/v1/order",
                        {"symbol": "BTCUSDT"}, note="t")
    ck("dry-run request returns shadow, no send",
       r.get("dry_run") is True and "signature=" in r["url"])

    async def _refuses(client, tag, frag):
        try:
            await client.request(None, "POST", "/fapi/v1/order", {})
            ck(tag, False)
        except RuntimeError as e:
            ck(tag, frag in str(e))

    # real-path refusals
    c2 = va.SignedRestClient("binance", settings, dry_run=False, ilog=ilog)
    await _refuses(c2, "DRY_RUN=true blocks real send", "DRY_RUN")
    s2 = S(_env_file=None, BINANCE_API_KEY="k", BINANCE_API_SECRET="s",
           EXEC_MODE="live", DRY_RUN=False, YES_REAL=False)
    c3 = va.SignedRestClient("binance", s2, dry_run=False, ilog=ilog)
    await _refuses(c3, "YES_REAL=false blocks real send", "YES_REAL")
    expect_err("pending signer -> NotImplementedError with pointer",
               lambda: va.SignedRestClient("hl", s2, dry_run=True),
               "signer not shipped")
    expect_err("unknown venue -> ValueError",
               lambda: va.SignedRestClient("nope", s2, dry_run=True),
               "no venue spec")
asyncio.run(_client_checks())

# ------------------------------------------------ 5. template parity
print("[5] .env.example template parity")
ck("example loadable", vs.example_is_loadable())
gaps = vs.missing_from_example()
ck("no missing template vars", gaps == [])
ck("example documents every VENUE_FIELDS var",
   all(n in open(vs.EXAMPLE_FILE).read()
       for ns in vs.VENUE_FIELDS.values() for n in ns))

# ------------------------------------------------ 6. connector additions
print("[6] connector registry additions (P5d Wave-0)")
for v in ("gate", "kucoin", "bitmex", "deribit", "htx"):
    ck(f"funding fn _f_{v} shipped", hasattr(vc, f"_f_{v}"))
for v in ("gate", "kucoin", "deribit", "paradex"):
    ck(f"book fn _book_{v} shipped", hasattr(vc, f"_book_{v}"))
for v, fee in (("mexc", (5.0, 1.0)), ("gate", (5.0, 2.0)),
               ("kucoin", (6.0, 2.0)), ("bitmex", (7.5, -2.5))):
    ck(f"FEES verified row {v}", vc.FEES.get(v) == fee)
ck("fee accessor roles", vc.get_fee("mexc", "taker") == 5.0
   and vc.get_fee("mexc", "maker") == 1.0)
import v3_scanner  # noqa: E402
for v in ("gate", "kucoin", "bitmex", "deribit", "htx", "paradex"):
    ck(f"scanner OUR_VENUES includes {v}", v in v3_scanner.OUR_VENUES)

# ------------------------------------------------ summary
print(f"\ntest_venue_specs: {N[1]}/{N[0]} PASS")
sys.exit(0 if N[1] == N[0] else 1)
