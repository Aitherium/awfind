"""The Exa-parity verbs: contents, similar, answer, accounts, agent-readme."""
from __future__ import annotations

import json

import pytest
from awfind import cli
from awfind.client import FindClient, FindError, page_from_fetch, parse_cited


def test_similar_target_does_not_clobber_the_service_url(monkeypatch, tmp_path):
    """`similar <url>` once overwrote --url, so the client POSTed to the target site."""
    cfg = tmp_path / "awfind.json"
    cfg.write_text(json.dumps({"url": "https://search.invalid"}), encoding="utf-8")
    seen = {}

    def fake_similar(self, url, **kw):
        seen["base"], seen["target"] = self.base_url, url
        raise FindError("stop")

    monkeypatch.setattr(FindClient, "similar", fake_similar)
    for k in ("AWFIND_URL", "AWFIND_ACCOUNT", "AWFIND_BROWSER_URL"):
        monkeypatch.delenv(k, raising=False)
    rc = cli.main(["--config", str(cfg), "similar", "https://target.example/page"])
    assert rc == 1
    assert seen == {"base": "https://search.invalid", "target": "https://target.example/page"}


def test_contents_falls_through_every_rung(monkeypatch):
    c = FindClient("https://search.invalid", browser_url="https://browser.invalid")
    calls = []

    def post(path, body):
        calls.append(path)
        if path == "/extract":
            return {"success": False, "content": "", "error": "403"}
        raise FindError(f"{path}: HTTP 403")

    monkeypatch.setattr(c, "_post", post)
    monkeypatch.setattr(c, "_render", lambda url: {"status": "success", "content": "a b c"})
    page = c.contents("https://site.example")
    assert calls == ["/extract", "/fetch"]
    assert page.raw["extraction_method"].startswith("render")


def test_contents_without_a_browser_names_the_missing_rung(monkeypatch):
    c = FindClient("https://search.invalid")
    monkeypatch.setattr(c, "_post", lambda p, b: (_ for _ in ()).throw(FindError(f"{p}: 403")))
    with pytest.raises(FindError, match="no browser_url configured"):
        c.contents("https://site.example")


def test_answer_raises_on_the_in_band_error():
    with pytest.raises(FindError):
        parse_cited("q", "[WEB_RESEARCH_ERROR: search failed]")


def test_fetch_page_title():
    assert page_from_fetch("u", {"content": "# T\n> d"}).title == "T"


def test_agent_readme_prints(capsys):
    assert cli.main(["agent-readme"]) == 0
    out = capsys.readouterr().out
    for verb in ("contents", "similar", "answer", "--account"):
        assert verb in out


def test_accounts_cli_roundtrip(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AWFIND_ACCOUNT", raising=False)
    cfg = str(tmp_path / "awfind.json")
    assert cli.main(["--config", cfg, "accounts", "add", "work",
                     "--url", "https://work.invalid", "--token", "s3cret"]) == 0
    assert cli.main(["--config", cfg, "accounts", "list"]) == 0
    out = capsys.readouterr().out
    assert "work" in out and "s3cret" not in out
    assert cli.main(["--config", cfg, "--account", "nope", "q", "x"]) == 2
    assert cli.main(["--config", cfg, "accounts", "rm", "work"]) == 0
