"""An MCP server for awfind -- ask one question, get ranked answers back.

    pip install "awfind[mcp]"

then point a client at `awfind mcp`:

    {"mcpServers": {"awfind": {"command": "awfind", "args": ["mcp"]}}}

The service origin, token and CA bundle resolve exactly as the CLI resolves them
(AWFIND_URL / AWFIND_TOKEN / SSL_CERT_FILE, then ~/.aither/awfind.json), so a
machine where `awfind q ...` works needs no env block at all.

WHAT IT SEARCHES
----------------
Whatever the AitherSearch-shaped service has configured -- `find_providers`
says which. Typically web engines (duckduckgo, brave, perplexity), model hubs
(huggingface, civitai) and `browser_context` (pages the owner's browser has
seen). The service ranks every hit and returns the score breakdown; this server
passes the rank and the source URL through rather than flattening them away.

DESIGN NOTES THAT MATTER
------------------------
**Connection is fixed at server start**, never a tool argument. Letting a model
choose which host receives the bearer token is the caller-supplied-identity
shape (security-review-patterns #2); awrelay's server makes the same call.

**Read-only by default.** The service's `auto_learn` defaults to TRUE, which
writes every query into its learning store. Here it is sent as FALSE: an agent
that searches forty times while debugging should not silently train the
service. There is no tool that writes.

**Failure is loud.** A transport or HTTP error is returned as an explicit
"search FAILED" line, never as an empty list: a dead backend and an unpopular
query are different facts.

**Imports are lazy** so `awfind mcp` is listing tools well under a second;
httpx is only imported on the first real call.

SDK VERSION
-----------
Prefers the `mcp` 2.x `MCPServer` and falls back to the 1.x `FastMCP`, which
exposes the same `tool()` / `list_tools()` / `call_tool()` / `run_stdio_async()`
surface. awgraph and awrelay pin 2.x only; measured 2026-09-26 this host had
mcp 1.29.1 installed, on which those two servers cannot start. Accepting both
keeps the tested version and the running version the same one.
"""

from __future__ import annotations

import json
from typing import Any, Optional

_INSTALL_HINT = (
    "The MCP server needs the `mcp` package: pip install \"awfind[mcp]\". "
    "Raised rather than degraded, because an MCP server that starts and serves "
    "no tools looks to the client exactly like a server with nothing to offer."
)

#: Hard cap on results per call. The service allows more, but an agent's context
#: is the scarce resource and a ranked list is read top-down.
MAX_LIMIT = 25

FIND_DESCRIPTION = (
    "Ask one search question and get RANKED answers back, each with its source "
    "URL, the provider that found it, and the service's relevance score "
    "(highest first). Searches the AitherSearch service this machine is "
    "configured for (awfind config) -- web engines, model hubs (huggingface, "
    "civitai) and browser_context (pages the owner's browser has already seen). "
    "Call find_providers first if results are empty: a service with no provider "
    "available returns nothing for everything.\n"
    "PREFER THIS over the gateway's web_search when: the AitherOS MCP gateway is "
    "down or absent (this needs only the search service); you want to pick the "
    "provider (e.g. sources=['huggingface'] for models, ['browser_context'] for "
    "what the owner already read); you need the score to decide what to trust; "
    "or you want nothing written to memory. "
    "PREFER the gateway's web_search when the gateway is up and you want the "
    "results persisted into agent memory for later recall. "
    "Neither searches this repository's code -- use rg or awgraph for that. "
    "Queries over 512 characters are refused: distill a prompt into a query."
)


def _build_mcp_server(name: str, instructions: str):
    """The 2.x MCPServer when present, else the 1.x FastMCP. Same tool surface."""
    try:
        from mcp.server import MCPServer  # type: ignore[attr-defined]
        return MCPServer(name=name, instructions=instructions)
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise ImportError(_INSTALL_HINT) from exc
    return FastMCP(name=name, instructions=instructions)


