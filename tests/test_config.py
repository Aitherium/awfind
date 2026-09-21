"""How awfind finds the service — the rung order, and what it says when it cannot.

These overlap the CLI `--self-test` on purpose, the same way `test_client.py`
does: the self-test is what ships and runs on any install with no pytest, this
is what runs in CI with the mutation guards that prove each assertion can still
fail.

The one assertion with no counterpart in the self-test is the LAST one, which
reads the package's own source: a default endpoint cannot be caught by any
behavioural test, because a client with one baked in behaves perfectly — on its
author's machine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from awfind.cli import main
from awfind.config import (
    CA_ENVS,
    ConfigError,
    UnresolvedError,
    config_path,
    load_config,
    resolve_ca_bundle,
    resolve_token,
    resolve_url,
    write_config,
)

_ENVS = ("AWFIND_URL", "AWFIND_TOKEN", "AWFIND_CONFIG") + CA_ENVS


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No test may inherit the developer's own configuration.

    Without this the suite passes or fails depending on whether the machine
    running it happens to export AWFIND_URL — which is precisely the class of
    bug this module exists to make visible.
    """
    for name in _ENVS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def cfg(tmp_path) -> str:
    return str(tmp_path / "awfind.json")


# ── the order ──────────────────────────────────────────────────────────────


def test_flag_beats_env_beats_config_beats_error(cfg, monkeypatch):
    # 4. nothing: an error, not a guess.
    with pytest.raises(UnresolvedError):
        resolve_url(None, path=cfg)

    # 3. the config file.
    write_config("https://in-file.invalid", path=cfg)
    assert resolve_url(None, path=cfg) == ("https://in-file.invalid", cfg)

    # 2. the environment outranks it.
    monkeypatch.setenv("AWFIND_URL", "https://in-env.invalid")
    assert resolve_url(None, path=cfg) == ("https://in-env.invalid", "AWFIND_URL")

    # 1. the flag outranks that.
    assert resolve_url("https://on-flag.invalid", path=cfg)[0] == "https://on-flag.invalid"


def test_token_follows_the_same_order_and_absent_is_legal(cfg, monkeypatch):
    assert resolve_token(None, path=cfg) == (None, "unauthenticated")
    write_config("https://h.invalid", "tok-file", path=cfg)
    assert resolve_token(None, path=cfg)[0] == "tok-file"
    monkeypatch.setenv("AWFIND_TOKEN", "tok-env")
    assert resolve_token(None, path=cfg)[0] == "tok-env"
    assert resolve_token("tok-flag", path=cfg)[0] == "tok-flag"


def test_the_config_path_moves_with_its_own_variable(tmp_path, monkeypatch):
    moved = tmp_path / "elsewhere.json"
    monkeypatch.setenv("AWFIND_CONFIG", str(moved))
    assert config_path() == moved
    # And the default is under the user's home, not the working directory — a
    # config that lived next to the CWD would silently change with `cd`.
    monkeypatch.delenv("AWFIND_CONFIG")
    assert config_path().parent.parent == Path.home()


# ── the error names every step ─────────────────────────────────────────────


def test_the_unresolved_error_names_every_rung_it_tried(cfg):
    with pytest.raises(UnresolvedError) as exc:
        resolve_url(None, path=cfg)
    msg = str(exc.value)
    # Each rung by the name the reader would have to type. The old message
    # named two of three and never mentioned a file at all, so the durable fix
    # was undiscoverable from the failure.
    for needle in ("--url", "AWFIND_URL", cfg, "awfind config set"):
        assert needle in msg, f"the error never mentions {needle!r}"


def test_the_cli_prints_that_trail_and_exits_2(cfg, capsys):
    code = main(["--config", cfg, "stats"])
    assert code == 2
    err = capsys.readouterr().err
    assert "AWFIND_URL" in err and cfg in err and "awfind config set" in err


# ── a bad config is reported, never swallowed ──────────────────────────────


@pytest.mark.parametrize(
    "body, why",
    [
        ("{not json", "unparseable"),
        ('["a"]', "a JSON array rather than an object"),
        ('{"url": "search.example.com:443"}', "a url with no scheme"),
        ('{"url": "https://"}', "a url with no host"),
        ('{"url": 7}', "a url that is not a string"),
    ],
)
def test_a_broken_config_raises_rather_than_reading_as_absent(cfg, body, why):
    Path(cfg).write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError):
        resolve_url(None, path=cfg)
    assert True, why


def test_a_broken_config_is_not_silently_the_same_as_no_config(cfg, capsys):
    """The distinction the CLI must preserve, end to end.

    Both exit 2; the MESSAGES must differ. Collapsed into one, a typo in the
    file sends the reader to write the config they already wrote.
    """
    missing = main(["--config", cfg, "stats"])
    absent_msg = capsys.readouterr().err
    Path(cfg).write_text("{not json", encoding="utf-8")
    broken = main(["--config", cfg, "stats"])
    broken_msg = capsys.readouterr().err
    assert missing == broken == 2
    assert absent_msg != broken_msg
    assert "not valid JSON" in broken_msg


