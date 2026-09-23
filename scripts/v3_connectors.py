#!/usr/bin/env python3
"""v3 M1 connectors (spec Part 12 §3.1): one interface over all venues.

  get_book(venue, base)            -> (bids, asks)          [async]
  get_funding(venue, base)         -> {rate, interval_h, next_time, source}
  get_fee(venue, role)             -> bps                    [verified table]

Native live funding for binance/aster/okx/bybit/bitget/hl/dydx/bingx/
backpack; Sharpe fallback for nado/orderly (native probes are open items
12 §6.1/§6.2). Sharpe connector stores provenance (X-Data-As-Of) and
computes staleness; cross_check() flags >10% Sharpe-vs-native disagreement
(the Part-10 Gate lesson).
"""
import asyncio
from datetime import datetime, timezone

import aiohttp

import depth_sampler as ds
import depth_newvenue as nv

# ------------------------------------------------ fees: Part-12 §2.1 table
FEES = {  # venue -> (taker_bps, maker_bps)
    "aster":    (4.0, 0.0),
    "hl":       (4.5, 1.5),
    "binance":  (5.0, 2.0),
    "okx":      (5.0, 2.0),
    "dydx":     (5.0, 2.0),
    "bybit":    (5.5, 2.0),
    "bitget":   (6.0, 2.0),
    "bingx":    (5.0, 2.0),
    "nado":     (3.5, 1.0),
    "orderly":  (3.0, 0.0),
    "backpack": (9.5, 8.5),
    # P5d (Part 23/24): official-source rows only
    "mexc":     (5.0, 1.0),    # 2026-03-31 official API announcement: taker 0.05% / maker 0.01%; re-verify at key time
    "gate":     (5.0, 2.0),    # gate v4 standard USDT futures schedule
    "kucoin":   (6.0, 2.0),    # kucoin futures standard schedule
    "bitmex":   (7.5, -2.5),   # bitmex perp schedule incl. maker rebate
}


def get_fee(venue, role="taker"):
    return FEES[venue][0 if role == "taker" else 1]


_session: aiohttp.ClientSession | None = None


