"""`awfind mcp` must actually answer, ranked, and refuse loudly.

The ranking and failure contract is tested with a fake client (no service); the
server surface is driven through its OWN list_tools()/call_tool(), the path a
client takes, and skips only when the `mcp` extra is absent.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time

import pytest
from awfind import mcp_server
from awfind.client import Answer, FindError


class FakeClient:
    def __init__(self, by_provider=None, fail=()):
        self.by_provider = by_provider or {}
        self.fail = set(fail)
        self.calls = []

    def _go(self, mode, query, **kw):
        self.calls.append((mode, query, kw))
        p = kw.get("provider")
        if p in self.fail:
            raise FindError(f"/search/{mode}: HTTP 503: down")
        return Answer({"query": query, "provider": p or "auto",
                       "results": self.by_provider.get(p, [])})

    def quick(self, query, **kw):
        return self._go("quick", query, **kw)

    def deep(self, query, **kw):
        return self._go("deep", query, **kw)

    def providers(self):
        return {"providers": [{"name": "duckduckgo", "available": True}]}


def hit(url, score, source="duckduckgo", title="t"):
    return {"title": title, "url": url, "snippet": "s", "source": source, "score": score}


def test_results_are_ranked_by_score_and_carry_their_url():
    c = FakeClient({None: [hit("https://a", 0.2), hit("https://b", 0.9), hit("https://c", 0.5)]})
    out = json.loads(mcp_server.run_find("q", limit=10, client=c))
    assert [r["url"] for r in out["results"]] == ["https://b", "https://c", "https://a"]
    assert [r["rank"] for r in out["results"]] == [1, 2, 3]
    assert all(r["source"] for r in out["results"])


def test_multiple_sources_merge_dedupe_and_name_both_providers():
    c = FakeClient({
        "duckduckgo": [hit("https://x", 0.4), hit("https://y", 0.3)],
        "huggingface": [hit("https://x", 0.8, source="huggingface")],
    })
    out = json.loads(mcp_server.run_find("q", sources=["duckduckgo", "huggingface"], client=c))
    assert [r["url"] for r in out["results"]] == ["https://x", "https://y"]
    assert out["results"][0]["score"] == 0.8
    assert out["results"][0]["source"] == "duckduckgo+huggingface"
    assert [k["provider"] for _, _, k in c.calls] == ["duckduckgo", "huggingface"]


def test_limit_is_applied_after_merge_and_capped():
    c = FakeClient({None: [hit(f"https://{i}", i / 100) for i in range(40)]})
    out = json.loads(mcp_server.run_find("q", limit=500, client=c))
    assert len(out["results"]) == mcp_server.MAX_LIMIT
    assert out["results"][0]["url"] == "https://39"


def test_search_is_read_only_auto_learn_off():
    c = FakeClient({None: [hit("https://a", 0.5)]})
    mcp_server.run_find("q", client=c)
    assert c.calls[0][2]["auto_learn"] is False


def test_a_dead_backend_is_a_failure_not_an_empty_list():
    c = FakeClient(fail={None})
    out = mcp_server.run_find("q", client=c)
    assert out.startswith("search FAILED")


def test_partial_failure_is_reported_alongside_results():
    c = FakeClient({"duckduckgo": [hit("https://a", 0.5)]}, fail={"brave"})
    out = json.loads(mcp_server.run_find("q", sources=["duckduckgo", "brave"], client=c))
    assert len(out["results"]) == 1
    assert out["failed_sources"] and "brave" in out["failed_sources"][0]


def test_empty_results_say_which_kind_of_empty():
    out = json.loads(mcp_server.run_find("q", client=FakeClient()))
    assert out["results"] == [] and "find_providers" in out["note"]


@pytest.mark.parametrize("query,mode", [("", "quick"), ("x" * 513, "quick"), ("q", "sideways")])
def test_bad_input_is_refused_before_dialling(query, mode):
    c = FakeClient()
    assert mcp_server.run_find(query, mode=mode, client=c).startswith("Refused")
    assert c.calls == []


def test_deep_mode_uses_the_deep_route():
    c = FakeClient({None: [hit("https://a", 0.5)]})
    mcp_server.run_find("q", mode="deep", client=c)
    assert c.calls[0][0] == "deep"


def test_unconfigured_machine_is_reported(monkeypatch, tmp_path):
    monkeypatch.delenv("AWFIND_URL", raising=False)
    monkeypatch.setenv("AWFIND_CONFIG", str(tmp_path / "none.json"))
    out = mcp_server.run_find("q")
    assert out.startswith("awfind is not configured") and "AWFIND_URL" in out


# -- the server surface ------------------------------------------------------

mcp = pytest.importorskip("mcp", reason="awfind[mcp] not installed")


def _text(result) -> str:
    if isinstance(result, tuple):  # 1.x FastMCP returns (content, structured)
        result = result[0]
    content = getattr(result, "content", result)
    return "\n".join(getattr(c, "text", "") or "" for c in content)


def test_server_declares_find_and_find_providers():
    server = mcp_server.build_server()
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == {"find", "find_providers"}
    desc = tools["find"].description
    assert "web_search" in desc and "PREFER" in desc
    assert {"query", "limit", "sources"} <= set((getattr(tools["find"], "input_schema", None) or tools["find"].inputSchema)["properties"])


def test_server_find_routes_through_run_find(monkeypatch):
    fake = FakeClient({None: [hit("https://b", 0.9), hit("https://a", 0.1)]})
    monkeypatch.setattr(mcp_server, "_client", lambda: fake)
    server = mcp_server.build_server()
    out = json.loads(_text(asyncio.run(server.call_tool("find", {"query": "q", "limit": 2}))))
    assert [r["url"] for r in out["results"]] == ["https://b", "https://a"]


def test_cli_mcp_starts_fast_and_lists_tools_over_stdio():
    """Spawn `python -m awfind.cli mcp` and speak JSON-RPC to it: the real path."""
    proc = subprocess.Popen([sys.executable, "-m", "awfind.cli", "mcp"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    t0 = time.monotonic()
    try:
        def send(msg):
            proc.stdin.write((json.dumps(msg) + "\n").encode())
            proc.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "0"}}})
        init = json.loads(proc.stdout.readline())
        assert init["result"]["serverInfo"]["name"] == "awfind"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = json.loads(proc.stdout.readline())
        elapsed = time.monotonic() - t0
        assert {t["name"] for t in listed["result"]["tools"]} == {"find", "find_providers"}
        assert elapsed < 10, f"server took {elapsed:.1f}s to list tools"
    finally:
        proc.kill()
        proc.wait()
