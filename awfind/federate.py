"""Federated search: one query across web, code, memory and git history.

WHY THIS LIVES IN THE CLIENT, NOT IN THE SERVICE
================================================
awfind is a portable client for a NETWORK service (AitherSearch-shaped). The
other three sources are LOCAL bricks, and each keeps its state on the machine
that runs it: awgraph's chunk cache under the user's local app data, awm's
SQLite memory file, awgit's git objects. An AitherSearch server in a container
cannot see any of that, so making them server-side providers would mean
shipping the repository and the owner's memory database to the service. They
are federated HERE, where the data already is.

WHY SUBPROCESSES RATHER THAN IMPORTS
====================================
A brick is a separately installed, separately versioned package. Importing
``awgraph.graph`` would pin this client to one brick version, drag awgraph's
import cost into every ``awfind q`` (and every MCP server start), and make a
brick upgrade a breaking change for awfind. The published contract of each
brick is its CLI with ``--json``, so that is what is called. A missing brick is
reported as ``unavailable`` with the install line; it never raises past
:func:`federate` and never kills the web search.

THE MEASURED TRAP IN awgit
==========================
awgit's read verbs (log, grep, read) are git passthrough. On Windows,
``awgit git -- log ...`` was measured to exit 0 with EMPTY stdout: the launcher
inherits a pipe into git and git writes nothing to it. An empty string there
reads as "no history", which is exactly the silent-fallback shape this package
refuses. So the history source probes awgit once per (binary, root) and uses it
only when the probe's stdout actually round-trips; otherwise it calls git
directly and SAYS SO in the source report.

WHAT A RESULT IS
================
Every row is ``title``, an exact ``passage``, a ``locator`` precise enough to
open the thing (``path:line`` for code, ``scope:key`` for memory,
``sha:path:line`` for history, the URL for web), the ``kind``, and a ``score``
with the meaning of that number named in ``score_kind``. Scores are comparable
WITHIN a kind; :func:`merge` normalises each kind's best hit to 1.0 so that one
kind cannot drown the others, and it reports the normalised value as
``merged_score`` rather than pretending the raw numbers were calibrated.

"EXACT" IS VERIFIED, NOT ASSERTED
=================================
A passage is re-read from its source and checked to be a non-empty substring of
it before it is returned: the file line at ``path:line``, the stored value of
an awm row, the blob at ``sha:path`` (``sha^:path`` for a removed line). Text is
never reflowed, never whitespace-collapsed, and never annotated with a suffix.
A row whose source cannot be read, or whose passage does not verify, is DROPPED
and counted in its source's report, so a caller can tell "nothing matched" from
"matches were thrown away". Web snippets are the provider's own text and there
is no local source to re-read, so the verbatim check applies to the three local
kinds; a web result with no snippet has no passage at all and is dropped and
counted too.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

from awfind.client import QUERY_MAX_CHARS, FindError
from awfind.config import ConfigError, UnresolvedError

__all__ = [
    "KINDS",
    "SCORE_KIND",
    "Passage",
    "SourceReport",
    "SourceUnavailableError",
    "SourceFailedError",
    "parse_kinds",
    "merge",
    "verbatim",
    "exact_passage",
    "parse_git_log",
    "search_web",
    "search_code",
    "search_memory",
    "search_history",
    "repo_root",
    "federate",
]

#: The sources, in the order used to break ties after normalisation. Web first
#: because it is the package's original surface; the local bricks follow in the
#: order an agent tends to reach for them.
KINDS = ("web", "code", "memory", "history")
_KIND_ORDER = {kind: i for i, kind in enumerate(KINDS)}

#: What a raw ``score`` MEANS for each kind. The numbers are not comparable
#: across kinds; naming them stops a caller from sorting 0.7 (an awm scope
#: weight) above 0.7 (a service relevance score) and calling that a ranking.
SCORE_KIND = {
    "web": "service relevance",
    "code": "position in awgraph's hybrid ranking",
    "memory": "awm scope weight",
    "history": "recency of the commit",
}

#: awgraph loads a persisted chunk cache and hydrates vectors on every query.
#: Measured 2026-10-26: 51-84 s cold on a 519,178-chunk index (83.6 s live). The
#: other three are fast; the budget is set so the slow one reports a timeout
#: instead of hanging a caller forever. ``blob`` bounds one ``git show`` for
#: passage verification.
_TIMEOUTS = {"code": 240.0, "memory": 30.0, "history": 60.0, "blob": 30.0}

#: A passage longer than this is clipped to a contiguous slice. Clipping never
#: rewrites the text, so the result is still a substring of the source.
_PASSAGE_MAX = 600

_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

Runner = Callable[..., Tuple[int, str, str]]
Which = Callable[[str], Optional[str]]


class SourceUnavailableError(RuntimeError):
    """The brick is absent, or has nothing to answer from (no index).

    Distinct from :class:`SourceFailedError`: "not installed" and "installed and
    broken" have different fixes, and one message for both sends half the
    readers to the wrong place.
    """


class SourceFailedError(RuntimeError):
    """The brick ran and failed. The report carries its first stderr line."""


@dataclass
class Passage:
    """One result: an exact passage with a locator precise enough to open it."""

    title: str
    passage: str
    locator: str
    kind: str
    score: float
    meta: dict = field(default_factory=dict)
    #: Set by :func:`merge`: this kind's best hit is 1.0.
    merged_score: float = 0.0
    rank: int = 0

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "kind": self.kind,
            "title": self.title,
            "passage": self.passage,
            "locator": self.locator,
            "score": round(float(self.score), 4),
            "score_kind": SCORE_KIND.get(self.kind, "score"),
            "merged_score": round(float(self.merged_score), 4),
            "meta": self.meta,
        }


@dataclass
class SourceReport:
    """What one selected source did. Every selection gets exactly one report."""

    kind: str
    status: str  # "answered" | "unavailable" | "failed"
    detail: str = ""
    count: int = 0
    #: Rows the source returned that could not be verified as verbatim. Counted,
    #: never hidden: it is the difference between no match and discarded matches.
    dropped: int = 0

    def to_dict(self) -> dict:
        return {"kind": self.kind, "status": self.status,
                "detail": self.detail, "count": self.count,
                "dropped": self.dropped}


def parse_kinds(spec: Optional[Any] = None) -> List[str]:
    """Validate a source selector. Raises ValueError naming the valid kinds.

    Accepts "web,code" or an iterable. Duplicates collapse; the caller's order
    is preserved so a report reads in the order that was asked for.
    """
    if spec is None:
        return list(KINDS)
    items = spec.split(",") if isinstance(spec, str) else list(spec)
    wanted: List[str] = []
    for item in items:
        name = str(item).strip().lower()
        if name and name not in wanted:
            wanted.append(name)
    if not wanted:
        raise ValueError(f"no sources selected — choose from: {', '.join(KINDS)}")
    unknown = [name for name in wanted if name not in KINDS]
    if unknown:
        raise ValueError(
            f"unknown source {unknown[0]!r} — choose from: {', '.join(KINDS)}"
        )
    return wanted


def merge(rows: Sequence[Passage], limit: int) -> List[Passage]:
    """Merge kinds into one ranked list, each kind's best normalised to 1.0.

    Cross-kind scores are not calibrated (see :data:`SCORE_KIND`), so sorting
    the raw numbers would let whichever source happens to use a larger scale
    win every query. Normalising within each kind gives every kind one shot at
    the top, then ties fall to :data:`KINDS` order so the output is stable.
    Within a kind the source's own order is kept, and a repeated locator is
    deduplicated (best-ranked copy wins).
    """
    tops: dict = {}
    for row in rows:
        tops[row.kind] = max(tops.get(row.kind, 0.0), float(row.score))

    def key(pair: Tuple[int, Passage]) -> tuple:
        index, row = pair
        top = tops.get(row.kind, 0.0)
        norm = (float(row.score) / top) if top > 0 else 0.0
        return (-norm, _KIND_ORDER.get(row.kind, len(KINDS)), index)

    ordered = [row for _, row in sorted(enumerate(rows), key=key)]
    seen: set = set()
    out: List[Passage] = []
    for row in ordered:
        dedupe = (row.kind, row.locator)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        top = tops.get(row.kind, 0.0)
        row.merged_score = (float(row.score) / top) if top > 0 else 0.0
        out.append(row)
    out = out[:max(0, int(limit))]
    for i, row in enumerate(out, 1):
        row.rank = i
    return out


def verbatim(passage: str, source: str) -> bool:
    """True when *passage* is a non-empty substring of its *source*.

    This is the whole exactness contract in one predicate: a passage that is not
    in the source is a paraphrase, and a paraphrase labelled exact is the defect
    the verification in every local source exists to prevent.
    """
    return bool(passage) and passage in (source or "")


def _clip(line: str, at: int = -1, width: int = _PASSAGE_MAX) -> str:
    """A contiguous slice of one line, preferring word boundaries when clipping.

    The slice is taken from *line* unchanged — no whitespace collapsing and no
    ellipsis markers — so the result stays a substring of the line, and the
    caller can verify it against the whole source.
    """
    line = line.strip()
    if len(line) <= width:
        return line
    start = 0 if at < 0 else max(0, at - width // 3)
    if start:
        space = line.find(" ", start)
        if 0 <= space < at:
            start = space + 1
    end = min(len(line), start + width)
    if end < len(line):
        space = line.rfind(" ", start, end)
        if space > start:
            end = space
    return line[start:end].strip()


def exact_passage(text: str, query: str, width: int = _PASSAGE_MAX) -> str:
    """The first source line that answers *query*, as a verified verbatim slice.

    A 4,000-character memory value pasted whole is not a passage; the line that
    answers the query is. The line is chosen, never rewritten: whitespace is
    kept exactly as stored and no ellipsis is invented. Returns "" when nothing
    can be excerpted or when the slice fails :func:`verbatim`.
    """
    terms = [term.lower() for term in (query or "").split() if term]
    fallback = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if not fallback:
            fallback = line
        low = line.lower()
        for term in terms:
            at = low.find(term)
            if at >= 0:
                chunk = _clip(line, at, width)
                return chunk if verbatim(chunk, text) else ""
    chunk = _clip(fallback, -1, width)
    return chunk if verbatim(chunk, text) else ""


def _first_line(text: str, limit: int = 240) -> str:
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line:
            return line[:limit]
    return ""


def _json_rows(text: str) -> List[dict]:
    try:
        data = json.loads(text or "")
    except ValueError as exc:
        raise SourceFailedError(f"unparseable JSON from the brick: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("results") or data.get("rows") or []
    if not isinstance(data, list):
        raise SourceFailedError("the brick answered with JSON that is not a list")
    return [row for row in data if isinstance(row, dict)]


def _read_passage(root: str, relpath: str, line: int,
                  query: str = "") -> str:
    """One exact line of the file at a code hit, verified against the file.

    Re-read, never taken from the index: awgraph reports a path and a line, and
    the passage has to come from the source itself. A single line is the unit
    because it is the only slice whose newline convention cannot make a verbatim
    claim false. Bounded three ways: the path must resolve inside the indexed
    root, the line must be plausible, and the slice is clipped. Returns "" when
    nothing can be verified; the caller DROPS that row rather than substituting
    the index's signature, which is not text from the file.
    """
    if not relpath or line < 1 or line > 100_000:
        return ""
    try:
        base = Path(root).resolve()
        target = (base / relpath).resolve()
    except (OSError, ValueError):
        return ""
    if target != base and base not in target.parents:
        return ""
    if target.is_dir():
        return ""
    try:
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return ""
    lines = text.splitlines()
    # The hit line first, then the next and the previous: an index can point at
    # a blank separator, and the neighbouring line is still exact source text.
    for index in (line - 1, line, line - 2):
        if 0 <= index < len(lines):
            found = exact_passage(lines[index], query)
            if found and verbatim(found, text):
                return found
    return ""


def _run(argv: Sequence[str], *, cwd: Optional[str] = None,
         timeout: float = 60.0) -> Tuple[int, str, str]:
    """Run a brick and CAPTURE its output. Never inherit stdio.

    Inherited stdio is what makes ``awgit git -- log`` print nothing on this
    host (see the module docstring); an explicit pipe is also what lets the
    caller tell "no matches" from "the brick never spoke".
    """
    kwargs: dict = {}
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if flags:
            # A console-less parent spawning a console child flashes a window
            # that steals focus (a helper that raises another app's window
            # uninvited). Harmless off Windows.
            kwargs["creationflags"] = flags
    try:
        done = subprocess.run(
            list(argv), cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, **kwargs,
        )
    except FileNotFoundError as exc:
        raise SourceUnavailableError(f"{argv[0]} is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise SourceFailedError(f"{argv[0]}: timed out after {timeout:.0f}s") from exc
    return done.returncode, done.stdout or "", done.stderr or ""


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


#: One probe per (awgit, root): does awgit's forwarded stdout actually arrive?
#: Cached because the probe is a process spawn and the answer cannot change
#: while a command runs.
_LAUNCHER_CACHE: dict = {}


def _history_launcher(which: Which, run: Runner,
                      root: Optional[str]) -> Tuple[List[str], str]:
    """(argv prefix that behaves like git, launcher label) for history reads.

    Prefers awgit because it is the history brick and owns the surface. The
    probe is not paranoia: measured 2026-10-26, ``awgit git -- rev-parse HEAD``
    exits 0 with empty stdout on Windows, so trusting it would report every
    repository as having no history. git is what awgit forwards to, so falling
    back to it changes nothing about the data — only who is credited.
    """
    awgit = which("awgit")
    if awgit:
        key = (awgit, root)
        usable = _LAUNCHER_CACHE.get(key)
        if usable is None:
            try:
                code, out, _err = run(
                    [awgit, "git", "--", "rev-parse", "--show-toplevel"],
                    cwd=root, timeout=15.0,
                )
                usable = code == 0 and bool(out.strip())
            except (SourceUnavailableError, SourceFailedError):
                usable = False
            _LAUNCHER_CACHE[key] = usable
        if usable:
            return [awgit, "git", "--"], "awgit"
    git = which("git")
    if not git:
        raise SourceUnavailableError("neither awgit nor git is on PATH")
    if awgit:
        return [git], "git (awgit forwarding returned no stdout)"
    return [git], "git"


def repo_root(run: Runner = _run, which: Which = _which) -> Optional[str]:
    """The git top level, so an index lookup does not depend on the shell's cwd.

    awgraph indexes a ROOT, and a query from a subdirectory looks for an index
    under that subdirectory. Every measured "no index" on a healthy machine was
    this mismatch, so the root is resolved once and passed down.
    """
    git = which("git")
    if not git:
        return None
    try:
        code, out, _err = run([git, "rev-parse", "--show-toplevel"], timeout=15.0)
    except (SourceUnavailableError, SourceFailedError):
        return None
    if code != 0:
        return None
    return out.strip() or None


def _clean_query(query: str) -> str:
    text = (query or "").strip()
    if not text:
        raise ValueError("query must not be empty")
    if len(text) > QUERY_MAX_CHARS:
        raise ValueError(
            f"query is {len(text)} chars (max {QUERY_MAX_CHARS}) — distill it before searching"
        )
    return " ".join(text.split())


# ── sources ────────────────────────────────────────────────────────────────


def _verified_detail(base: str, kept: int, dropped: int, unit: str) -> str:
    """The report line: how many passages came back, and how many were discarded."""
    detail = f"{base} ({kept} verified {unit}"
    if dropped:
        detail += f", {dropped} dropped: not verbatim in the source"
    return detail + ")"


def search_web(query: str, limit: int, client: Any,
               extra: Optional[dict] = None) -> Tuple[List[Passage], str, int]:
    """The configured search service: whatever providers it has.

    A snippet is the provider's own text: there is no local source to re-read,
    so nothing can be verified against. A result with no snippet has no passage
    and is dropped and counted, because a row is supposed to BE an exact passage.
    """
    try:
        answer = client.quick(query, limit=limit, auto_learn=False)
    except FindError as exc:
        raise SourceFailedError(f"search service: {exc}") from exc
    rows = []
    dropped = 0
    for result in answer.results:
        passage = (result.snippet or "").strip()
        if not passage:
            dropped += 1
            continue
        rows.append(Passage(
            title=result.title or result.url,
            passage=passage,
            locator=result.url or f"{result.source}:{result.title}",
            kind="web",
            score=float(result.score or 0.0),
            meta={"provider": result.source or answer.provider,
                  "url": result.url},
        ))
    if extra is not None:
        extra["answer"] = answer.answer
        extra["citations"] = list(answer.citations or [])
        extra["provider"] = answer.provider
    detail = f"{answer.provider or 'search service'} ({len(rows)} hits"
    if dropped:
        detail += f", {dropped} dropped: no snippet"
    return rows, detail + ")", dropped


def search_code(query: str, limit: int, root: str, *,
                which: Which = _which,
                run: Runner = _run) -> Tuple[List[Passage], str, int]:
    """awgraph: hybrid keyword+semantic retrieval over the repository index."""
    awgraph = which("awgraph")
    if not awgraph:
        raise SourceUnavailableError("awgraph not on PATH — pip install awgraph")
    code, out, err = run(
        [awgraph, "query", "--path", root, "--json", "-k", str(int(limit)), "--", query],
        timeout=_TIMEOUTS["code"],
    )
    # awgraph's contract: 0 a match, 1 a real negative answer, 2 could not run.
    if code == 2:
        raise SourceUnavailableError(_first_line(err) or "awgraph could not run (exit 2)")
    if code not in (0, 1):
        raise SourceFailedError(f"awgraph query: exit {code}: {_first_line(err)}")
    raw = _json_rows(out)
    rows: List[Passage] = []
    dropped = 0
    for i, item in enumerate(raw[:limit]):
        path = str(item.get("path") or "")
        line = int(item.get("line") or 0)
        name = str(item.get("name") or "(unnamed)")
        signature = str(item.get("signature") or "")
        # The passage is the FILE's line, verified against the file. The
        # signature is index metadata, not source text, so it is reported in
        # meta and never substituted for a passage that failed verification.
        passage = _read_passage(root, path, line, query) if path else ""
        if not passage:
            dropped += 1
            continue
        rows.append(Passage(
            title=name,
            passage=passage,
            locator=f"{path}:{line}",
            kind="code",
            score=round(1.0 - i / max(len(raw), 1), 4),
            meta={"type": item.get("type", ""), "path": path,
                  "line": line, "signature": signature},
        ))
    return rows, _verified_detail(f"awgraph from {root}", len(rows), dropped,
                                  "chunks"), dropped


def search_memory(query: str, limit: int, scope: Optional[str] = None, *,
                  which: Which = _which,
                  run: Runner = _run) -> Tuple[List[Passage], str, int]:
    """awm: the scoped memory the next agent is meant to read before debugging."""
    awm = which("awm")
    if not awm:
        raise SourceUnavailableError("awm not on PATH — pip install awm")
    argv = [awm, "recall", "--query", query, "--json", "--limit", str(int(limit))]
    if scope:
        argv.extend(["--scope", scope])
    code, out, err = run(argv, timeout=_TIMEOUTS["memory"])
    if code != 0:
        raise SourceFailedError(f"awm recall: exit {code}: {_first_line(err)}")
    rows: List[Passage] = []
    dropped = 0
    for item in _json_rows(out)[:limit]:
        key = str(item.get("key") or "")
        value = str(item.get("value") or "")
        where = str(item.get("scope") or scope or "")
        # The passage is a slice of the STORED value, verified against it.
        passage = exact_passage(value, query)
        if not passage:
            dropped += 1
            continue
        weight = item.get("weight")
        score = float(weight) if isinstance(weight, (int, float)) else 1.0
        rows.append(Passage(
            title=key or where,
            passage=passage,
            locator=f"{where}:{key}",
            kind="memory",
            score=score,
            meta={"scope": where, "key": key,
                  "memory_kind": item.get("kind", ""),
                  "updated": item.get("updated")},
        ))
    return rows, _verified_detail("awm recall", len(rows), dropped, "memories"), dropped


def _matches(text: str, terms: Sequence[str]) -> bool:
    low = text.lower()
    return bool(terms) and all(term in low for term in terms)


def parse_git_log(text: str, terms: Sequence[str], per_commit: int = 2) -> List[dict]:
    """Exact changed lines out of ``git log -G -U0 -p``. Pure.

    Each record is a commit header written with ``--format`` and ``\\x1e`` as
    the record separator, followed by zero or more unified diffs. A line counts
    when it CHANGED and contains every query term: the removed side and the
    added side are different facts, so both are reported with the side named
    and the line number on the side it belongs to.
    """
    records: List[dict] = []
    for block in (text or "").split("\x1e"):
        lines = block.strip("\n").splitlines()
        if not lines:
            continue
        header = lines[0].split("\x1f")
        if len(header) < 4:
            continue
        sha, short, date, subject = header[0], header[1], header[2], header[3]
        path = ""
        old_line = new_line = 0
        emitted = 0
        for line in lines[1:]:
            if line.startswith("diff --git "):
                path = ""
            elif line.startswith("+++ "):
                target = line[4:].split("\t")[0].strip()
                if target != "/dev/null":
                    path = target[2:] if target.startswith("b/") else target
            elif line.startswith("--- "):
                source = line[4:].split("\t")[0].strip()
                if source != "/dev/null" and not path:
                    path = source[2:] if source.startswith("a/") else source
            elif line.startswith("@@"):
                found = _HUNK.match(line)
                if found:
                    old_line = int(found.group(1))
                    new_line = int(found.group(2))
            elif line.startswith("+") and not line.startswith("+++"):
                body = line[1:]
                if path and emitted < per_commit and _matches(body, terms):
                    records.append({"sha": sha, "short": short, "date": date,
                                    "subject": subject, "path": path,
                                    "line": new_line, "side": "added",
                                    "text": body})
                    emitted += 1
                new_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                body = line[1:]
                if path and emitted < per_commit and _matches(body, terms):
                    records.append({"sha": sha, "short": short, "date": date,
                                    "subject": subject, "path": path,
                                    "line": old_line, "side": "removed",
                                    "text": body})
                    emitted += 1
                old_line += 1
    return records


def _blob(launcher: Sequence[str], run: Runner, revision: str, path: str,
          cwd: Optional[str]) -> Optional[str]:
    """The file's text at *revision*, or None when it cannot be read.

    This is the re-read the history passage's exactness rests on: the diff says
    a line changed, but only the blob proves the text. A missing blob (a file
    created in that same commit on the removed side, a path that moved) is a
    DROP, never a guess.
    """
    try:
        code, out, _err = run(
            [*launcher, "show", f"{revision}:{path}"],
            cwd=cwd, timeout=_TIMEOUTS["blob"],
        )
    except (SourceUnavailableError, SourceFailedError):
        return None
    return out if code == 0 else None


def search_history(query: str, limit: int, root: Optional[str] = None, *,
                   which: Which = _which,
                   run: Runner = _run) -> Tuple[List[Passage], str, int]:
    """git history: commits whose diff changed a line the query names."""
    terms = [term.lower() for term in query.split() if term]
    if not terms:
        return [], "git (empty query)", 0
    launcher, label = _history_launcher(which, run, root)
    # `-G` is POSIX-regex and has no lookahead, so it narrows by the LONGEST
    # term only; the local match then requires every term on the line. The
    # pattern is ATTACHED so a term that starts with "-" is not read as a flag.
    probe = re.escape(max(terms, key=len))
    argv = [*launcher, "log",
            f"--max-count={int(limit)}", f"-G{probe}",
            "--unified=0", "-p", "--date=short",
            "--format=%x1e%H%x1f%h%x1f%ad%x1f%s"]
    if root:
        argv.extend(["--", "."])
    code, out, err = run(argv, cwd=root, timeout=_TIMEOUTS["history"])
    if code != 0:
        raise SourceFailedError(f"{label} log: exit {code}: {_first_line(err)}")
    found = parse_git_log(out, terms)
    rows: List[Passage] = []
    dropped = 0
    blobs: dict = {}
    for i, item in enumerate(found[:limit]):
        # Added lines live in the commit's own blob; removed lines only exist in
        # the parent's. The provenance ("added in <sha>") lives in meta — a
        # suffix in the passage would stop it being text from the blob.
        revision = item["sha"] if item["side"] == "added" else f"{item['sha']}^"
        key = (revision, item["path"])
        if key not in blobs:
            blobs[key] = _blob(launcher, run, revision, item["path"], root)
        blob = blobs[key]
        passage = exact_passage(item["text"], query) if blob is not None else ""
        if not passage or not verbatim(passage, blob):
            dropped += 1
            continue
        rows.append(Passage(
            title=str(item["subject"]) or str(item["short"]),
            passage=passage,
            locator=f"{item['short']}:{item['path']}:{item['line']}",
            kind="history",
            score=round(1.0 - i / max(len(found), 1), 4),
            meta={"sha": item["sha"], "short": item["short"],
                  "date": item["date"], "path": item["path"],
                  "line": item["line"], "side": item["side"]},
        ))
    return rows, _verified_detail(f"{label} log -G", len(rows), dropped,
                                  "changed lines"), dropped


# ── the one entry point ────────────────────────────────────────────────────


def federate(query: str, *, kinds: Optional[Any] = None, limit: int = 10,
             root: Optional[str] = None, scope: Optional[str] = None,
             web_client_factory: Optional[Callable[[], Any]] = None,
             which: Which = _which, run: Runner = _run) -> dict:
    """Search every selected source and return one merged, labelled payload.

    A missing brick, an unconfigured web service, a brick that fails, and a
    brick that is simply slow are four different facts; every selected source
    gets a report with one of ``answered`` / ``unavailable`` / ``failed``. The
    web search is never killed by a local brick, and a local brick is never
    hidden behind a web failure.
    """
    text = _clean_query(query)
    wanted = parse_kinds(kinds)
    if int(limit) < 1:
        raise ValueError("limit must be at least 1")
    limit = int(limit)
    where = os.path.abspath(root) if root else (repo_root(run=run, which=which) or os.getcwd())

    web_extra: dict = {}

    def do_web() -> Tuple[List[Passage], str, int]:
        if web_client_factory is None:
            raise SourceUnavailableError(
                "no search service configured — run: awfind config set --url https://host:port"
            )
        try:
            client = web_client_factory()
        except (UnresolvedError, ConfigError) as exc:
            raise SourceUnavailableError(str(exc)) from exc
        return search_web(text, limit, client, web_extra)

    jobs: dict = {}
    if "web" in wanted:
        jobs["web"] = do_web
    if "code" in wanted:
        jobs["code"] = partial(search_code, text, limit, where, which=which, run=run)
    if "memory" in wanted:
        jobs["memory"] = partial(search_memory, text, limit, scope, which=which, run=run)
    if "history" in wanted:
        jobs["history"] = partial(search_history, text, limit, where, which=which, run=run)

    reports: List[SourceReport] = []
    rows: List[Passage] = []
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(jobs)))) as pool:
        futures = {kind: pool.submit(job) for kind, job in jobs.items()}
        for kind in wanted:
            try:
                found, detail, dropped = futures[kind].result()
            except SourceUnavailableError as exc:
                reports.append(SourceReport(kind, "unavailable", str(exc), 0))
                continue
            except SourceFailedError as exc:
                reports.append(SourceReport(kind, "failed", str(exc), 0))
                continue
            except Exception as exc:  # noqa: BLE001 - a brick must never kill the search
                reports.append(SourceReport(kind, "failed",
                                            f"{type(exc).__name__}: {exc}", 0))
                continue
            rows.extend(found)
            reports.append(SourceReport(kind, "answered", detail, len(found),
                                        int(dropped or 0)))

    merged = merge(rows, limit)
    payload: dict = {
        "query": text,
        "root": where,
        "sources_requested": wanted,
        "sources_answered": [r.kind for r in reports if r.status == "answered"],
        "sources_unavailable": [r.kind for r in reports if r.status == "unavailable"],
        "sources_failed": [r.kind for r in reports if r.status == "failed"],
        "sources": [r.to_dict() for r in reports],
        "results": [row.to_dict() for row in merged],
    }
    if scope:
        payload["scope"] = scope
    if web_extra.get("answer"):
        payload["answer"] = web_extra["answer"]
        payload["citations"] = web_extra.get("citations") or []
    return payload