def _client():
    """A FindClient from the CLI's resolution order. Raises with the whole trail."""
    from awfind.client import FindClient
    from awfind.config import (
        resolve_browser_url,
        resolve_ca_bundle,
        resolve_token,
        resolve_url,
    )

    url, _ = resolve_url(None)
    token, _ = resolve_token(None)
    verify, _ = resolve_ca_bundle(None)
    browser, _ = resolve_browser_url(None)
    return FindClient(url, token, verify=verify, browser_url=browser)


def rank(answers: list, limit: int) -> list[dict]:
    """Merge one or more Answers into one ranked list, deduplicated by URL.

    Pure (takes parsed Answers), so the ordering contract is testable with no
    service. When the same URL comes back from two providers the higher score
    wins and both providers are named -- corroboration is information.
    """
    best: dict[str, dict] = {}
    order: list[str] = []
    for ans in answers:
        for r in ans.results:
            key = r.url or f"{r.source}:{r.title}"
            row = {
                "title": r.title,
                "url": r.url,
                "snippet": (r.snippet or "")[:400],
                "source": r.source or ans.provider,
                "score": round(r.score, 4),
            }
            if key not in best:
                best[key] = row
                order.append(key)
                continue
            prev = best[key]
            sources = sorted({*prev["source"].split("+"), row["source"]} - {""})
            if row["score"] > prev["score"]:
                best[key] = row
            best[key]["source"] = "+".join(sources)
    # Stable sort on score: ties keep the service's own order.
    rows = sorted((best[k] for k in order), key=lambda d: -d["score"])
    for i, row in enumerate(rows[:limit], 1):
        row["rank"] = i
    return rows[:limit]


def run_find(query: str, limit: int = 10, sources: Optional[list[str]] = None,
             mode: str = "quick", client: Any = None) -> str:
    """The body of the `find` tool, at module level so it is testable without mcp."""
    from awfind.client import MODES, FindError, search_body
    from awfind.config import ConfigError, UnresolvedError

    limit = max(1, min(int(limit or 10), MAX_LIMIT))
    if mode not in MODES:
        return f"Refused: mode must be one of {MODES}, got {mode!r}."
    wanted = [s.strip() for s in (sources or []) if s and s.strip()]
    try:
        # Validate before dialling: the service would refuse the same things,
        # a round trip later.
        search_body(query, mode=mode, limit=limit)
    except ValueError as exc:
        return f"Refused: {exc}"
    try:
        c = client or _client()
    except (UnresolvedError, ConfigError) as exc:
        return f"awfind is not configured on this machine: {exc}"

    call = c.deep if mode == "deep" else c.quick
    answers, failures = [], []
    for provider in (wanted or [None]):
        try:
            answers.append(call(query, limit=limit, provider=provider,
                                auto_learn=False, include_answer=True))
        except FindError as exc:
            failures.append(f"{provider or 'default'}: {exc}")

    if not answers:
        return "search FAILED (no provider answered): " + "; ".join(failures)

    rows = rank(answers, limit)
    synthesized = next((a.answer for a in answers if a.answer), None)
    payload: dict[str, Any] = {
        "query": query,
        "mode": mode,
        "providers": sorted({a.provider for a in answers if a.provider}),
        "answer": synthesized,
        "results": rows,
    }
    if failures:
        payload["failed_sources"] = failures
    if not rows:
        payload["note"] = ("No results. Call find_providers before concluding the "
                           "query is unpopular -- an unavailable provider returns "
                           "nothing, cheerfully.")
    return json.dumps(payload, indent=1)


def run_providers(client: Any = None) -> str:
    """The body of the `find_providers` tool."""
    from awfind.client import FindError
    from awfind.config import ConfigError, UnresolvedError

    try:
        c = client or _client()
        return json.dumps(c.providers())
    except (UnresolvedError, ConfigError) as exc:
        return f"awfind is not configured on this machine: {exc}"
    except FindError as exc:
        return f"providers FAILED: {exc}"


def _guarded(fn, client: Any = None) -> str:
    """Run one tool body; configuration and service failures come back as text."""
    from awfind.client import FindError
    from awfind.config import ConfigError, UnresolvedError

    try:
        return fn(client or _client())
    except (UnresolvedError, ConfigError) as exc:
        return f"awfind is not configured on this machine: {exc}"
    except ValueError as exc:
        return f"Refused: {exc}"
    except FindError as exc:
        return f"FAILED: {exc}"