async def _get(url, params=None, timeout=20):
    async with _session.get(url, params=params,
                            timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.json(content_type=None), dict(r.headers)


async def _post(url, payload, timeout=20):
    async with _session.post(url, json=payload,
                             timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.json(content_type=None), dict(r.headers)


def _iv_from_hist(rows):
    """[(ts_ms, rate)] -> median interval hours (Part-4 infer rule)."""
    if len(rows) < 3:
        return None
    ds_ = sorted((rows[i + 1][0] - rows[i][0]) / 3600_000
                 for i in range(len(rows) - 1))
    ds_ = [d for d in ds_ if 0 < d <= 48]
    return ds_[len(ds_) // 2] if ds_ else None


# ------------------------------------------------------------- books
async def get_book(venue, base):
    return await pl_book(venue, base)


async def pl_book(venue, base):
    if venue == "binance":
        return await ds.book_binance(base)
    if venue == "okx":
        return await ds.book_okx(base)
    if venue == "bybit":
        return await ds.book_bybit(base)
    if venue == "bitget":
        return await ds.book_bitget(base)
    if venue == "aster":
        return await ds.book_aster(base)
    if venue == "hl":
        return await ds.book_hl(base)
    if venue == "dydx":
        return await ds.book_dydx(base)
    if venue == "bingx":
        return await nv.book_bingx(base)
    if venue == "backpack":
        return await nv.book_backpack(base)
    if venue == "nado":
        return await nv.book_nado(base)
    if venue == "gate":
        return await _book_gate(base)
    if venue == "kucoin":
        return await _book_kucoin(base)
    if venue == "deribit":
        return await _book_deribit(base)
    if venue == "paradex":
        return await _book_paradex(base)
    raise RuntimeError(f"no book fetcher for {venue}")   # htx/bitmex: funding-only (P5d)


# ----------------------------------------------------------- live funding
async def get_funding(venue, base):
    """Native live funding; Sharpe fallback where native is open-item."""
    fn = {"binance": _f_binance, "aster": _f_aster, "okx": _f_okx,
          "bybit": _f_bybit, "bitget": _f_bitget, "hl": _f_hl,
          "dydx": _f_dydx, "bingx": _f_bingx, "backpack": _f_backpack,
          "gate": _f_gate, "kucoin": _f_kucoin, "bitmex": _f_bitmex,
          "deribit": _f_deribit, "htx": _f_htx}   # P5d Wave-0
    if venue in fn:
        try:
            return await fn[venue](base)
        except Exception as e:
            print(f"  native funding {venue} {base}: ERR {str(e)[:70]}")
    return await sharpe_one(venue, base)


async def _f_binance(base):
    s = base + "USDT"
    j, _ = await _get("https://fapi.binance.com/fapi/v1/premiumIndex",
                      params={"symbol": s})
    h, _ = await _get("https://fapi.binance.com/fapi/v1/fundingRate",
                      params={"symbol": s, "limit": 4})
    rows = [(r["fundingTime"], float(r["fundingRate"])) for r in h]
    return {"rate": float(j["lastFundingRate"]),
            "interval_h": _iv_from_hist(rows) or 8.0,
            "next_time": j.get("nextFundingTime"), "source": "native"}


async def _f_aster(base):
    if base not in ds.ASTER_SYMS:  # ensure symbol map is loaded
        info, _ = await _get("https://fapi.asterdex.com/fapi/v1/exchangeInfo")
        for x in info.get("symbols", []):
            if x.get("status") == "TRADING":
                ds.ASTER_SYMS[x["baseAsset"].upper()] = x["symbol"]
    s = ds.ASTER_SYMS.get(base, base + "USDT")
    j, _ = await _get("https://fapi.asterdex.com/fapi/v1/premiumIndex",
                      params={"symbol": s})
    h, _ = await _get("https://fapi.asterdex.com/fapi/v1/fundingRate",
                      params={"symbol": s, "limit": 4})
    rows = [(r["fundingTime"], float(r["fundingRate"])) for r in h]
    return {"rate": float(j["lastFundingRate"]),
            "interval_h": _iv_from_hist(rows) or 1.0,
            "next_time": j.get("nextFundingTime"), "source": "native"}


async def _f_okx(base):
    inst = base + "-USDT-SWAP"
    j, _ = await _get("https://www.okx.com/api/v5/public/funding-rate",
                      params={"instId": inst})
    d = j["data"][0]
    h, _ = await _get("https://www.okx.com/api/v5/public/funding-rate-history",
                      params={"instId": inst, "limit": 4})
    rows = [(int(r["fundingTime"]), float(r["fundingRate"])) for r in h["data"]]
    return {"rate": float(d["fundingRate"]),
            "interval_h": _iv_from_hist(rows) or 8.0,
            "next_time": d.get("nextFundingTime"), "source": "native"}


async def _f_bybit(base):
    s = base + "USDT"
    j, _ = await _get("https://api.bybit.com/v5/market/tickers",
                      params={"category": "linear", "symbol": s})
    d = j["result"]["list"][0]
    h, _ = await _get("https://api.bybit.com/v5/market/funding/history",
                      params={"category": "linear", "symbol": s, "limit": 4})
    rows = [(int(r["fundingRateTimestamp"]), float(r["fundingRate"]))
            for r in h["result"]["list"]]
    return {"rate": float(d["fundingRate"]),
            "interval_h": _iv_from_hist(rows) or 8.0,
            "next_time": d.get("nextFundingTime"), "source": "native"}


async def _f_bitget(base):
    s = base + "USDT"
    j, _ = await _get("https://api.bitget.com/api/v2/mix/market/"
                      "current-fund-rate",
                      params={"productType": "usdt-futures", "symbol": s})
    d = j["data"][0]
    h, _ = await _get("https://api.bitget.com/api/v2/mix/market/"
                      "history-fund-rate",
                      params={"productType": "usdt-futures", "symbol": s,
                              "pageSize": 4})
    rows = [(int(r["fundingTime"]), float(r["fundingRate"])) for r in h["data"]]
    return {"rate": float(d["fundingRate"]),
            "interval_h": _iv_from_hist(rows) or 8.0,
            "next_time": None, "source": "native"}


async def _f_hl(base):
    j, _ = await _post("https://api.hyperliquid.xyz/info",
                       {"type": "metaAndAssetCtxs"})
    uni, ctxs = j[0]["universe"], j[1]
    for x, c in zip(uni, ctxs):
        if x["name"].upper() == base:
            return {"rate": float(c["funding"]),
                    "interval_h": 1.0, "next_time": None, "source": "native"}
    raise RuntimeError(f"{base} not on hl")


async def _f_dydx(base):
    t = base + "-USD"
    j, _ = await _get("https://indexer.dydx.trade/v4/perpetualMarkets",
                      params={"ticker": t})
    d = (j.get("markets") or {}).get(t) if isinstance(j, dict) else None
    if d is None:
        raise RuntimeError(f"dydx {t}: no markets in response "
                           f"(transient indexer error)")
    # dydx funding is hourly by design
    return {"rate": float(d.get("nextFundingRate") or 0),
            "interval_h": 1.0, "next_time": None, "source": "native"}


async def _f_bingx(base):
    s = base + "-USDT"
    j, _ = await _get("https://open-api.bingx.com/openApi/swap/v2/quote/"
                      "premiumIndex", params={"symbol": s})
    d = j["data"]
    h, _ = await _get("https://open-api.bingx.com/openApi/swap/v2/quote/"
                      "fundingRate", params={"symbol": s, "limit": 4})
    rows = []
    if isinstance(h, dict) and isinstance(h.get("data"), list):
        rows = [(int(r["fundingTime"]), float(r["fundingRate"]))
                for r in h["data"] if r.get("fundingTime")]
    return {"rate": float(d["lastFundingRate"]),
            "interval_h": _iv_from_hist(rows),
            "next_time": None, "source": "native"}


async def _f_backpack(base):
    s = base + "_USDC_PERP"
    j, _ = await _get("https://api.backpack.exchange/api/v1/fundingRates",
                      params={"symbol": s})
    rows = []
    if isinstance(j, list):
        for r in j:
            ts = r.get("fundingTimestamp") or r.get("fundingTime") \
                or r.get("timestamp")
            if ts:
                rows.append((int(ts), float(r.get("fundingRate") or 0)))
    rate = rows[-1][1] if rows else 0.0
    return {"rate": rate, "interval_h": _iv_from_hist(rows) or 1.0,
            "next_time": None, "source": "native"}


# --------------------------------------------------------------- Sharpe
class Sharpe:
    """Discovery-grade aggregator connector with provenance (§3.1)."""

    BASE = "https://www.sharpe.ai/api"
    last_headers: dict = {}

    @classmethod
    async def _call(cls, path, params=None):
        j, hdrs = await _get(f"{cls.BASE}/{path}", params=params)
        cls.last_headers = hdrs
        asof = (hdrs.get("X-Data-As-Of") or hdrs.get("x-data-as-of"))
        stale = (hdrs.get("is_stale") or hdrs.get("Is-Stale"))
        return j, asof, stale

    @classmethod
    def staleness_s(cls):
        asof = cls.last_headers.get("X-Data-As-Of") or \
            cls.last_headers.get("x-data-as-of")
        if not asof:
            return None
        try:
            t = datetime.fromisoformat(str(asof).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - t).total_seconds()
        except ValueError:
            return None

    @classmethod
    async def current(cls, coin=None):
        params = {"type": "current"}
        if coin:
            params["coin"] = coin
        j, asof, stale = await cls._call("funding/rates", params)
        rows = j if isinstance(j, list) else (j.get("data") or [])
        out = []
        for r in rows:
            try:
                out.append({"coin": str(r.get("base_coin", "")).upper(),
                            "venue": str(r.get("exchange", "")).lower(),
                            "rate": float(r.get("rate") or 0),
                            "interval_h": float(r.get("interval_hours") or 1),
                            "mark": float(r.get("mark_price") or 0),
                            "asof": asof, "stale": stale})
            except (TypeError, ValueError):
                continue
        return out

    @classmethod
    async def history(cls, coin, days=90):
        """Rows: {'exchange', 'symbol', 'settled_at' (ISO), 'rate',
        'interval_hours'}. Free API: exchange/offset/start_time/end_time
        params IGNORED; oldest-first; 5000-row cap -> caller applies the
        two-slice strategy. Returns {venue_lower: [(ts_ms, rate, iv_h)]}."""
        j, asof, stale = await cls._call(
            "funding/rates", params={"type": "history", "coin": coin,
                                     "days": days, "limit": 5000})
        rows = j if isinstance(j, list) else (j.get("data") or [])
        out = {}
        for r in rows:
            try:
                v = str(r.get("exchange", "")).lower()
                sa = r.get("settled_at")
                if not sa:
                    continue
                t = datetime.fromisoformat(str(sa).replace("Z", "+00:00"))
                ts_ms = int(t.timestamp() * 1000)
                out.setdefault(v, []).append(
                    (ts_ms, float(r.get("rate") or 0),
                     float(r.get("interval_hours") or 0)))
            except (TypeError, ValueError):
                continue
        return out, asof, stale

    @classmethod
    async def arb_table(cls):
        j, asof, stale = await cls._call("arbitrage/cross-exchange")
        rows = j if isinstance(j, list) else (j.get("data") or [])
        return rows, asof, stale

    @classmethod
    async def listings(cls):
        j, asof, stale = await cls._call("listings/recent")
        rows = j if isinstance(j, list) else \
            (j.get("data") or j.get("rows") or [])
        return rows, asof, stale

    @classmethod
    async def settlement(cls, limit=500):
        """Dollar-weighted settlement ranking (spec §3.1d, daily).

        Rows: {base_coin, asset_class, open_interest_usd, windows}
        windows: {'current'/'1d'/...: {net, long_paid, short_paid,
                  estimated, rate_source}} - net USD paid per window."""
        j, asof, stale = await cls._call("funding/settlement",
                                         params={"limit": limit})
        rows = j if isinstance(j, list) else (j.get("data") or [])
        out = []
        for r in rows:
            try:
                out.append({"coin": str(r.get("base_coin", "")).upper(),
                            "asset_class": r.get("asset_class"),
                            "oi_usd": float(r.get("open_interest_usd") or 0),
                            "windows": r.get("windows") or {}})
            except (TypeError, ValueError):
                continue
        return out, asof, stale

    @classmethod
    async def rwa(cls):
        """RWA-perp universe sync (spec §3.1e, weekly).

        Rows carry venue/market/mechanism + funding_apr + OI +
        is_stale/is_price_suspect provenance flags (1855 rows live)."""
        j, asof, stale = await cls._call("rwa-perps/rates",
                                         params={"type": "current",
                                                 "limit": 5000})
        rows = j if isinstance(j, list) else (j.get("data") or [])
        out = []
        for r in rows:
            try:
                out.append({"venue": str(r.get("venue", "")).lower(),
                            "symbol": r.get("symbol"),
                            "market": r.get("market"),
                            "asset_class": r.get("asset_class"),
                            "mechanism": r.get("mechanism"),
                            "rate": float(r.get("funding_rate") or 0),
                            "interval_h": float(r.get("interval_hours") or 0),
                            "apr": float(r.get("funding_apr") or 0),
                            "oi_usd": float(r.get("open_interest_usd") or 0),
                            "stale": r.get("is_stale"),
                            "suspect": r.get("is_price_suspect")})
            except (TypeError, ValueError):
                continue
        return out, asof, stale


# ------------------------------------------- P5d Wave-0 native probes ------
# Part 23/24: gate, kucoin, bitmex, deribit, htx funding + gate/kucoin/
# deribit/paradex books.  Sandbox-verified 2026-09-22 (probe_p5d_endpoints).
# lighter/mexc/blofin are IP/WAF-blocked here -> Sharpe fallback (probe
# pending).  Fee policy: FEES stays a verified table; new rows only with an
# official source (mexc below = 2026-03-31 official API announcement).

async def _f_gate(base):
    s = base + "_USDT"
    j, _ = await _get("https://api.gateio.ws/api/v4/futures/usdt/contracts/"
                      + s)
    h, _ = await _get("https://api.gateio.ws/api/v4/futures/usdt/"
                      "funding_rate", params={"contract": s, "limit": 4})
    rows = [(int(r["t"]) * 1000, float(r["r"])) for r in h]
    return {"rate": float(j["funding_rate_indicative"]),
            "interval_h": _iv_from_hist(rows) or 8.0,
            "next_time": None, "source": "native"}


async def _f_kucoin(base):
    s = ("XBT" if base == "BTC" else base) + "USDTM"
    j, _ = await _get("https://api-futures.kucoin.com/api/v1/contracts/"
                      + s)
    d = j["data"]
    rate = d.get("fundingFeeRate")          # verified payload key (P5d)
    if rate is None:
        raise RuntimeError("kucoin contract payload has no fundingFeeRate")
    gran = float(d.get("fundingRateGranularity") or 28800000)
    return {"rate": float(rate),
            "interval_h": gran / 3.6e6 or 8.0,
            "next_time": d.get("nextFundingRateDateTime"),
            "source": "native"}


_BITMEX_SYMS = {}


async def _bitmex_symbol(base):
    """Resolve the active linear (USDT) perp symbol; XBT alias for BTC."""
    if base in _BITMEX_SYMS:
        return _BITMEX_SYMS[base]
    b = "XBT" if base == "BTC" else base
    j, _ = await _get("https://www.bitmex.com/api/v1/instrument",
                      params={"filter": '{"typ":"IFXXXP","state":"Open"}',
                              "columns": "symbol,state,typ"})
    # NOTE (P5d): BitMEX mid-migration to underscore symbols; XBTUSDT
    # settled 2026-09-16.  Prefer open linear perps; raise (-> Sharpe
    # fallback) when the base has none open.
    cands = [x["symbol"] for x in j
             if x.get("state") == "Open"
             and x["symbol"].replace("_", "").startswith(b + "USDT")]
    if not cands:
        raise RuntimeError(f"bitmex: no active USDT perp for {base}")
    cands.sort(key=len)     # plain SYMUSDT before suffixed variants
    _BITMEX_SYMS[base] = cands[0]
    return _BITMEX_SYMS[base]


async def _f_bitmex(base):
    s = await _bitmex_symbol(base)
    h, _ = await _get("https://www.bitmex.com/api/v1/funding",
                      params={"symbol": s, "count": 4, "reverse": True})
    if not h:
        raise RuntimeError(f"bitmex: no funding rows for {s}")
    rows = [(int(datetime.strptime(r["timestamp"][:19],
                                   "%Y-%m-%dT%H:%M:%S")
                 .replace(tzinfo=timezone.utc).timestamp() * 1000),
             float(r["fundingRate"])) for r in h]
    iv = None
    iv_s = str(h[0].get("fundingInterval") or "")   # duration as epoch+delta
    if len(iv_s) >= 19:
        iv = (int(iv_s[11:13]) + int(iv_s[14:16]) / 60.0) or None
    return {"rate": rows[0][1], "interval_h": _iv_from_hist(rows) or iv
            or 8.0, "next_time": None, "source": "native"}


_DERIBIT_SYMS = {}


async def _deribit_symbol(base):
    """BTC-PERPETUAL / SOL-PERPETUAL style name via public instruments."""
    if base in _DERIBIT_SYMS:
        return _DERIBIT_SYMS[base]
    j, _ = await _get("https://www.deribit.com/api/v2/public/"
                      "get_instruments",
                      params={"currency": base, "kind": "future",
                              "expired": "false"})
    perps = [x["instrument_name"] for x in j["result"]
             if x.get("is_active")
             and x["instrument_name"].endswith("PERPETUAL")]
    if not perps:
        raise RuntimeError(f"deribit: no perpetual for {base}")
    _DERIBIT_SYMS[base] = perps[0]
    return _DERIBIT_SYMS[base]


async def _f_deribit(base):
    """Funding from ticker (funding_1h = full rate); fallback to history
    interest_1h (deterministic component only - understates when premium
    is nonzero; noted in Part 24 guide)."""
    s = await _deribit_symbol(base)
    j, _ = await _get("https://www.deribit.com/api/v2/public/ticker",
                      params={"instrument_name": s})
    f1 = (j.get("result") or {}).get("funding_1h")
    if f1 is not None:
        return {"rate": float(f1), "interval_h": 1.0,
                "next_time": None, "source": "native"}
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    j, _ = await _get("https://www.deribit.com/api/v2/public/"
                      "get_funding_rate_history",
                      params={"instrument_name": s,
                              "start_timestamp": now_ms - 36 * 3600 * 1000,
                              "end_timestamp": now_ms, "count": 8})
    rws = j["result"]
    if not rws:
        raise RuntimeError(f"deribit: empty funding history for {s}")
    rows = [(int(r["timestamp"]),
             float(r.get("interest_1h") or 0.0)) for r in rws]
    return {"rate": rows[-1][1],
            "interval_h": _iv_from_hist(rows) or 1.0,
            "next_time": None, "source": "native"}


async def _f_htx(base):
    # USDT-margined linear swap; funding cadence fixed 8h (history endpoint
    # not verified from sandbox -> explicit default, not inferred).
    s = base + "-USDT"
    j, _ = await _get("https://api.hbdm.com/linear-swap-api/v1/"
                      "swap_funding_rate", params={"contract_code": s})
    d = j["data"]
    return {"rate": float(d["funding_rate"]), "interval_h": 8.0,
            "next_time": int(d.get("funding_time") or 0) or None,
            "source": "native"}


# ---- P5d books (qty converted to base units; per-venue multiplier docs) ----

_GATE_MULT = {}


async def _book_gate(base):
    s = base + "_USDT"
    if s not in _GATE_MULT:
        j, _ = await _get("https://api.gateio.ws/api/v4/futures/usdt/"
                          "contracts/" + s)
        _GATE_MULT[s] = float(j.get("quanto_multiplier") or 0.0001)
    mult = _GATE_MULT[s]
    j, _ = await _get("https://api.gateio.ws/api/v4/futures/usdt/"
                      "order_book", params={"contract": s, "limit": 10})
    bids = [(float(l["p"]), float(l["s"]) * mult) for l in j.get("bids", [])]
    asks = [(float(l["p"]), float(l["s"]) * mult) for l in j.get("asks", [])]
    return bids, asks


_KUCOIN_MULT = {}


async def _book_kucoin(base):
    s = ("XBT" if base == "BTC" else base) + "USDTM"
    if s not in _KUCOIN_MULT:
        j, _ = await _get("https://api-futures.kucoin.com/api/v1/contracts/"
                          + s)
        _KUCOIN_MULT[s] = float(j["data"].get("multiplier") or 0.001)
    mult = _KUCOIN_MULT[s]
    j, _ = await _get("https://api-futures.kucoin.com/api/v1/level2/"
                      "snapshot", params={"symbol": s})
    d = j["data"]
    bids = [(float(p), float(q) * mult) for p, q in d.get("bids", [])][:10]
    asks = [(float(p), float(q) * mult) for p, q in d.get("asks", [])][:10]
    return bids, asks


async def _book_deribit(base):
    """Inverse (BTC/ETH) contracts are USD-denominated: size*cs/px -> base."""
    s = await _deribit_symbol(base)
    j, _ = await _get("https://www.deribit.com/api/v2/public/"
                      "get_order_book",
                      params={"instrument_name": s, "depth": 10})
    r = j["result"]
    inverse = s.startswith(("BTC-", "ETH-"))
    cs = float(r.get("minimum_amount") or 10.0)

    def conv(levels):
        out = []
        for p, q in levels or []:
            p, q = float(p), float(q)
            out.append((p, q * cs / p if inverse else q))
        return out

    return conv(r.get("bids")), conv(r.get("asks"))


async def _book_paradex(base):
    j, _ = await _get("https://api.prod.paradex.trade/v1/orderbook/"
                      f"{base}-USD-PERP")
    bids = [(float(p), float(q)) for p, q in j.get("bids", [])][:10]
    asks = [(float(p), float(q)) for p, q in j.get("asks", [])][:10]
    return bids, asks


async def sharpe_one(venue, base):
    """Sharpe current for one venue/coin (fallback source)."""
    rows = await Sharpe.current(base)
    for r in rows:
        if r["venue"] == venue and r["coin"] == base:
            return {"rate": r["rate"], "interval_h": r["interval_h"],
                    "next_time": None, "source": "sharpe",
                    "asof": r["asof"], "stale": r["stale"]}
    raise RuntimeError(f"{base} on {venue} not in Sharpe current book")


async def cross_check(coin, venues=None, tol=0.10):
    """Sharpe-vs-native disagreement check (quarantine if >tol)."""
    legs = {}
    for v in (venues or ["binance", "okx", "bybit", "bitget", "aster",
                         "hl", "dydx", "bingx", "backpack", "gate",
                         "kucoin", "bitmex", "deribit", "htx"]):
        try:
            legs[v] = await get_funding(v, coin)
        except Exception:
            pass
    sh = {}
    try:
        for r in await Sharpe.current(coin):
            if r["coin"] == coin:
                sh[r["venue"]] = r
    except Exception:
        pass
    out = []
    for v, n in legs.items():
        s = sh.get(v)
        if not s or not n.get("interval_h"):
            continue
        a_n = n["rate"] / n["interval_h"] * 24 * 365
        a_s = s["rate"] / s["interval_h"] * 24 * 365
        diff = abs(a_n - a_s) / max(abs(a_n), 1e-9)
        out.append({"venue": v, "native_apr": a_n, "sharpe_apr": a_s,
                    "rel_diff": diff, "quarantine": diff > tol})
    return out


_owned_session = False


async def init():
    """Load symbol maps needed by book/funding fetchers.

    Adopts a caller-managed live session (e.g. paper_ledger's) instead of
    creating a second one; close() then leaves that session untouched."""
    global _session, _owned_session
    if _session is None:
        if ds._session is not None and not ds._session.closed:
            _session = ds._session
            _owned_session = False
        else:
            _session = aiohttp.ClientSession(
                headers={"User-Agent": "Mozilla/5.0"})
            ds._session = _session
            nv._session = _session
            _owned_session = True
    if not ds.ASTER_SYMS:
        try:
            info, _ = await _get(
                "https://fapi.asterdex.com/fapi/v1/exchangeInfo")
            for x in info.get("symbols", []):
                if x.get("status") == "TRADING":
                    ds.ASTER_SYMS[x["baseAsset"].upper()] = x["symbol"]
        except Exception as e:
            print(f"aster symmap: ERR {e}")
    try:
        await nv.load_nado_symbols()
    except Exception as e:
        print(f"nado syms: ERR {e}")


async def close():
    global _session, _owned_session
    if _session and _owned_session:
        await _session.close()
        if ds._session is _session:
            ds._session = None
        if nv._session is _session:
            nv._session = None
    _session = None
    _owned_session = False


async def _selftest():
    await init()
    f = await get_funding("binance", "BTC")
    print("binance BTC:", f)
    f = await get_funding("okx", "INJ")
    print("okx INJ:", f)
    f = await get_funding("nado", "LINK")
    print("nado LINK (sharpe):", f)
    cc = await cross_check("INJ", venues=["binance", "okx", "bingx"])
    for r in cc:
        print("xcheck:", r)
    await close()


if __name__ == "__main__":
    asyncio.run(_selftest())