def test_an_unreadable_config_is_an_error_not_an_empty_dict(cfg, monkeypatch):
    Path(cfg).write_text("{}", encoding="utf-8")

    def _boom(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", _boom)
    with pytest.raises(ConfigError):
        load_config(cfg)


# ── TLS ────────────────────────────────────────────────────────────────────


def test_the_tls_bundle_env_is_honoured_in_order(cfg, monkeypatch):
    assert resolve_ca_bundle(path=cfg) == (True, "default trust store")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/second.pem")
    assert resolve_ca_bundle(path=cfg) == ("/second.pem", "REQUESTS_CA_BUNDLE")
    monkeypatch.setenv("SSL_CERT_FILE", "/first.pem")
    assert resolve_ca_bundle(path=cfg) == ("/first.pem", "SSL_CERT_FILE")
    # And the config file is the rung below both.
    monkeypatch.delenv("SSL_CERT_FILE")
    monkeypatch.delenv("REQUESTS_CA_BUNDLE")
    write_config("https://h.invalid", ca_bundle="/from-file.pem", path=cfg)
    assert resolve_ca_bundle(path=cfg) == ("/from-file.pem", cfg)


def test_the_bundle_reaches_the_ssl_context_builder(cfg, monkeypatch):
    """Spy on what the transport is actually handed.

    Resolving the right path and then not passing it is a silent failure: the
    request still succeeds against a public CA and fails against the private
    one for a reason that never names the bundle.
    """
    seen = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def get(self, path):
            raise AssertionError("the spy must not reach the network")

    monkeypatch.setenv("SSL_CERT_FILE", "/private-ca.pem")
    write_config("https://h.invalid", path=cfg)
    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    main(["--config", cfg, "stats"])
    assert seen.get("verify") == "/private-ca.pem"


def test_tls_never_resolves_to_unverified(cfg, monkeypatch):
    """No rung may produce False. An unverified search client is worse than none."""
    for name in CA_ENVS:
        monkeypatch.setenv(name, "/x.pem")
        assert resolve_ca_bundle(path=cfg)[0] is not False
        monkeypatch.delenv(name)
    Path(cfg).write_text(json.dumps({"ca_bundle": ""}), encoding="utf-8")
    assert resolve_ca_bundle(path=cfg)[0] is True


def test_a_non_string_ca_bundle_is_refused(cfg):
    Path(cfg).write_text(json.dumps({"ca_bundle": False}), encoding="utf-8")
    # False is the value someone reaches for to "turn verification off". It is
    # not a path, and accepting it here would smuggle verify=False in through
    # a config file.
    assert resolve_ca_bundle(path=cfg)[0] is True


# ── writing it ─────────────────────────────────────────────────────────────


def test_write_config_merges_rather_than_replaces(cfg):
    write_config("https://one.invalid", "tok", path=cfg)
    write_config("https://two.invalid", path=cfg)
    data = json.loads(Path(cfg).read_text(encoding="utf-8"))
    assert data["url"] == "https://two.invalid"
    assert data["token"] == "tok", "setting the url dropped the token"


def test_write_config_keeps_keys_it_does_not_know(cfg):
    Path(cfg).write_text(json.dumps({"future_option": 1}), encoding="utf-8")
    write_config("https://one.invalid", path=cfg)
    assert json.loads(Path(cfg).read_text(encoding="utf-8"))["future_option"] == 1


def test_write_config_refuses_a_url_it_could_not_dial(cfg):
    with pytest.raises(ConfigError):
        write_config("search.example.com", path=cfg)
    assert not Path(cfg).exists(), "a refused write still created the file"


def test_the_cli_config_commands_round_trip(cfg, capsys):
    assert main(["--config", cfg, "config", "set", "--url", "https://h.invalid"]) == 0
    assert "wrote" in capsys.readouterr().out
    assert main(["--config", cfg, "config", "path"]) == 0
    assert capsys.readouterr().out.strip() == cfg
    assert main(["--config", cfg, "config", "show"]) == 0
    assert "https://h.invalid" in capsys.readouterr().out
    # Nothing to set is a refusal, not a no-op success.
    assert main(["--config", cfg, "config", "set"]) == 2


def test_config_show_never_echoes_the_token(cfg, capsys):
    write_config("https://h.invalid", "sup3r-secret-value", path=cfg)
    main(["--config", cfg, "config", "show"])
    out = capsys.readouterr().out
    assert "sup3r-secret-value" not in out
    assert "18 chars" in out


# ── no endpoint is baked in ────────────────────────────────────────────────


def test_no_default_url_is_baked_into_the_package():
    """A client that ships its author's address is useless to everyone else.

    Behaviour cannot catch this — a baked default works perfectly on the one
    machine it was written on — so the assertion reads the source. Scoped to
    URL-shaped occurrences: the words themselves are legitimate in prose about
    what NOT to do, and a rule that flooded on those would be switched off.
    """
    import awfind

    pkg = Path(awfind.__file__).parent
    offenders = []
    for py in sorted(pkg.glob("*.py")):
        for n, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            for needle in ("localhost", "127.0.0.1", "aitheros-"):
                if needle in line and ("://" in line or "url" in line.lower()):
                    offenders.append(f"{py.name}:{n}: {line.strip()[:90]}")
    assert not offenders, "an internal address appears as a URL in the package:\n" + \
        "\n".join(offenders)
