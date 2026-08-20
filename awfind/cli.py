"""awfind CLI.

    awfind q "what changed in podman 5.4"
    awfind deep "cross-model KV cache transfer" --limit 20
    awfind providers
    awfind --self-test

The service origin comes from --url or AWFIND_URL; the token from --token or
AWFIND_TOKEN. Neither is guessed: a search client that silently falls back to
some default endpoint sends your queries somewhere you did not choose.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

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

# ── self-test ──────────────────────────────────────────────────────────────
# Everything asserted here is PURE. A self-test that needs a live service is a
# self-test that gets skipped, and a skipped check is indistinguishable from a
# passing one.


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
        try:
            search_body(bad)
        except ValueError:
            pass
        else:
            failures.append(f"search_body accepted a query that is {why}")
    if len(search_body("x" * QUERY_MAX_CHARS)["query"]) != QUERY_MAX_CHARS:
        failures.append("a query exactly at the limit was refused; the bound is inclusive")

    # 4. An unknown mode is refused rather than sent. Sent, it would be ignored
    #    and silently answered as the service's default.
    try:
        search_body("q", mode="sideways")
    except ValueError:
        pass
    else:
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

    for f in failures:
        print(f"  FAIL  {f}")
    if failures:
        print(f"SELF-TEST: {len(failures)} failure(s)")
        return 1
    print("  PASS  search body is exactly the declared field set, with the service's defaults")
    print("  PASS  empty/over-long queries and unknown modes are refused with a reason")
    print("  PASS  absent answer is None, missing score is 0.0, results iterate in order")
    print("SELF-TEST: awfind ok")
    return 0


# ── commands ───────────────────────────────────────────────────────────────


def _client(args: argparse.Namespace) -> FindClient:
    url = args.url or os.environ.get("AWFIND_URL")
    if not url:
        print("no service URL: pass --url or set AWFIND_URL", file=sys.stderr)
        raise SystemExit(2)
    return FindClient(url, args.token or os.environ.get("AWFIND_TOKEN"))


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
    ap = argparse.ArgumentParser(prog="awfind", description=__doc__)
    ap.add_argument("--self-test", action="store_true",
                    help="prove this client still holds its contract, offline")
    ap.add_argument("--url", help="service origin (or AWFIND_URL)")
    ap.add_argument("--token", help="bearer token (or AWFIND_TOKEN)")
    ap.add_argument("--json", action="store_true", help="print the raw response")
    sub = ap.add_subparsers(dest="cmd")

    for name, help_text in (("q", "quick search"), ("deep", "deep search")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("query", nargs="+")
        p.add_argument("--limit", type=int, default=10)
        p.add_argument("--provider")

    sub.add_parser("providers", help="which backends the service has configured")
    sub.add_parser("stats", help="service counters")

    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.cmd:
        ap.print_help()
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
