"""Federation: every source labelled, one merged ranking, no silence.

Every subprocess and network seam is injected. These tests therefore run on a
machine with no awgraph index, no awm database and no search service, and still
pin each source's invocation, parsing and failure shape. Where a plausible
"simplification" would make a test green while breaking the product contract,
the test comment says which one.
"""

from __future__ import annotations

import json

import pytest
from awfind import federate as fed
from awfind.client import Answer, FindError


def row(kind: str, score: float, locator: str, title: str = "t") -> fed.Passage:
    return fed.Passage(title, "p", locator, kind, score)


class FakeWeb:
    def __init__(self, results=None, answer=None, citations=None, fail=None):
        self.results = results if results is not None else [
            {"title": "Web hit", "url": "https://e.x", "snippet": "web passage",
             "score": 0.9, "source": "fake"}]
        self.answer = answer
        self.citations = citations or []
        self.fail = fail
        self.calls = []

    def quick(self, query, **kwargs):
        self.calls.append((query, kwargs))
        if self.fail:
            raise FindError(self.fail)
        return Answer({"provider": "fake", "results": self.results,
                       "answer": self.answer, "citations": self.citations})


def never_runs(*_args, **_kwargs):
    raise AssertionError("a brick that could not be located was executed")


# ── selector and merge ─────────────────────────────────────────────────────


def test_default_is_every_source_and_the_asked_order_is_kept():
    assert fed.parse_kinds() == ["web", "code", "memory", "history"]
    assert fed.parse_kinds("history, web") == ["history", "web"]
    assert fed.parse_kinds("web,code,web") == ["web", "code"]


@pytest.mark.parametrize("bad", ["", "   ", "web,nope", []])
def test_an_unknown_or_empty_selector_is_refused_with_the_valid_set(bad):
    with pytest.raises(ValueError) as exc:
        fed.parse_kinds(bad)
    assert "web" in str(exc.value) and "history" in str(exc.value)


def test_merge_normalises_each_kind_by_its_own_best_not_by_the_largest_number():
    """Mutation guard: sorting raw scores puts code first (40 beats 0.1).

    The numbers are different units -- a scope weight, a rank position, a
    relevance score -- so only within-kind ordering is real information.
    """
    merged = fed.merge([row("web", 0.1, "u1"), row("code", 40.0, "a:1"),
                        row("code", 20.0, "a:2")], limit=10)
    assert [r.kind for r in merged] == ["web", "code", "code"]
    assert [r.merged_score for r in merged] == [1.0, 1.0, 0.5]
    assert [r.rank for r in merged] == [1, 2, 3]


def test_merge_ties_fall_to_the_declared_source_order_not_input_order():
    merged = fed.merge([row("history", 1.0, "h"), row("memory", 1.0, "m"),
                        row("code", 1.0, "c"), row("web", 1.0, "w")], limit=10)
    assert [r.kind for r in merged] == ["web", "code", "memory", "history"]


def test_merge_deduplicates_a_locator_within_a_kind_and_applies_the_limit_last():
    merged = fed.merge([row("code", 5.0, "a:1"), row("code", 4.0, "a:1"),
                        row("code", 3.0, "a:2")], limit=10)
    assert len(merged) == 2 and merged[0].merged_score == 1.0
    assert len(fed.merge([row("web", 1.0, "u1"), row("code", 1.0, "a:1")], limit=1)) == 1


def test_a_kind_with_only_zero_scores_still_ranks_without_dividing_by_zero():
    merged = fed.merge([row("memory", 0.0, "m")], limit=5)
    assert len(merged) == 1 and merged[0].merged_score == 0.0


def test_an_excerpt_is_a_verbatim_contiguous_slice_of_its_source():
    """Mutation guard: collapsing whitespace makes the passage a paraphrase.

    The source stores "hookd  pool" with two spaces; returning "hookd pool"
    would read as exact while existing nowhere in the memory.
    """
    long_value = "x" * 300 + " hookd  pool " + "y" * 300
    got = fed.exact_passage(long_value, "hookd pool")
    assert "hookd  pool" in got
    assert "hookd pool" not in got          # not the collapsed spelling
    assert len(got) <= 600
    assert got in long_value                # the contract, checked


