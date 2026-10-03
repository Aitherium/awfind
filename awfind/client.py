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
    POST /extract         {url, timeout} -> clean text, title, headings, links
    POST /web/research    {question, max_sources, synthesize} -> one cited answer
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

import re
from typing import Any, Optional
from urllib.parse import urlsplit

__all__ = [
    "FindClient", "FindError", "Result", "Answer", "Page", "Cited",
    "SEARCH_FIELDS", "MODES", "QUERY_MAX_CHARS", "search_body",
    "similar_query", "parse_cited", "page_from_fetch", "page_from_render",
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


def _brief(body: str, limit: int = 300) -> str:
    """An error body short enough to read: JSON detail if any, HTML reduced to text."""
    text = body or ""
    try:
        import json

        detail = json.loads(text).get("detail")
        if detail:
            text = detail if isinstance(detail, str) else json.dumps(detail)
    except (ValueError, AttributeError):
        pass
    if "<" in text and ">" in text:
        text = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


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


class Page:
    """One URL's extracted contents (POST /extract)."""

    __slots__ = ("url", "title", "description", "content", "headings", "links",
                 "published", "author", "words", "raw")

    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.url: str = raw.get("url", "")
        self.title: str = raw.get("title", "") or ""
        self.description: str = raw.get("description", "") or ""
        self.content: str = raw.get("content", "") or ""
        self.headings: list = raw.get("headings") or []
        self.links: list = raw.get("links") or []
        self.published: str = raw.get("published_date", "") or ""
        self.author: str = raw.get("author", "") or ""
        self.words: int = int(raw.get("word_count") or 0)

    def __repr__(self) -> str:
        return f"<Page {self.title!r} {self.url} {self.words}w>"


#: The research route reports failure INSIDE a 200, as a bracketed tag. Matched
#: here so a failed research call raises instead of printing as an answer.
_RESEARCH_ERROR = re.compile(r"^\s*\[WEB_RESEARCH_ERROR:\s*(.*?)\]?\s*$", re.S)
_RESEARCH_HEADER = re.compile(r"^\s*\[WEB_RESEARCH:[^\]]*\][^\n]*\n?")
_URL = re.compile(r"https?://[^\s\]>\"']+")


class Cited:
    """An answer with the URLs it cites, in first-seen order."""

    __slots__ = ("question", "answer", "citations", "raw")

    def __init__(self, question: str, answer: str, citations: list, raw: Any) -> None:
        self.question = question
        self.answer = answer
        self.citations = citations
        self.raw = raw

    def __repr__(self) -> str:
        return f"<Cited {self.question!r} {len(self.citations)} citations>"


def parse_cited(question: str, text: str) -> "Cited":
    """Turn the research route's text into a Cited. Pure.

    Raises FindError when the text is the route's in-band error tag, so an
    outage never prints as if it were the answer.
    """
    text = text or ""
    m = _RESEARCH_ERROR.match(text)
    if m:
        raise FindError(f"/web/research: {m.group(1).strip() or 'failed'}")
    body = _RESEARCH_HEADER.sub("", text, count=1).strip()
    if not body:
        raise FindError("/web/research: empty answer")
    seen: list[str] = []
    for u in _URL.findall(body):
        u = u.rstrip(".,;:")
        # Keep a URL's own parentheses (wiki titles) but drop the one that
        # closes the surrounding "(source: ...)".
        while u.endswith(")") and u.count(")") > u.count("("):
            u = u[:-1].rstrip(".,;:")
        if u not in seen:
            seen.append(u)
    return Cited(question, body, seen, text)


def page_from_fetch(url: str, raw: dict) -> "Page":
    """A Page from the /fetch route's markdown. Pure.

    The title is the first markdown heading, or failing that the first line;
    the description is the first blockquote line, which is where the fetcher
    puts a page's meta description.
    """
    content = (raw or {}).get("content", "") or ""
    title = description = ""
    for line in content.splitlines():
        t = line.strip()
        if not t:
            continue
        if not title and t.startswith("#"):
            title = t.lstrip("#").strip()
        elif not description and t.startswith(">"):
            description = t.lstrip(">").strip()
        if title and description:
            break
    if not title:
        title = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")[:200]
    return Page({
        "url": raw.get("url") or url,
        "title": title,
        "description": description,
        "content": content,
        "word_count": len(content.split()),
        "extraction_method": "fetch",
        "truncated": int(raw.get("length") or 0) > len(content),
    })


def page_from_render(url: str, raw: dict) -> "Page":
    """A Page from a rendering service's text. Pure. Renders carry no title."""
    content = (raw or {}).get("content") or (raw or {}).get("text") or ""
    if not content.strip():
        raise FindError(f"render: {url} rendered to empty text")
    return Page({
        "url": raw.get("url") or url,
        "content": content,
        "word_count": len(content.split()),
        "extraction_method": f"render:{raw.get('engine', '')}",
    })


def similar_query(page: "Page") -> str:
    """The search query that stands for a page when finding pages like it. Pure.

    Title, then description, then the first headings, then the opening of the
    body: the parts a page uses to say what it is about. Trimmed to the
    service's query bound on a word boundary.
    """
    parts: list[str] = []
    for chunk in (page.title, page.description,
                  " ".join(str(h.get("text", h) if isinstance(h, dict) else h)
                           for h in page.headings[:4])):
        chunk = " ".join(str(chunk or "").split())
        if chunk and chunk.lower() not in " ".join(parts).lower():
            parts.append(chunk)
    q = " ".join(parts)
    if len(q) < 40:
        # No title to go on (a render): use sentence-length lines, skipping the
        # one- and two-word menu items a rendered page starts with.
        lines = [ln.strip() for ln in page.content.splitlines()]
        prose = [ln for ln in lines if len(ln.split()) >= 6] or lines
        q = (q + " " + " ".join(" ".join(prose).split()[:60])).strip()
    if len(q) > QUERY_MAX_CHARS:
        q = q[:QUERY_MAX_CHARS].rsplit(" ", 1)[0]
    if not q:
        raise ValueError(f"{page.url}: page has no title or text to search by")
    return q


def _same_page(a: str, b: str) -> bool:
    """Two URLs naming one page, ignoring scheme, www., query and a trailing slash."""
    def norm(u: str) -> str:
        sp = urlsplit(u)
        host = sp.netloc.lower().removeprefix("www.")
        return f"{host}{sp.path.rstrip('/')}"
    return norm(a) == norm(b)


class FindClient:
    """Talks to an AitherSearch-shaped service."""

    def __init__(self, base_url: str, token: Optional[str] = None, *,
                 timeout: float = DEFAULT_TIMEOUT, verify: bool | str = True,
                 browser_url: Optional[str] = None) -> None:
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
        # A rendering service (AitherBrowser-shaped POST /browse) for sites that
        # refuse plain fetchers. Never sent the search bearer: it is another host.
        self.browser_url = browser_url.rstrip("/") if browser_url else None

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
            raise FindError(f"{path}: HTTP {r.status_code}: {_brief(r.text)}")
        return r.json()

    def _get(self, path: str) -> Any:
        try:
            with self._http() as c:
                r = c.get(path)
        except Exception as exc:  # noqa: BLE001
            raise FindError(f"{path}: {exc}") from exc
        if r.status_code >= 400:
            raise FindError(f"{path}: HTTP {r.status_code}: {_brief(r.text)}")
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

    def contents(self, url: str, *, timeout: float = 30.0) -> Page:
        """Clean contents of one URL: text, title, headings, links.

        POST /extract first (structured: title, headings, links). Many sites
        refuse its bot user-agent (measured: GitHub 422, Cloudflare 403), so on
        a refusal this falls back to POST /fetch, which content-negotiates
        markdown, and says so in ``raw["extraction_method"]``. Both failing
        raises with both reasons.
        """
        if not (url or "").startswith(("http://", "https://")):
            raise ValueError(f"url must start with http:// or https://, got {url!r}")
        errors: list[str] = []
        try:
            raw = self._post("/extract", {"url": url, "timeout": timeout})
            # The extractor reports a refused fetch INSIDE a 200: success false,
            # empty content. Accepted, that is a blank page standing in for a 403.
            if raw.get("success") is False or not (raw.get("content") or "").strip():
                raise FindError(f"/extract: {_brief(str(raw.get('error') or 'empty content'))}")
            return Page(raw)
        except FindError as exc:
            errors.append(str(exc))
        try:
            return page_from_fetch(url, self._post("/fetch", {"url": url}))
        except FindError as exc:
            errors.append(str(exc))
        if self.browser_url:
            try:
                return page_from_render(url, self._render(url))
            except FindError as exc:
                errors.append(str(exc))
        else:
            errors.append("no browser_url configured for a render fallback")
        raise FindError("; then ".join(errors))

    def _render(self, url: str) -> dict:
        """POST /browse on the rendering service: exactly its four declared fields."""
        import httpx

        body = {"url": url, "wait_time": 1500, "extract_text": True, "screenshot": False}
        try:
            r = httpx.post(f"{self.browser_url}/browse", json=body,
                           timeout=max(self.timeout, 90.0), verify=self.verify)
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            raise FindError(f"render: {exc}") from exc
        if r.status_code >= 400:
            raise FindError(f"render: HTTP {r.status_code}: {_brief(r.text)}")
        raw = r.json()
        if raw.get("status") not in ("success", "ok"):
            raise FindError(f"render: the browser reported status={raw.get('status')!r}")
        return raw

    def answer(self, question: str, *, max_sources: int = 3) -> Cited:
        """One synthesized answer with the URLs it cites (POST /web/research)."""
        q = (question or "").strip()
        if not q:
            raise ValueError("question must not be empty")
        if len(q) > QUERY_MAX_CHARS:
            raise ValueError(f"question is {len(q)} chars (max {QUERY_MAX_CHARS})")
        raw = self._post("/web/research",
                         {"question": q, "max_sources": max_sources, "synthesize": True})
        return parse_cited(q, (raw or {}).get("result", ""))

    def similar(self, url: str, *, limit: int = 10, provider: Optional[str] = None,
                mode: str = "quick") -> Answer:
        """Pages like this URL: extract it, search by what it says it is, drop itself.

        The query is built from the page (see `similar_query`) rather than sent
        as the URL, because a URL is not a query any web engine ranks by meaning.
        Pass provider="exa" for neural ranking when the service has it.
        """
        page = self.contents(url)
        q = similar_query(page)
        call = self.deep if mode == "deep" else self.quick
        ans = call(q, limit=limit + 2, provider=provider, auto_learn=False)
        ans.results = [r for r in ans.results if not _same_page(r.url, url)][:limit]
        ans.raw = {**ans.raw, "similar_to": url, "similar_query": q,
                   "results": [r.raw for r in ans.results]}
        return ans

    # ── About the service ───────────────────────────────────────────────────

    def providers(self) -> Any:
        """Which backends are configured.

        Worth calling before concluding a query is unpopular: a service with no
        provider configured returns nothing for everything, cheerfully.
        """
        return self._get("/providers")

    def stats(self) -> dict:
        return self._get("/stats")
