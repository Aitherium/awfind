"""What a stranger's machine must still be true of awfind.

These overlap the CLI `--self-test` on purpose. The self-test is what ships (it
runs on any install, with no pytest and no network); this is what runs in CI with
the mutation guards that prove each assertion can still fail.
"""
from __future__ import annotations

import pytest
from awfind import (
    MODES,
    QUERY_MAX_CHARS,
    SEARCH_FIELDS,
    Answer,
    FindClient,
    FindError,
    Result,
    search_body,
)
from awfind.cli import main


def test_search_body_is_exactly_the_declared_field_set():
    # THE load-bearing assertion. This service takes extra="ignore", so a field
    # spelled wrong here is DROPPED with a 200 and no error anywhere — there is
    # no 422 to catch it, and the results are simply computed from less than you
    # asked for. Membership is not enough; the exact tuple is.
    assert tuple(search_body("q")) == SEARCH_FIELDS


def test_search_body_defaults_match_the_service_defaults():
    assert search_body("hello") == {
        "query": "hello", "mode": "quick", "provider": None, "limit": 10,
        "include_answer": True, "auto_learn": True, "synthesize": False,
    }


@pytest.mark.parametrize("bad", ["", "   ", "\n\t"])
def test_empty_query_is_refused_with_a_reason(bad):
    with pytest.raises(ValueError):
        search_body(bad)


def test_a_prompt_sized_query_is_refused_and_the_bound_is_inclusive():
    # The service refuses this too; refusing here just spends no round trip
    # learning it. The boundary matters: an exclusive bound would reject a query
    # the service accepts, which is a fabricated failure — worse than the bug.
    search_body("x" * QUERY_MAX_CHARS)
    with pytest.raises(ValueError):
        search_body("x" * (QUERY_MAX_CHARS + 1))


def test_unknown_mode_is_refused_rather_than_silently_ignored():
    # MUTATION GUARD: without this, "sideways" is sent, dropped by extra="ignore",
    # and answered as the service default — a wrong answer with no error.
    with pytest.raises(ValueError):
        search_body("q", mode="sideways")
    for m in MODES:
        assert search_body("q", mode=m)["mode"] == m


def test_query_is_stripped_so_padding_is_not_a_second_cache_key():
    assert search_body("  q  ")["query"] == "q"


def test_absent_answer_is_none_not_empty_string():
    # "" reads as "it answered, emptily" — a different fact from "no answer was
    # produced", and callers branch on it.
    assert Answer({}).answer is None
    assert Answer({"answer": ""}).answer is None
    assert Answer({"answer": "a"}).answer == "a"


@pytest.mark.parametrize("raw", [{}, {"results": None}, {"results": []}])
def test_a_resultless_response_parses_to_zero_results(raw):
    assert len(Answer(raw)) == 0


def test_results_parse_and_iterate_in_order():
    ans = Answer({"results": [{"title": "a", "url": "u1"}, {"title": "b", "url": "u2"}]})
    assert [r.title for r in ans] == ["a", "b"]
    assert ans.results[0].url == "u1"


def test_missing_score_is_zero_not_none_because_callers_sort_on_it():
    assert Result({}).score == 0.0
    assert Result({"score": "0.5"}).score == 0.5


def test_base_url_normalised_and_no_token_means_no_header():
    assert FindClient("https://h/").base_url == "https://h"
    assert FindClient("https://h").token is None


def test_transport_failure_raises_rather_than_returning_no_results():
    # MUTATION GUARD: returning [] here would make a dead backend look exactly
    # like an unpopular query — the silence this package is written against.
    c = FindClient("http://127.0.0.1:9")  # nothing listens on discard
    with pytest.raises(FindError):
        c.search("anything")


def test_self_test_passes_and_is_the_shipped_check():
    assert main(["--self-test"]) == 0


def test_no_subcommand_is_an_error_not_a_silent_success():
    assert main([]) == 2