def test_an_excerpt_of_a_multiline_value_never_joins_lines():
    value = "a  b\n c hookd\n"
    got = fed.exact_passage(value, "hookd")
    assert got == "c hookd" and got in value
    assert fed.exact_passage(value, "a") == "a  b"


def test_an_empty_source_produces_no_passage_and_verbatim_refuses_absence():
    assert fed.exact_passage("", "q") == ""
    assert fed.verbatim("hookd", "a hookd worker") is True
    assert fed.verbatim("", "a hookd worker") is False
    assert fed.verbatim("hookd pool", "a hookd worker") is False


# ── git log parsing ────────────────────────────────────────────────────────

LOG_TWO_SIDES = (
    "\x1e" + "a" * 40 + "\x1fabc1234\x1f2026-01-02\x1ffix the pool\n"
    "diff --git a/pkg/x.py b/pkg/x.py\n"
    "--- a/pkg/x.py\n+++ b/pkg/x.py\n"
    "@@ -10,0 +11,2 @@\n"
    "+pool = 24  # hookd workers\n"
    "+pool = 25\n"
    "@@ -40 +41,0 @@\n"
    "-pool = 6  # hookd workers\n"
)


def test_git_log_reports_the_changed_side_with_its_own_line_number():
    rows = fed.parse_git_log(LOG_TWO_SIDES, ["hookd", "pool"])
    assert [(r["side"], r["line"]) for r in rows] == [("added", 11), ("removed", 40)]
    assert all(r["path"] == "pkg/x.py" and r["short"] == "abc1234" for r in rows)


def test_git_log_requires_every_query_term_on_the_line():
    """Mutation guard: matching ANY term would make "pool = 25" a hit."""
    rows = fed.parse_git_log(LOG_TWO_SIDES, ["hookd", "absent"])
    assert rows == []


def test_git_log_caps_the_passages_per_commit():
    rows = fed.parse_git_log(LOG_TWO_SIDES, ["pool"], per_commit=1)
    assert len(rows) == 1


def test_git_log_finds_the_path_of_a_deleted_file():
    log = ("\x1e" + "b" * 40 + "\x1fdeadbee\x1f2026-01-01\x1fdrop it\n"
           "diff --git a/gone.py b/gone.py\n"
           "--- a/gone.py\n+++ /dev/null\n"
           "@@ -7 +0,0 @@\n"
           "-removed hookd line\n")
    rows = fed.parse_git_log(log, ["hookd"])
    assert [(r["path"], r["line"], r["side"]) for r in rows] == [("gone.py", 7, "removed")]


# ── the subprocess seam (the awgit trap) ───────────────────────────────────


def test_run_captures_stdio_and_names_its_encoding(monkeypatch):
    """Mutation guard: inherited stdio is the measured awgit empty-output bug.

    `awgit git -- log` exits 0 with no stdout when its pipes are inherited
    (2026-10-26), which reads as "no history". An explicit capture is what makes
    "no matches" distinguishable from "the brick never spoke".
    """
    seen = {}

    class Done:
        returncode = 0
        stdout = "out"
        stderr = ""

    def fake(argv, **kwargs):
        seen.update(kwargs)
        return Done()

    monkeypatch.setattr(fed.subprocess, "run", fake)
    assert fed._run(["git", "log"]) == (0, "out", "")
    assert seen["capture_output"] is True
    assert seen["encoding"] == "utf-8"


def test_run_maps_a_timeout_to_failed_and_a_missing_binary_to_unavailable(monkeypatch):
    import subprocess

    def timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="awgraph", timeout=1)

    monkeypatch.setattr(fed.subprocess, "run", timeout)
    with pytest.raises(fed.SourceFailedError):
        fed._run(["awgraph", "query"])

    def missing(*_a, **_k):
        raise FileNotFoundError("nope")

    monkeypatch.setattr(fed.subprocess, "run", missing)
    with pytest.raises(fed.SourceUnavailableError):
        fed._run(["awgraph", "query"])


# ── code source ────────────────────────────────────────────────────────────


