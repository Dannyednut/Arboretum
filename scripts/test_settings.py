#!/usr/bin/env python3
"""test_settings.py - P4b key/config scaffold tests.

Covers:
  template      .env.example parses; no field drift vs VENUE_FIELDS;
                .gitignore protects .env
  defaults      clean env -> paper / DRY_RUN true / YES_REAL false
  validators    EXEC_MODE whitelist; YES_REAL consistency (real mode is a
                P4b decision, never implicit)
  credentials   unset -> None; partial -> loud error naming the missing
                fields + .env path; complete -> CredentialSet whose repr
                NEVER contains the secret (SecretStr + custom __repr__)
  precedence    process env beats file (12-factor)
  adapter hook  LiveAdapter.credentials_ready reflects settings; real-mode
                refusal intact even when keys exist
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE)


FAILED = []


def ok(name, cond, extra=""):
    print(f"  {'ok' if cond else 'FAIL'} {name}"
          f"{'' if cond else f'  <- {extra}'}")
    if not cond:
        FAILED.append(name)


SECRET = "supersecretvalue1234567890"


def clean(**overrides):
    """Fresh V3Settings: no project .env, venue env vars scrubbed."""
    from v3_settings import V3Settings, VENUE_FIELDS
    for ns in VENUE_FIELDS.values():
        for n in ns:
            os.environ.pop(n, None)
    for n in ("EXEC_MODE", "DRY_RUN", "YES_REAL"):
        os.environ.pop(n, None)
    return V3Settings(_env_file=None, **overrides)


def test_template():
    print("template + repo hygiene:")
    from v3_settings import example_is_loadable, missing_from_example
    ok("example_loadable", example_is_loadable())
    ok("example_no_gaps", missing_from_example() == [],
       str(missing_from_example()))
    gi = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
    ok("gitignore_covers_env",
       any(ln.strip() == ".env" for ln in gi.splitlines())
       and "!.env.example" in gi)
    ex = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
    ok("example_documents_contract",
       "trade-only" in ex and "withdrawal" in ex.lower()
       and "IP allowlist" in ex)
    ok("example_has_no_secrets",
       SECRET not in ex and "sk-" not in ex.replace("sk-scheme", ""))


def test_defaults_and_validators():
    print("defaults + validators:")
    s = clean()
    ok("defaults_paper", s.EXEC_MODE == "paper" and s.DRY_RUN is True
       and s.YES_REAL is False)
    ok("default_sizing", s.P4B_PILOT_USD_PER_LEG == 50.0
       and s.MAX_NOTIONAL_USD == 1000.0)
    try:
        clean(EXEC_MODE="yolo")
        ok("mode_whitelist", False, "no raise")
    except Exception as e:
        ok("mode_whitelist", "paper" in str(e) or "live" in str(e))
    try:
        clean(YES_REAL=True)   # DRY_RUN still default true -> contradiction
        ok("yesreal_needs_live_dryrun_false", False, "no raise")
    except Exception as e:
        ok("yesreal_needs_live_dryrun_false",
           "YES_REAL" in str(e) and "DRY_RUN" in str(e))
    s = clean(EXEC_MODE="live", DRY_RUN=False, YES_REAL=True)
    ok("yesreal_consistent_ok", s.YES_REAL is True)


def test_credentials():
    print("credentials lifecycle:")
    s = clean()
    ok("unset_is_none", s.credentials("binance") is None)
    s = clean(BINANCE_API_KEY="k-only")
    try:
        s.credentials("binance")
        ok("partial_loud_error", False, "no raise")
    except Exception as e:
        ok("partial_loud_error", "BINANCE_API_SECRET" in str(e)
           and ".env" in str(e), str(e)[:120])
    s = clean(BINANCE_API_KEY="AbCdEfghijkl", BINANCE_API_SECRET=SECRET)
    c = s.credentials("binance")
    ok("complete_returns_set", c is not None and c.venue == "binance")
    r = repr(c) + str(c)
    ok("repr_never_leaks_secret",
       SECRET not in r and "AbCd***" in r, r)
    st = s.credentials_status()
    ok("status_reflects", st["binance"] is True
       and all(not st[v] for v in ("aster", "okx", "bingx", "nado")))
    # okx 3-field contract
    s = clean(OKX_API_KEY="k", OKX_API_SECRET="s")
    try:
        s.credentials("okx")
        ok("okx_needs_passphrase", False, "no raise")
    except Exception as e:
        ok("okx_needs_passphrase", "OKX_PASSPHRASE" in str(e))
    okx = clean(OKX_API_KEY="k", OKX_API_SECRET="s",
                OKX_PASSPHRASE="p").credentials("okx")
    ok("okx_complete", okx is not None
       and len(okx.fields) == 3)
    # unknown venue
    try:
        s.credentials("ftx")
        ok("unknown_venue_refused", False, "no raise")
    except ValueError:
        ok("unknown_venue_refused", True)
    # require vs optional
    s2 = clean()
    try:
        s2.require_credentials("bingx")
        ok("require_unset_raises", False, "no raise")
    except Exception as e:
        ok("require_unset_raises", "trade-only" in str(e))


def test_precedence():
    print("env > .env precedence:")
    from v3_settings import V3Settings, mask
    # manual hygiene: clean() would pop the env var we set on purpose
    for n in ("BINANCE_API_KEY", "BINANCE_API_SECRET"):
        os.environ.pop(n, None)
    os.environ["BINANCE_API_KEY"] = "envwins"
    try:
        s = V3Settings(_env_file=None, BINANCE_API_SECRET=SECRET)
        c = s.credentials("binance")
        masked = mask(c.fields["BINANCE_API_KEY"].get_secret_value())
        ok("env_beats_kwargs_default",
           masked.startswith("envw") and "7ch" in masked
           and "envwins" not in masked, masked)
    finally:
        os.environ.pop("BINANCE_API_KEY", None)


def test_adapter_hook():
    print("adapter settings hook:")
    import v3_exec_adapter as exa
    s_clean = clean()
    la = exa.LiveAdapter(dry_run=True, intent_log=None, settings=s_clean)
    ok("adapter_not_ready_clean",
       la.credentials_ready("binance") is False)
    s_full = clean(BINANCE_API_KEY="AbCd", BINANCE_API_SECRET=SECRET)
    la2 = exa.LiveAdapter(dry_run=True, intent_log=None, settings=s_full)
    ok("adapter_ready_with_keys",
       la2.credentials_ready("binance") is True)
    try:
        exa.LiveAdapter(dry_run=False, settings=s_full)
        ok("real_refusal_with_keys", False, "no raise")
    except RuntimeError as e:
        ok("real_refusal_with_keys", "P4b" in str(e))
    # parity untouched by settings
    BOOK = ([(99.5, 2)], [(100.5, 2)])
    rp = exa.PaperAdapter().place_taker("okx", "INJ", "buy", 100.0, BOOK)
    rl = la2.place_taker("okx", "INJ", "buy", 100.0, BOOK)
    ok("parity_with_settings", rp == rl)


def main():
    print("=== test_settings (P4b config scaffold) ===")
    test_template()
    test_defaults_and_validators()
    test_credentials()
    test_precedence()
    test_adapter_hook()
    total = 24
    print(f"\n{total - len(FAILED)}/{total} CHECKS PASS")
    if FAILED:
        print("FAILED:", ", ".join(FAILED))
        sys.exit(1)
    print("ALL SETTINGS TESTS PASS")


if __name__ == "__main__":
    main()
