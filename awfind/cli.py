"""awfind CLI.

    awfind config set --url https://search.example.com   # once, per machine
    awfind q "what changed in podman 5.4"
    awfind deep "cross-model KV cache transfer" --limit 20
    awfind providers
    awfind doctor
    awfind --self-test

The service origin comes from --url, then AWFIND_URL, then the config file
(~/.aither/awfind.json, or AWFIND_CONFIG) — and then nothing. No default is
guessed: a search client that silently falls back to some endpoint sends your
queries somewhere you did not choose. When no rung supplies one, the error
lists every rung it tried and the one command that ends the problem, rather
than naming two of the three and leaving you to find the file.

The token follows the same order (--token, AWFIND_TOKEN, config file) but an
absent one is legal — plenty of these services are open on a trusted network.
TLS trust comes from SSL_CERT_FILE, then REQUESTS_CA_BUNDLE, then the config
file's `ca_bundle`; a service on a private CA is the normal case here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from awfind.client import (
    MODES,
    QUERY_MAX_CHARS,
    SEARCH_FIELDS,
    Answer,
    FindClient,
    FindError,
    Result,
    search_body,
)
from awfind.config import (
    ConfigError,
    UnresolvedError,
    config_path,
    resolution_report,
    resolve_ca_bundle,
    resolve_token,
    resolve_url,
    write_config,
)

# ── self-test ──────────────────────────────────────────────────────────────
# Everything asserted here is PURE. A self-test that needs a live service is a
# self-test that gets skipped, and a skipped check is indistinguishable from a
# passing one.


def _refuses(query: str, **kwargs: object) -> bool:
    """True when search_body rejects this call.

    A named helper rather than a bare `except ValueError: pass` at each site: the
    swallowing shape is how a check that no longer checks anything still reads as
    a check.
    """
    try:
        search_body(query, **kwargs)  # type: ignore[arg-type]
    except ValueError:
        return True
    return False


def _reports_a_config_problem(path: str) -> bool:
    """True when resolving against this config file raises ConfigError.

    A named helper rather than `except ConfigError: pass` at each site, for the
    same reason `_refuses` above is one: a bare swallowing handler is how a
    check that no longer checks anything still reads as a check.
    """
    try:
        resolve_url(None, path=path)
    except ConfigError:
        return True
    except UnresolvedError:
        # Nothing was configured at all — a DIFFERENT answer from "your config
        # is broken", and reporting it as the same would hide exactly the
        # collapse this test exists to prevent.
        return False
    return False


def _self_test_resolution() -> list[str]:
    """Prove the resolution order, in a sandbox, with no network and no real home.

    Runs inside the shipped `--self-test` rather than only in the repo's pytest
    because the thing it proves — "this machine has no service URL and here is
    the file to put one in" — is a property of the MACHINE, so it has to be
    checkable on the machine that has the problem.
    """
    import tempfile

    failures: list[str] = []
    saved = {k: os.environ.get(k) for k in ("AWFIND_URL", "AWFIND_TOKEN",
                                            "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = str(Path(tmp) / "awfind.json")

            # a. Nothing anywhere: the error names EVERY rung, not two of three.
            try:
                resolve_url(None, path=cfg)
                failures.append("an unresolvable url did not raise")
            except UnresolvedError as exc:
                msg = str(exc)
                for needle in ("--url", "AWFIND_URL", cfg, "awfind config set"):
                    if needle not in msg:
                        failures.append(f"the unresolved-url error never mentions {needle!r}")

            # b. The config file is a real rung, and the flag and env outrank it.
            write_config("https://from-file.invalid", path=cfg)
            if resolve_url(None, path=cfg)[0] != "https://from-file.invalid":
                failures.append("a configured url was not used")
            os.environ["AWFIND_URL"] = "https://from-env.invalid"
            if resolve_url(None, path=cfg)[0] != "https://from-env.invalid":
                failures.append("the environment did not beat the config file")
            if resolve_url("https://from-flag.invalid", path=cfg)[0] != "https://from-flag.invalid":
                failures.append("the flag did not beat the environment")
            os.environ.pop("AWFIND_URL")

            # c. A wrong value in the file is REPORTED. Swallowed, a typo would
            #    print the same "nothing is configured" as an empty machine.
            for body, why in (('{"url": "search.example.com:1"}', "a url with no scheme"),
                              ("{not json", "an unparseable config")):
                Path(cfg).write_text(body, encoding="utf-8")
                if not _reports_a_config_problem(cfg):
                    failures.append(f"{why} was accepted rather than reported")

            # d. TLS trust comes from the environment these services already
            #    use, and NEVER resolves to "do not verify".
            Path(cfg).unlink()
            if resolve_ca_bundle(path=cfg) != (True, "default trust store"):
                failures.append("an unconfigured ca bundle is not the default trust store")
            os.environ["SSL_CERT_FILE"] = "/ca.pem"
            if resolve_ca_bundle(path=cfg)[0] != "/ca.pem":
                failures.append("SSL_CERT_FILE was ignored")
            os.environ.pop("SSL_CERT_FILE")
            os.environ["REQUESTS_CA_BUNDLE"] = "/ca2.pem"
            if resolve_ca_bundle(path=cfg)[0] != "/ca2.pem":
                failures.append("REQUESTS_CA_BUNDLE was ignored")
            os.environ.pop("REQUESTS_CA_BUNDLE")

            # e. No token is a legal answer; refusing to run without one would
            #    break every deployment that is simply open on a trusted network.
            if resolve_token(None, path=cfg) != (None, "unauthenticated"):
                failures.append("an absent token was not reported as unauthenticated")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return failures


def _self_test() -> int:
    failures: list[str] = []

    # 1. The search body is EXACTLY the declared field set. This is the rule the
    #    whole package exists for: the service takes extra="ignore", so a field
    #    this client spells wrong is DROPPED with a 200 and no error anywhere.
    #    A test asserting only "query is in there" passes on that bug.
    body = search_body("hello")
    if tuple(body) != SEARCH_FIELDS:
        failures.append(f"search_body keys {tuple(body)} != declared {SEARCH_FIELDS}")
    if set(body) != set(SEARCH_FIELDS):
        failures.append("search_body sends a field the service does not declare")

    # 2. Defaults match the service's own defaults, so omitting an argument here
    #    and omitting it there mean the same thing.
    if body != {"query": "hello", "mode": "quick", "provider": None, "limit": 10,
                "include_answer": True, "auto_learn": True, "synthesize": False}:
        failures.append(f"search_body defaults drifted: {body}")

    # 3. The refusals the service also makes, made here — with the reason, not an
    #    empty result set.
    for bad, why in ((" ", "empty"), ("", "empty"), ("x" * (QUERY_MAX_CHARS + 1), "too long")):
        if not _refuses(bad):
            failures.append(f"search_body accepted a query that is {why}")
    if len(search_body("x" * QUERY_MAX_CHARS)["query"]) != QUERY_MAX_CHARS:
        failures.append("a query exactly at the limit was refused; the bound is inclusive")

    # 4. An unknown mode is refused rather than sent. Sent, it would be ignored
    #    and silently answered as the service's default.
    if not _refuses("q", mode="sideways"):
        failures.append("an unknown mode was accepted")
    for m in MODES:
        if search_body("q", mode=m)["mode"] != m:
            failures.append(f"a valid mode {m!r} did not survive")

    # 5. Query is stripped, so " q " and "q" are one cache key rather than two.
    if search_body("  q  ")["query"] != "q":
        failures.append("query was not stripped")

    # 6. Response parsing: an absent answer is None, never "". "" would read as
    #    "it answered, emptily" — a different fact from "no answer was produced".
    if Answer({}).answer is not None:
        failures.append("absent answer should be None")
    if Answer({"answer": ""}).answer is not None:
        failures.append('empty answer should normalise to None, not ""')
    if Answer({"answer": "a"}).answer != "a":
        failures.append("a real answer was dropped")

    # 7. A response with no results is an empty Answer, not a crash — and len()
    #    reports it, so "no hits" is checkable without touching .raw.
    if len(Answer({})) != 0 or len(Answer({"results": None})) != 0:
        failures.append("a resultless response did not parse to zero results")
    two = Answer({"results": [{"title": "a"}, {"title": "b"}]})
    if len(two) != 2 or [r.title for r in two] != ["a", "b"]:
        failures.append("results did not parse or did not iterate in order")

    # 8. A missing score is 0.0, not None — callers sort on it.
    if Result({}).score != 0.0:
        failures.append("a missing score should be 0.0")

    # 9. base_url normalised; no token means no header, never an empty one (an
    #    empty Bearer is rejected differently from an absent one, which sends
    #    you debugging the wrong side).
    if FindClient("https://h/").base_url != "https://h":
        failures.append("trailing slash not trimmed from base_url")
    if FindClient("https://h").token is not None:
        failures.append("token should default to None")

    # 10. A failure RAISES. Returned as [], a dead backend would be
    #     indistinguishable from an unpopular query.
    if not issubclass(FindError, Exception):
        failures.append("FindError is not raisable")

    failures.extend(_self_test_resolution())

    for f in failures:
        print(f"  FAIL  {f}")
    if failures:
        print(f"SELF-TEST: {len(failures)} failure(s)")
        return 1
    print("  PASS  search body is exactly the declared field set, with the service's defaults")
    print("  PASS  empty/over-long queries and unknown modes are refused with a reason")
    print("  PASS  absent answer is None, missing score is 0.0, results iterate in order")
    print("  PASS  url resolves flag > env > config file, and the error names every rung")
    print("  PASS  a broken config is reported, not swallowed; TLS never resolves to unverified")
    print("SELF-TEST: awfind ok")
    return 0


# ── commands ───────────────────────────────────────────────────────────────


def _client(args: argparse.Namespace) -> FindClient:
    """Build a client from the resolution order, or raise with the whole trail.

    The exceptions propagate to `main`, which prints them: the old version
    printed and called `raise SystemExit(2)` from in here, which made the
    message untestable without capturing stderr and made the exit code a
    property of a helper rather than of the command.
    """
    cfg_path = getattr(args, "config", None)
    url, _ = resolve_url(args.url, path=cfg_path)
    token, _ = resolve_token(args.token, path=cfg_path)
    verify, _ = resolve_ca_bundle(getattr(args, "ca_bundle", None), path=cfg_path)
    return FindClient(url, token, verify=verify)


def _cmd_config(args: argparse.Namespace) -> int:
    """Read or write the per-machine config file.

    `set` is what turns "no service URL" from a recurring interruption into a
    one-time one. Without it the only durable answer is an environment
    variable the user has to re-export in every shell, every cron entry and
    every agent that spawns a subprocess — which is why, measured, nobody
    ever configured this client at all.
    """
    p = config_path(getattr(args, "config", None))
    if args.config_cmd == "path":
        print(p)
        return 0
    if args.config_cmd == "show":
        # The token is a credential. Report its presence and length; printing
        # it would put it in the scrollback of whoever asked a harmless
        # question about where the config lives.
        for line in resolution_report(args.url, args.token, path=getattr(args, "config", None)):
            print(line)
        return 0
    if args.config_cmd == "set":
        if not any((args.set_url, args.set_token, args.set_ca_bundle)):
            print("awfind config set: nothing to set — pass --url, --token or --ca-bundle",
                  file=sys.stderr)
            return 2
        where = write_config(args.set_url, args.set_token,
                             ca_bundle=args.set_ca_bundle,
                             path=getattr(args, "config", None))
        print(f"wrote {where}")
        return 0
    return 2


def _show(ans: Answer, as_json: bool) -> None:
    if as_json:
        print(json.dumps(ans.raw, indent=2))
        return
    if ans.answer:
        print(ans.answer)
        print()
    for i, r in enumerate(ans, 1):
        print(f"{i}. {r.title}\n   {r.url}")
        if r.snippet:
            print(f"   {r.snippet}")
    # Say so explicitly. Printing nothing would look like the command failed.
    if not len(ans):
        print(f"no results (provider={ans.provider or 'unknown'}) — "
              "check `awfind providers` before concluding the query is unpopular")


def main(argv: list[str] | None = None) -> int:
    # GENERATED doctor intercept (gen_aw_doctor.py) -- do not edit
    _dv = locals().get("argv")
    if (_dv if _dv is not None else __import__("sys").argv[1:])[:1] == ["doctor"]:
        from ._doctor import report
        return report()
    # GENERATED repo-state intercept (gen_aw_doctor.py) -- do not edit
    try:
        from awgit import state as _aw_state
    except Exception:
        _aw_state = None
    if _aw_state is not None:
        _sv = locals().get("argv")
        if _aw_state.cli_banner(_sv if _sv is not None else __import__("sys").argv[1:]):
            return 0
    ap = argparse.ArgumentParser(prog="awfind", description=__doc__)
    ap.add_argument("--self-test", action="store_true",
                    help="prove this client still holds its contract, offline")
    ap.add_argument("--url", help="service origin (or AWFIND_URL, or the config file)")
    ap.add_argument("--token", help="bearer token (or AWFIND_TOKEN, or the config file)")
    ap.add_argument("--ca-bundle", dest="ca_bundle",
                    help="CA bundle for the service's TLS "
                         "(or SSL_CERT_FILE / REQUESTS_CA_BUNDLE, or the config file)")
    ap.add_argument("--config", help=f"config file to use (default {config_path()})")
    ap.add_argument("--json", action="store_true", help="print the raw response")
    sub = ap.add_subparsers(dest="cmd")

    for name, help_text in (("q", "quick search"), ("deep", "deep search")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("query", nargs="+")
        p.add_argument("--limit", type=int, default=10)
        p.add_argument("--provider")

    sub.add_parser("providers", help="which backends the service has configured")
    sub.add_parser("stats", help="service counters")

    cfg = sub.add_parser("config", help="where this machine looks for the service")
    csub = cfg.add_subparsers(dest="config_cmd")
    cset = csub.add_parser("set", help="write the service settings for this machine")
    cset.add_argument("--url", dest="set_url", help="service origin to remember")
    cset.add_argument("--token", dest="set_token", help="bearer token to remember")
    cset.add_argument("--ca-bundle", dest="set_ca_bundle",
                      help="CA bundle path to remember")
    csub.add_parser("show", help="what each resolution rung supplies right now")
    csub.add_parser("path", help="print the config file path and exit")

    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.cmd:
        ap.print_help()
        return 2

    if args.cmd == "config":
        if not getattr(args, "config_cmd", None):
            cfg.print_help()
            return 2
        try:
            return _cmd_config(args)
        except ConfigError as exc:
            print(f"awfind: {exc}", file=sys.stderr)
            return 2

    try:
        c = _client(args)
        if args.cmd in ("q", "deep"):
            query = " ".join(args.query)
            fn = c.quick if args.cmd == "q" else c.deep
            _show(fn(query, limit=args.limit, provider=args.provider), args.json)
            return 0
        if args.cmd == "providers":
            print(json.dumps(c.providers(), indent=2))
            return 0
        if args.cmd == "stats":
            print(json.dumps(c.stats(), indent=2))
            return 0
    except (UnresolvedError, ConfigError) as exc:
        # Configuration, not transport. Same exit code as any other "you asked
        # wrongly": nothing was dialled, so calling it a service failure would
        # send the reader to look at a service that is very likely fine.
        print(f"awfind: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        # A refusal made HERE, before the round trip. Distinct exit code from a
        # transport failure so a script can tell "I asked wrongly" from "it broke".
        print(f"awfind: {exc}", file=sys.stderr)
        return 2
    except FindError as exc:
        print(f"awfind: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