def test_code_source_returns_the_exact_file_line_and_a_locator(tmp_path):
    src = tmp_path / "pkg" / "mod.py"
    src.parent.mkdir()
    src.write_text("line one\nline two target\nline three\n", encoding="utf-8")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return 0, json.dumps([{"name": "target", "type": "function",
                               "path": "pkg/mod.py", "line": 2,
                               "signature": "def target()"}]), ""

    rows, detail, dropped = fed.search_code("target", 5, str(tmp_path),
                                            which=lambda _n: "/opt/bin/awgraph", run=run)
    assert calls[0][0][:2] == ["/opt/bin/awgraph", "query"]
    assert "--path" in calls[0][0] and str(tmp_path) in calls[0][0]
    assert rows[0].locator == "pkg/mod.py:2"
    # Mutation guard: returning the index's signature instead of reading the
    # file loses the exact passage, which is the product.
    assert rows[0].passage == "line two target"
    assert rows[0].passage in src.read_text(encoding="utf-8")
    assert dropped == 0 and "1 verified" in detail


def test_code_source_drops_a_hit_it_cannot_verify_and_never_quotes_the_index(tmp_path):
    """Mutation guard: a path outside the root used to fall back to the signature.

    The signature is awgraph's metadata, not text in the file, so it cannot be
    returned as an exact passage. The row is dropped and counted instead.
    """
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret.py").write_text("secret hookd line\n", encoding="utf-8")

    def run(argv, **kwargs):
        return 0, json.dumps([{"name": "leak", "type": "function",
                               "path": "../secret.py", "line": 1,
                               "signature": "def leak()"}]), ""

    rows, detail, dropped = fed.search_code("hookd", 5, str(root),
                                            which=lambda _n: "awgraph", run=run)
    assert rows == [] and dropped == 1
    assert "1 dropped" in detail


def test_code_source_drops_a_hit_whose_file_is_missing(tmp_path):
    def run(argv, **kwargs):
        return 0, json.dumps([{"name": "ghost", "path": "pkg/ghost.py",
                               "line": 3, "signature": "def ghost()"}]), ""

    rows, _detail, dropped = fed.search_code("ghost", 5, str(tmp_path),
                                             which=lambda _n: "awgraph", run=run)
    assert rows == [] and dropped == 1


def test_code_source_rc2_is_unavailable_with_awgraphs_own_reason(tmp_path):
    """Mutation guard: rc 2 with empty stdout must not read as zero results."""
    def run(argv, **kwargs):
        return 2, "", "no index for /repo -- run: awgraph index /repo\n"

    with pytest.raises(fed.SourceUnavailableError) as exc:
        fed.search_code("q", 5, str(tmp_path), which=lambda _n: "awgraph", run=run)
    assert "no index" in str(exc.value)


def test_code_source_rc1_with_an_empty_json_list_is_an_answered_zero(tmp_path):
    def run(argv, **kwargs):
        return 1, "[]", ""

    rows, _detail, dropped = fed.search_code("q", 5, str(tmp_path),
                                             which=lambda _n: "awgraph", run=run)
    assert rows == [] and dropped == 0


def test_code_source_without_awgraph_is_unavailable(tmp_path):
    with pytest.raises(fed.SourceUnavailableError) as exc:
        fed.search_code("q", 5, str(tmp_path), which=lambda _n: None, run=never_runs)
    assert "pip install awgraph" in str(exc.value)


# ── memory source ──────────────────────────────────────────────────────────


def test_memory_source_returns_a_verbatim_slice_and_a_scope_key_locator():
    value = "y" * 400 + " six workers " + "z" * 400
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return 0, json.dumps([{
            "scope": "aitherium:x:repo", "key": "hookd",
            "value": value,
            "kind": "fact", "weight": 0.7, "updated": "2026-10-26",
        }]), ""

    rows, detail, dropped = fed.search_memory("six workers", 5,
                                              which=lambda _n: "awm", run=run)
    assert "--scope" not in calls[0]
    assert rows[0].locator == "aitherium:x:repo:hookd"
    assert rows[0].score == 0.7
    # The stored spelling, exactly: a substring that starts at the term.
    assert rows[0].passage.startswith("six workers")
    assert rows[0].passage in value and len(rows[0].passage) <= 600
    assert dropped == 0 and "1 verified" in detail
    assert rows[0].meta["memory_kind"] == "fact"


def test_memory_source_drops_a_value_it_cannot_quote():
    """Mutation guard: a row with no quotable text must be counted, not shipped."""
    def run(argv, **kwargs):
        return 0, json.dumps([{"scope": "s", "key": "k", "value": "",
                               "weight": 1.0}]), ""

    rows, detail, dropped = fed.search_memory("q", 5,
                                              which=lambda _n: "awm", run=run)
    assert rows == [] and dropped == 1
    assert "1 dropped" in detail


