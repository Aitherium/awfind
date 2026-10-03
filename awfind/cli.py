"""awfind CLI.

    awfind config set --url https://search.example.com   # once, per machine
    awfind q "what changed in podman 5.4"
    awfind deep "cross-model KV cache transfer" --limit 20
    awfind contents https://example.com                   # clean page text
    awfind similar https://example.com --provider exa     # pages like this one
    awfind answer "latest stable podman"                  # answer + citations
    awfind --account work q "..."                         # a named account
    awfind agent-readme                                   # the manual, for an LLM
    awfind providers
    awfind doctor
    awfind mcp                                            # stdio MCP server
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
    Cited,
    FindClient,
    FindError,
    Page,
    Result,
    page_from_fetch,
    page_from_render,
    parse_cited,
    search_body,
    similar_query,
)
from awfind.config import (
    ConfigError,
    UnresolvedError,
    config_path,
    list_accounts,
    remove_account,
    resolution_report,
    resolve_browser_url,
    resolve_ca_bundle,
    resolve_token,
    resolve_url,
    set_default_account,
    write_account,
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


def _self_test_accounts() -> list[str]:
    """Named accounts: overlay, explicit pick, and a loud unknown name. Sandboxed."""
    import tempfile

    failures: list[str] = []
    saved = {k: os.environ.get(k) for k in ("AWFIND_URL", "AWFIND_TOKEN", "AWFIND_ACCOUNT",
                                            "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = str(Path(tmp) / "awfind.json")
            write_config("https://home.invalid", "home-token", ca_bundle="/ca.pem", path=cfg)
            write_account("work", "https://work.invalid", "work-token", path=cfg)
            if resolve_url(None, path=cfg)[0] != "https://home.invalid":
                failures.append("no account picked should mean the top level")
            if resolve_url(None, path=cfg, account="work")[0] != "https://work.invalid":
                failures.append("--account work did not pick the work url")
            if resolve_token(None, path=cfg, account="work")[0] != "work-token":
                failures.append("--account work did not pick the work token")
            if resolve_ca_bundle(None, path=cfg, account="work")[0] != "/ca.pem":
                failures.append("an account did not inherit the top-level ca_bundle")
            os.environ["AWFIND_ACCOUNT"] = "work"
            if resolve_url(None, path=cfg)[0] != "https://work.invalid":
                failures.append("AWFIND_ACCOUNT was ignored")
            os.environ.pop("AWFIND_ACCOUNT")
            try:
                resolve_token(None, path=cfg, account="nope")
                failures.append("an unknown account fell back instead of failing")
            except ConfigError as exc:
                if "work" not in str(exc):
                    failures.append("the unknown-account error does not list known accounts")
            set_default_account("work", path=cfg)
            if resolve_url(None, path=cfg)[0] != "https://work.invalid":
                failures.append("default_account was ignored")
            rows = list_accounts(path=cfg)
            if rows != [("work", "https://work.invalid", True, True)]:
                failures.append(f"list_accounts wrong: {rows}")
            if not remove_account("work", path=cfg):
                failures.append("remove_account did not find the account")
            if resolve_url(None, path=cfg)[0] != "https://home.invalid":
                failures.append("removing the default account did not clear default_account")
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

    # 11. The research route reports failure inside a 200. It must RAISE, and a
    #     real answer must keep its citations, deduplicated, in order.
    try:
        parse_cited("q", "[WEB_RESEARCH_ERROR: search failed: boom]")
        failures.append("an in-band research error parsed as an answer")
    except FindError:
        pass
    cited = parse_cited("q", "[WEB_RESEARCH: q] synthesized from 2 sources:\n"
                             "It is 6.1 (source: https://a.example/x). "
                             "See https://b.example/y, and https://a.example/x. "
                             "(source: https://w.example/A_(b))")
    if cited.citations != ["https://a.example/x", "https://b.example/y",
                           "https://w.example/A_(b)"]:
        failures.append(f"citations parsed wrong: {cited.citations}")
    if cited.answer.startswith("[WEB_RESEARCH"):
        failures.append("the research header leaked into the answer")

    # 12. similar() searches by what the page says it is, within the bound.
    q = similar_query(Page({"url": "https://e.x", "title": "Exa CLI",
                            "description": "neural search " * 80}))
    if not q.startswith("Exa CLI") or len(q) > QUERY_MAX_CHARS:
        failures.append(f"similar_query wrong or over the bound: {len(q)} chars")
    try:
        similar_query(Page({"url": "https://e.x"}))
        failures.append("a page with nothing to search by produced a query")
    except ValueError:
        pass

    # 13. The /fetch fallback still yields a title and description to search by.
    pg = page_from_fetch("https://e.x", {"content": "# Exa CLI\n\n> Neural search.\n\nbody"})
    if (pg.title, pg.description, pg.raw.get("extraction_method")) != (
            "Exa CLI", "Neural search.", "fetch"):
        failures.append(f"page_from_fetch parsed wrong: {pg.title!r} {pg.description!r}")

    # 14. A render carries text only; an empty render raises rather than
    #     passing as a blank page.
    if page_from_render("https://e.x", {"content": "hi there", "engine": "pw"}).words != 2:
        failures.append("page_from_render did not count words")
    try:
        page_from_render("https://e.x", {"content": "  "})
        failures.append("an empty render passed as a page")
    except FindError:
        pass

    failures.extend(_self_test_resolution())
    failures.extend(_self_test_accounts())

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
    print("  PASS  research errors raise; citations parse in order; similar() stays in bound")
    print("  PASS  named accounts overlay the top level; an unknown name fails loudly")
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
    acct = getattr(args, "account", None)
    url, _ = resolve_url(args.url, path=cfg_path, account=acct)
    token, _ = resolve_token(args.token, path=cfg_path, account=acct)
    verify, _ = resolve_ca_bundle(getattr(args, "ca_bundle", None), path=cfg_path,
                                  account=acct)
    browser, _ = resolve_browser_url(getattr(args, "browser_url", None), path=cfg_path,
                                     account=acct)
    return FindClient(url, token, verify=verify, browser_url=browser)


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
        for line in resolution_report(args.url, args.token, path=getattr(args, "config", None),
                                      account=getattr(args, "account", None)):
            print(line)
        return 0
    if args.config_cmd == "set":
        if not any((args.set_url, args.set_token, args.set_ca_bundle, args.set_browser_url)):
            print("awfind config set: nothing to set — pass --url, --token, --ca-bundle "
                  "or --browser-url", file=sys.stderr)
            return 2
        where = write_config(args.set_url, args.set_token,
                             ca_bundle=args.set_ca_bundle,
                             path=getattr(args, "config", None),
                             browser_url=args.set_browser_url)
        print(f"wrote {where}")
        return 0
    return 2


def _cmd_accounts(args: argparse.Namespace) -> int:
    """Named accounts: one service + bearer per name, picked with --account."""
    cfg_path = getattr(args, "config", None)
    if args.accounts_cmd == "list":
        rows = list_accounts(path=cfg_path)
        if args.json:
            print(json.dumps([{"name": n, "url": u, "token": t, "default": d}
                              for n, u, t, d in rows], indent=2))
            return 0
        if not rows:
            print("no named accounts — add one: awfind accounts add <name> --url https://...")
        for name, url, has_tok, default in rows:
            print(f"{'*' if default else ' '} {name:<14} {url}  "
                  f"{'token set' if has_tok else 'no token'}")
        return 0
    if args.accounts_cmd == "add":
        where = write_account(args.name, args.set_url, args.set_token,
                              ca_bundle=args.set_ca_bundle, path=cfg_path)
        print(f"account {args.name!r} written to {where}")
        return 0
    if args.accounts_cmd == "rm":
        if not remove_account(args.name, path=cfg_path):
            print(f"awfind: no account named {args.name!r}", file=sys.stderr)
            return 2
        print(f"account {args.name!r} removed")
        return 0
    if args.accounts_cmd == "default":
        name = None if args.name in ("-", "none") else args.name
        set_default_account(name, path=cfg_path)
        print(f"default account: {name or '(top level)'}")
        return 0
    return 2


def _show_pages(pages: list, as_json: bool, max_chars: int) -> None:
    if as_json:
        print(json.dumps([pg.raw for pg in pages], indent=2))
        return
    for pg in pages:
        print(f"# {pg.title or '(untitled)'}\n{pg.url}  ({pg.words} words)")
        if pg.published:
            print(f"published {pg.published}")
        text = pg.content if max_chars <= 0 else pg.content[:max_chars]
        print()
        print(text)
        if max_chars > 0 and len(pg.content) > max_chars:
            print(f"\n[... {len(pg.content) - max_chars} more chars; --max-chars 0 for all]")
        print()


def _show_cited(c: Cited, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"question": c.question, "answer": c.answer,
                          "citations": c.citations}, indent=2))
        return
    print(c.answer)
    if c.citations:
        print()
        for i, u in enumerate(c.citations, 1):
            print(f"[{i}] {u}")


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
    ap.add_argument("--browser-url", dest="browser_url",
                    help="rendering fallback for contents (or AWFIND_BROWSER_URL)")
    ap.add_argument("--account",
                    help="named account from the config file (or AWFIND_ACCOUNT)")
    sub = ap.add_subparsers(dest="cmd")

    for name, help_text in (("q", "quick search"), ("deep", "deep search")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("query", nargs="+")
        p.add_argument("--limit", type=int, default=10)
        p.add_argument("--provider")

    pc = sub.add_parser("contents", help="clean contents of one or more URLs")
    pc.add_argument("urls", nargs="+")
    pc.add_argument("--max-chars", type=int, default=4000,
                    help="truncate each page's text (0 = no limit)")
    ps = sub.add_parser("similar", help="pages like this URL")
    ps.add_argument("target", metavar="url")
    ps.add_argument("--limit", type=int, default=10)
    ps.add_argument("--provider", help="e.g. exa for neural ranking")
    ps.add_argument("--deep", action="store_true", help="deep search instead of quick")
    pa = sub.add_parser("answer", help="one answer with the URLs it cites")
    pa.add_argument("question", nargs="+")
    pa.add_argument("--sources", type=int, default=3, help="pages to read (default 3)")
    sub.add_parser("agent-readme", help="print the operating manual for an LLM")

    acc = sub.add_parser("accounts", help="named service accounts (pick with --account)")
    asub = acc.add_subparsers(dest="accounts_cmd")
    asub.add_parser("list", help="every account; tokens are never printed")
    aadd = asub.add_parser("add", help="create or update a named account")
    aadd.add_argument("name")
    aadd.add_argument("--url", dest="set_url")
    aadd.add_argument("--token", dest="set_token")
    aadd.add_argument("--ca-bundle", dest="set_ca_bundle")
    arm = asub.add_parser("rm", help="delete a named account")
    arm.add_argument("name")
    adef = asub.add_parser("default", help="make an account the default ('-' clears it)")
    adef.add_argument("name")

    sub.add_parser("providers", help="which backends the service has configured")
    sub.add_parser("stats", help="service counters")
    sub.add_parser("mcp", help="serve find() over MCP stdio for a coding agent")

    cfg = sub.add_parser("config", help="where this machine looks for the service")
    csub = cfg.add_subparsers(dest="config_cmd")
    cset = csub.add_parser("set", help="write the service settings for this machine")
    cset.add_argument("--url", dest="set_url", help="service origin to remember")
    cset.add_argument("--token", dest="set_token", help="bearer token to remember")
    cset.add_argument("--ca-bundle", dest="set_ca_bundle",
                      help="CA bundle path to remember")
    cset.add_argument("--browser-url", dest="set_browser_url",
                      help="rendering service for sites that refuse plain fetchers")
    csub.add_parser("show", help="what each resolution rung supplies right now")
    csub.add_parser("path", help="print the config file path and exit")

    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.cmd:
        ap.print_help()
        return 2

    if args.cmd == "mcp":
        if args.account:
            # The MCP server resolves its connection once, from the environment.
            os.environ["AWFIND_ACCOUNT"] = args.account
        from awfind.mcp_server import main as mcp_main
        return mcp_main()

    if args.cmd == "agent-readme":
        from awfind.agent_readme import AGENT_README
        sys.stdout.write(AGENT_README)
        return 0

    if args.cmd == "accounts":
        if not getattr(args, "accounts_cmd", None):
            acc.print_help()
            return 2
        try:
            return _cmd_accounts(args)
        except ConfigError as exc:
            print(f"awfind: {exc}", file=sys.stderr)
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
        if args.cmd == "contents":
            _show_pages([c.contents(u) for u in args.urls], args.json, args.max_chars)
            return 0
        if args.cmd == "similar":
            _show(c.similar(args.target, limit=args.limit, provider=args.provider,
                            mode="deep" if args.deep else "quick"), args.json)
            return 0
        if args.cmd == "answer":
            _show_cited(c.answer(" ".join(args.question), max_sources=args.sources),
                        args.json)
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
