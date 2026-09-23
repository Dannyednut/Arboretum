#!/bin/bash
# P5d venue-integration research batch (Part 23) - cached to scripts/search_results/
cd /home/z/my-project/scripts/search_results || exit 1
run() { # slug, query
  [ -s "p5d_$1.json" ] && { echo "skip $1"; return; }
  z-ai function -n web_search -a "{\"query\": \"$2\", \"num\": 4}" -o "p5d_$1.json" >/dev/null 2>&1 \
    && echo "ok   $1" || echo "FAIL $1"
}
# --- DEX: agent/wallet-derived key class (execution paths)
run lighter_auth    "Lighter perp DEX API documentation L2 API key authentication"
run lighter_funding "Lighter DEX perpetuals funding rate interval hours"
run paradex_auth    "Paradex perp DEX API documentation authentication session key"
run paradex_funding "Paradex perpetuals funding rate interval"
run edgex_auth      "edgeX exchange API documentation authentication L2 key"
run apex_auth       "ApeX Omni API key documentation authentication trading"
run extended_auth   "Extended exchange crypto perps API documentation authentication"
run extended_fund   "Extended exchange X10 perps funding rate interval"
run dydx_exec       "dYdX v4 trading execution wallet mnemonic gRPC client documentation"
run drift_exec      "Drift protocol Solana trading SDK keypair documentation"
run nado_exec       "Nado perp DEX API authentication session key signing documentation"
run hl_agent        "Hyperliquid API wallet agent approval documentation"
run hip3            "Hyperliquid HIP-3 builder deployed perp markets API"
run orderly_exec    "Orderly Network API authentication signing key documentation"
run backpack_exec   "Backpack exchange API documentation authentication"
run aster_exec      "Aster DEX API key trading documentation"
# --- CEX: API-key class
run mexc            "MEXC futures API key permissions trade funding rate documentation"
run gate            "Gate.io perpetual futures API key authentication funding interval"
run kucoin          "KuCoin futures API key funding rate interval documentation"
run htx             "HTX Huobi perpetual swap API key funding rate documentation"
run kraken          "Kraken futures perpetual API funding rate interval documentation"
run bitmex          "BitMEX API key permissions testnet funding rate"
run deribit         "Deribit perpetual futures funding rate interval API"
run blofin          "BloFin futures API documentation funding rate permissions"
run whitebit        "WhiteBIT futures API funding rate documentation"
run toobit          "Toobit API documentation funding rate futures"
run weex            "WEEX futures API documentation funding rate"
run cbintl          "Coinbase International Exchange perpetual futures API funding"
run sweep           "small crypto exchanges extreme funding rates perp arbitrage venues 2026"
echo DONE