def test_memory_source_passes_an_explicit_scope():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return 0, "[]", ""

    fed.search_memory("q", 5, scope="mine", which=lambda _n: "awm", run=run)
    assert calls[0][-2:] == ["--scope", "mine"]


def test_memory_source_failure_carries_the_bricks_first_error_line():
    def run(argv, **kwargs):
        return 1, "", "\nmemory database is locked\n"

    with pytest.raises(fed.SourceFailedError) as exc:
        fed.search_memory("q", 5, which=lambda _n: "awm", run=run)
    assert "locked" in str(exc.value)


# ── history source ─────────────────────────────────────────────────────────

LOG_ONE = (
    "\x1e" + "a" * 40 + "\x1fabc1234\x1f2026-01-02\x1ffix the pool\n"
    "diff --git a/pkg/x.py b/pkg/x.py\n"
    "--- a/pkg/x.py\n+++ b/pkg/x.py\n"
    "@@ -10,0 +11,2 @@\n"
    "+pool = 24  # hookd workers\n"
    "+pool = 25\n"
)

#: The blob the added line above must be found in, verbatim.
BLOB_ONE = "workers = 24\npool = 24  # hookd workers\npool = 25\n"

LOG_REMOVED = (
    "\x1e" + "a" * 40 + "\x1fabc1234\x1f2026-01-02\x1fdrop the pool\n"
    "diff --git a/pkg/x.py b/pkg/x.py\n"
    "--- a/pkg/x.py\n+++ b/pkg/x.py\n"
    "@@ -40 +41,0 @@\n"
    "-pool = 6  # hookd workers\n"
)


def test_history_falls_back_to_git_when_awgits_forwarded_stdout_is_empty():
    """Mutation guard: trusting rc 0 alone calls awgit log and reports no history.

    Measured 2026-10-26: `awgit git -- rev-parse HEAD` exits 0 with empty
    stdout on Windows. The probe is about whether bytes arrive, not the exit
    code, and the report names the launcher that actually answered.
    """
    fed._LAUNCHER_CACHE.clear()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["/opt/bin/awgit", "git"]:
            return 0, "", ""                     # rc 0, but nothing arrived
        if "log" in argv:
            return 0, LOG_ONE, ""
        return 0, BLOB_ONE, ""                   # git show, for verification

    rows, detail, dropped = fed.search_history("hookd pool", 5, root=None,
                                               which=lambda n: f"/opt/bin/{n}", run=run)
    assert calls[0][:4] == ["/opt/bin/awgit", "git", "--", "rev-parse"]
    assert calls[1][0] == "/opt/bin/git"
    assert "awgit forwarding returned no stdout" in detail
    assert rows and rows[0].locator == "abc1234:pkg/x.py:11"
    assert rows[0].passage == "pool = 24  # hookd workers"
    assert rows[0].passage in BLOB_ONE
    assert dropped == 0
    fed._LAUNCHER_CACHE.clear()


def test_history_uses_awgit_when_its_stdout_round_trips():
    fed._LAUNCHER_CACHE.clear()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "rev-parse" in argv:
            return 0, "/repo\n", ""
        if "log" in argv:
            return 0, LOG_ONE, ""
        return 0, BLOB_ONE, ""

    _rows, detail, _dropped = fed.search_history("hookd pool", 5, root=None,
                                                 which=lambda n: f"/opt/bin/{n}", run=run)
    assert calls[1][:3] == ["/opt/bin/awgit", "git", "--"]
    log_argv = calls[1]
    assert any(a.startswith("--max-count=") for a in log_argv)
    assert "-Ghookd" in log_argv               # the longest term, attached
    assert "--unified=0" in log_argv
    assert detail.startswith("awgit")
    fed._LAUNCHER_CACHE.clear()


