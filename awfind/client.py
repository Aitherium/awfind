"""A portable client for an AitherSearch-shaped service.

WHY A CLIENT AND NOT A LIFT
===========================
`AitherSearch.py` is 3,949 lines with 33 monorepo imports. Lifting it into a
package produces something that raises `ModuleNotFoundError` on a stranger's
machine while reading as authoritative — a broken package, not a shipped one.

`awrelay` set the precedent: ship the small standalone CLIENT, keep the engine
private and free to change. The wire contract is the product.

WHAT "SHAPED" MEANS
===================
Any service exposing these routes — read off the running service, not invented:

    POST /search          {query, mode, provider, limit, include_answer,
                           auto_learn, synthesize}
    POST /search/quick    the same body with mode forced to "quick"
    POST /search/deep     the same body with mode forced to "deep"
    POST /fetch           retrieve one URL's content
    GET  /providers       which backends are configured
    GET  /stats

THE TRAP THIS CLIENT EXISTS TO AVOID
====================================
The search model does **not** declare `extra="forbid"` — it takes pydantic's
default, `extra="ignore"`. So a field name this client gets wrong is **silently
DROPPED**: no 422, no error, a perfectly ordinary 200, and results computed from
a request that quietly lost half of what you asked for.

That is the opposite of the browse service (`awbrowse`), which forbids extras and
therefore tells you loudly. Same platform, opposite failure mode — so do not
carry an assumption from one to the other. Here, the ONLY protection is sending
exactly the declared field set, which is why `SEARCH_FIELDS` is a constant the
self-test asserts against rather than a literal sprinkled through the code.

Depends on httpx and nothing else.
"""
from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "FindClient", "FindError", "Result", "Answer",
    "SEARCH_FIELDS", "MODES", "QUERY_MAX_CHARS", "search_body",
]

DEFAULT_TIMEOUT = 60.0

#: EXACTLY the fields the service's search model declares.
SEARCH_FIELDS = (
    "query", "mode", "provider", "limit",
    "include_answer", "auto_learn", "synthesize",
)

#: The modes the service routes on. `fast` has its own endpoint rather than being
#: a mode value; `/search/quick` and `/search/deep` just force this field.
MODES = ("quick", "deep")

#: The service refuses a longer query with an explicit message: it looks like a
#: prompt, not a search query. Checked client-side too, so the round trip is not
#: spent learning something decidable here — the SERVICE stays the authority,
#: this is just a cheaper copy of the same answer.
QUERY_MAX_CHARS = 512


class FindError(RuntimeError):
    """The service refused or could not answer.

    Raised, never returned as an empty result list. A search that failed and a
    search that genuinely matched nothing are different facts, and a client that
    returns `[]` for both makes a dead backend look like an unpopular query —
    the exact silence that hides an outage.
    """


def search_body(query: str, *, mode: str = "quick", provider: Optional[str] = None,
                limit: int = 10, include_answer: bool = True,
                auto_learn: bool = True, synthesize: bool = False) -> dict:
    """The request body for POST /search. Pure, so it is testable with no service.

    Raises ValueError for the things the service will also reject, so the caller
    gets the real reason rather than an empty result set.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("query must not be empty")
    if len(q) > QUERY_MAX_CHARS:
        raise ValueError(
            f"query is {len(q)} chars (max {QUERY_MAX_CHARS}) — this looks like a "
            "prompt, not a search query; distill it before searching"
        )
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    return {
        "query": q,
        "mode": mode,
        "provider": provider,
        "limit": limit,
        "include_answer": include_answer,
        "auto_learn": auto_learn,
        "synthesize": synthesize,
    }


class Result:
    """One search hit."""

    __slots__ = ("title", "url", "snippet", "source", "score", "raw")

    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.title: str = raw.get("title", "")
        self.url: str = raw.get("url", "")
        self.snippet: str = raw.get("snippet", "")
        self.source: str = raw.get("source", "")
        self.score: float = float(raw.get("score") or 0.0)

    def __repr__(self) -> str:
        return f"<Result {self.title!r} {self.url}>"


class Answer:
    """A whole search response: the synthesized answer plus its results."""

    __slots__ = ("query", "answer", "results", "citations", "provider",
                 "mode", "cached", "took_ms", "total", "raw")

    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.query: str = raw.get("query", "")
        # None, not "" — the service returns no answer at all unless one was
        # asked for and produced, and "" would read as "it answered, emptily".
        self.answer: Optional[str] = raw.get("answer") or None
        self.results: list[Result] = [Result(r) for r in raw.get("results") or []]
        self.citations: list = raw.get("citations") or []
        self.provider: str = raw.get("provider", "")
        self.mode: str = raw.get("mode", "")
        self.cached: bool = bool(raw.get("cached"))
        self.took_ms: float = float(raw.get("search_time_ms") or 0.0)
        self.total: int = int(raw.get("total_results") or 0)

    def __len__(self) -> int:
        return len(self.results)

    def __iter__(self):
        return iter(self.results)

    def __repr__(self) -> str:
        return f"<Answer {self.query!r} {len(self.results)} results via {self.provider}>"


class FindClient:
    """Talks to an AitherSearch-shaped service."""

    def __init__(self, base_url: str, token: Optional[str] = None, *,
                 timeout: float = DEFAULT_TIMEOUT, verify: bool | str = True) -> None:
        """
        base_url  the service origin. These serve TLS in-network; plain http into
                  a TLS listener closes the socket and reads as "the service is
                  down" while it is perfectly healthy.
        token     the CALLER's bearer, never a service credential — this package
                  ships publicly, so an internal key would either fail for
                  strangers or work for everyone who reads the source.
        verify    never False against a real deployment; trust the CA instead.
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.verify = verify

    def _http(self):
        import httpx  # local import so the module imports without httpx present

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return httpx.Client(base_url=self.base_url, headers=headers,
                            timeout=self.timeout, verify=self.verify)

    def _post(self, path: str, body: dict) -> dict:
        try:
            with self._http() as c:
                r = c.post(path, json=body)
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            raise FindError(f"{path}: {exc}") from exc
        if r.status_code >= 400:
            raise FindError(f"{path}: HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def _get(self, path: str) -> Any:
        try:
            with self._http() as c:
                r = c.get(path)
        except Exception as exc:  # noqa: BLE001
            raise FindError(f"{path}: {exc}") from exc
        if r.status_code >= 400:
            raise FindError(f"{path}: HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    # ── Searching ───────────────────────────────────────────────────────────

    def search(self, query: str, **kwargs: Any) -> Answer:
        """Search. Every keyword is validated here rather than dropped there."""
        return Answer(self._post("/search", search_body(query, **kwargs)))

    def quick(self, query: str, **kwargs: Any) -> Answer:
        kwargs["mode"] = "quick"
        return Answer(self._post("/search/quick", search_body(query, **kwargs)))

    def deep(self, query: str, **kwargs: Any) -> Answer:
        kwargs["mode"] = "deep"
        return Answer(self._post("/search/deep", search_body(query, **kwargs)))

    def fetch(self, url: str, **kwargs: Any) -> dict:
        """Retrieve one URL's content through the service."""
        return self._post("/fetch", {"url": url, **kwargs})

    # ── About the service ───────────────────────────────────────────────────

    def providers(self) -> Any:
        """Which backends are configured.

        Worth calling before concluding a query is unpopular: a service with no
        provider configured returns nothing for everything, cheerfully.
        """
        return self._get("/providers")

    def stats(self) -> dict:
        return self._get("/stats")