def run_contents(url: str, max_chars: int = 6000, client: Any = None) -> str:
    """The body of the `find_contents` tool."""
    def go(c):
        pg = c.contents(url)
        text = pg.content if max_chars <= 0 else pg.content[:max_chars]
        return json.dumps({"url": pg.url, "title": pg.title, "words": pg.words,
                           "method": pg.raw.get("extraction_method", ""),
                           "truncated": len(text) < len(pg.content), "content": text},
                          indent=1)
    return _guarded(go, client)


def run_answer(question: str, sources: int = 3, client: Any = None) -> str:
    """The body of the `find_answer` tool."""
    def go(c):
        a = c.answer(question, max_sources=max(1, min(int(sources or 3), 8)))
        return json.dumps({"answer": a.answer, "citations": a.citations}, indent=1)
    return _guarded(go, client)


def run_similar(url: str, limit: int = 10, provider: Optional[str] = None,
                client: Any = None) -> str:
    """The body of the `find_similar` tool."""
    def go(c):
        ans = c.similar(url, limit=max(1, min(int(limit or 10), MAX_LIMIT)),
                        provider=provider)
        return json.dumps({"similar_to": url, "query": ans.raw.get("similar_query"),
                           "results": rank([ans], MAX_LIMIT)}, indent=1)
    return _guarded(go, client)


def build_server():
    """Construct the MCP server. Raises ImportError if `mcp` is absent."""
    server = _build_mcp_server(
        "awfind",
        "Ranked search over the configured AitherSearch service: web engines, "
        "model hubs and the owner's browser context. find() answers a question "
        "with ranked, source-attributed results; find_providers() says which "
        "backends are live. Read-only: nothing is written or learned.",
    )

    @server.tool(name="find", description=FIND_DESCRIPTION)
    async def find(query: str, limit: int = 10, sources: Optional[list[str]] = None,
                   mode: str = "quick") -> str:
        """Search and rank.

        Args:
            query: The question, as a search query (max 512 chars).
            limit: Maximum ranked results, 1-25.
            sources: Provider names to search (see find_providers), e.g.
                ["duckduckgo"], ["huggingface"], ["browser_context"]. Omit to let
                the service choose. Several are searched and merged by score.
            mode: "quick" (default) or "deep" (slower, broader).
        """
        import asyncio

        return await asyncio.to_thread(run_find, query, limit, sources, mode)

    @server.tool(
        name="find_providers",
        description=("Which search backends the service has and whether each is "
                     "available right now. Call before trusting an empty find()."),
    )
    async def find_providers() -> str:
        import asyncio

        return await asyncio.to_thread(run_providers)

    @server.tool(
        name="find_contents",
        description=("Clean text of one URL (title, word count, extraction method). "
                     "Falls back from extraction to fetch to a real browser render "
                     "when a site refuses bots. Use after find() on the 1-3 best hits."),
    )
    async def find_contents(url: str, max_chars: int = 6000) -> str:
        import asyncio

        return await asyncio.to_thread(run_contents, url, max_chars)

    @server.tool(
        name="find_answer",
        description=("One synthesized answer to a question, with the URLs it cites. "
                     "Quote only these citations. Max 512 chars of question."),
    )
    async def find_answer(question: str, sources: int = 3) -> str:
        import asyncio

        return await asyncio.to_thread(run_answer, question, sources)

    @server.tool(
        name="find_similar",
        description=("Pages like a URL: reads it, searches by what it says it is, "
                     "drops the page itself. provider='exa' ranks by meaning."),
    )
    async def find_similar(url: str, limit: int = 10, provider: Optional[str] = None) -> str:
        import asyncio

        return await asyncio.to_thread(run_similar, url, limit, provider)

    return server


def main(argv: list[str] | None = None) -> int:
    """Serve over stdio. Blocks until the client disconnects."""
    import asyncio
    import sys

    try:
        server = build_server()
    except ImportError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        asyncio.run(server.run_stdio_async())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