def test_history_verifies_each_line_against_the_blob_and_drops_a_mismatch():
    """Mutation guard: the diff text alone is not proof the blob ever held it."""
    fed._LAUNCHER_CACHE.clear()

    def run(argv, **kwargs):
        if "rev-parse" in argv:
            return 0, "/repo\n", ""
        if "log" in argv:
            return 0, LOG_ONE, ""
        return 0, "unrelated text\n", ""          # the blob lacks the line

    rows, detail, dropped = fed.search_history("hookd pool", 5, root=None,
                                               which=lambda n: f"/opt/bin/{n}", run=run)
    assert rows == [] and dropped == 1
    assert "1 dropped" in detail
    fed._LAUNCHER_CACHE.clear()


def test_history_verifies_a_removed_line_against_the_parent_blob():
    """A removed line exists only in the parent, so `sha^:path` is the source."""
    fed._LAUNCHER_CACHE.clear()
    shows = []

    def run(argv, **kwargs):
        if "rev-parse" in argv:
            return 0, "/repo\n", ""
        if "log" in argv:
            return 0, LOG_REMOVED, ""
        shows.append(argv)
        return 0, "line\npool = 6  # hookd workers\n", ""

    rows, _detail, dropped = fed.search_history("hookd pool", 5, root=None,
                                                which=lambda n: f"/opt/bin/{n}", run=run)
    assert rows and rows[0].meta["side"] == "removed"
    assert rows[0].passage == "pool = 6  # hookd workers"
    assert dropped == 0
    assert f"{'a' * 40}^:pkg/x.py" in shows[-1]
    fed._LAUNCHER_CACHE.clear()


def test_history_without_awgit_or_git_is_unavailable():
    with pytest.raises(fed.SourceUnavailableError):
        fed.search_history("q", 5, which=lambda _n: None, run=never_runs)


# ── the entry point ────────────────────────────────────────────────────────


def test_federate_labels_missing_bricks_and_still_answers_web(tmp_path):
    payload = fed.federate("hookd pool", kinds="web,code,memory,history", limit=5,
                           root=str(tmp_path), web_client_factory=FakeWeb,
                           which=lambda _n: None, run=never_runs)
    assert payload["sources_answered"] == ["web"]
    assert sorted(payload["sources_unavailable"]) == ["code", "history", "memory"]
    assert payload["sources_failed"] == []
    assert [r["kind"] for r in payload["results"]] == ["web"]
    assert payload["results"][0]["rank"] == 1
    assert payload["results"][0]["score_kind"] == "service relevance"


def test_federate_does_not_dial_web_when_web_is_not_selected(tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return 0, "[]", ""

    payload = fed.federate("q", kinds="memory", limit=5, root=str(tmp_path),
                           web_client_factory=never_runs,
                           which=lambda _n: "awm", run=run)
    assert payload["sources_answered"] == ["memory"]
    assert calls and "recall" in calls[0]


def test_an_unconfigured_service_is_unavailable_not_a_dead_search(tmp_path):
    from awfind.config import UnresolvedError

    def factory():
        raise UnresolvedError("no service URL")

    def run(argv, **kwargs):
        return 0, json.dumps([{"scope": "s", "key": "k", "value": "v",
                               "weight": 1.0}]), ""

    payload = fed.federate("q", kinds="web,memory", limit=5, root=str(tmp_path),
                           web_client_factory=factory,
                           which=lambda _n: "awm", run=run)
    assert payload["sources_unavailable"] == ["web"]
    assert payload["sources_answered"] == ["memory"]
    assert "no service URL" in payload["sources"][0]["detail"]


def test_an_unexpected_brick_exception_is_labelled_failed_not_raised(tmp_path):
    """Mutation guard: catching only SourceFailedError would kill the search."""
    def run(argv, **kwargs):
        raise RuntimeError("segfault-ish")

    payload = fed.federate("q", kinds="memory", limit=5, root=str(tmp_path),
                           which=lambda _n: "awm", run=run)
    report = payload["sources"][0]
    assert report["status"] == "failed" and "RuntimeError" in report["detail"]
    assert payload["sources_answered"] == []


def test_web_answer_and_citations_survive_into_the_payload(tmp_path):
    web = FakeWeb(answer="the answer", citations=["https://e.x"])
    payload = fed.federate("q", kinds="web", root=str(tmp_path),
                           web_client_factory=lambda: web, which=lambda _n: None,
                           run=never_runs)
    assert payload["answer"] == "the answer"
    assert payload["citations"] == ["https://e.x"]
    assert web.calls[0][1]["auto_learn"] is False


def test_web_results_without_a_snippet_are_dropped_and_counted(tmp_path):
    """A row is supposed to BE an exact passage; one with no snippet is not."""
    web = FakeWeb(results=[
        {"title": "Empty", "url": "https://e.x", "snippet": "", "score": 0.9},
        {"title": "Full", "url": "https://f.x", "snippet": "a passage", "score": 0.8}])
    payload = fed.federate("q", kinds="web", root=str(tmp_path),
                           web_client_factory=lambda: web, which=lambda _n: None,
                           run=never_runs)
    report = payload["sources"][0]
    assert report["count"] == 1 and report["dropped"] == 1
    assert "1 dropped" in report["detail"]
    assert [r["locator"] for r in payload["results"]] == ["https://f.x"]


def test_federate_refuses_an_empty_query_and_a_non_positive_limit(tmp_path):
    for bad in ("  ", "x" * 513):
        with pytest.raises(ValueError):
            fed.federate(bad, kinds="web", root=str(tmp_path))
    with pytest.raises(ValueError):
        fed.federate("q", kinds="web", limit=0, root=str(tmp_path))


def test_repo_root_uses_gits_top_level_and_falls_back_to_none():
    def run(argv, **kwargs):
        return 0, "/repo\n", ""

    assert fed.repo_root(run=run, which=lambda _n: "git") == "/repo"

    def fail(argv, **kwargs):
        return 128, "", "not a repository"

    assert fed.repo_root(run=fail, which=lambda _n: "git") is None


# ── CLI ────────────────────────────────────────────────────────────────────


PAYLOAD = {
    "query": "q", "root": "/repo",
    "sources_requested": ["web"], "sources_answered": [], "sources_unavailable": ["web"],
    "sources_failed": [],
    "sources": [{"kind": "web", "status": "unavailable",
                 "detail": "no service URL", "count": 0}],
    "results": [],
}


def test_cli_find_returns_one_when_no_source_answered(monkeypatch, capsys):
    from awfind.cli import main

    monkeypatch.setattr(fed, "federate", lambda *a, **k: dict(PAYLOAD))
    assert main(["find", "q", "--sources", "web"]) == 1
    out = capsys.readouterr().out
    assert "unavailable" in out and "no service URL" in out


def test_cli_find_returns_zero_and_prints_the_passage_and_locator(monkeypatch, capsys):
    from awfind.cli import main

    answered = dict(PAYLOAD, sources_answered=["web"], sources_unavailable=[],
                    sources=[{"kind": "web", "status": "answered",
                              "detail": "fake (1 hits)", "count": 1,
                              "dropped": 0}],
                    results=[{"rank": 1, "kind": "web", "title": "Hit",
                              "passage": "exact passage", "locator": "https://e.x",
                              "score": 0.9, "score_kind": "service relevance",
                              "merged_score": 1.0, "meta": {}}])
    monkeypatch.setattr(fed, "federate", lambda *a, **k: answered)
    assert main(["find", "q", "--sources", "web"]) == 0
    out = capsys.readouterr().out
    assert "[web] Hit" in out and "https://e.x" in out and "exact passage" in out
    assert "answered" in out


def test_cli_find_prints_the_dropped_count_so_discards_are_visible(monkeypatch, capsys):
    from awfind.cli import main

    answered = dict(PAYLOAD, sources_answered=["code"], sources_unavailable=[],
                    sources=[{"kind": "code", "status": "answered",
                              "detail": "awgraph from /repo (0 verified chunks, "
                                        "2 dropped: not verbatim in the source)",
                              "count": 0, "dropped": 2}],
                    results=[])
    monkeypatch.setattr(fed, "federate", lambda *a, **k: answered)
    assert main(["find", "q", "--sources", "code"]) == 0
    out = capsys.readouterr().out
    assert "0 verified hit(s)" in out and "2 dropped (not verbatim)" in out


def test_cli_find_returns_two_for_an_unknown_source(capsys):
    from awfind.cli import main

    assert main(["find", "q", "--sources", "nope"]) == 2
    err = capsys.readouterr().err
    assert "web, code, memory, history" in err


def test_cli_find_refuses_scope_without_the_memory_source(capsys):
    from awfind.cli import main

    assert main(["find", "q", "--sources", "web", "--scope", "s"]) == 2
    assert "memory" in capsys.readouterr().err
