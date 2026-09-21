"""Where awfind finds the service, and how it trusts that service's TLS.

WHY THIS MODULE EXISTS
======================
Until this existed, `awfind stats` on a machine that had a search service
running one loopback port away printed::

    no service URL: pass --url or set AWFIND_URL

...and exited 2. Both halves of that sentence are true and neither is
actionable: it does not say what was looked at, it does not say where to put
the answer so it stops being asked, and it offers no place to put it other
than an environment variable that dies with the shell. Measured on a host with
a healthy service: the client had never been used once.

So the fix is not a default URL. A search client that silently falls back to
some endpoint sends your queries somewhere you did not choose, and a client
that ships with its author's address baked in is useless to everyone else and
a leak to its author. The fix is a RESOLUTION ORDER that ends in a file you
own, and an error that names every step it tried.

THE ORDER
=========
For both the URL and the token, highest first:

1. the command-line flag (``--url`` / ``--token``)
2. the environment (``AWFIND_URL`` / ``AWFIND_TOKEN``)
3. the config file — ``~/.aither/awfind.json`` by default, or ``AWFIND_CONFIG``
4. no default. An error that lists 1-3 and says how to fix it.

For the CA bundle the same shape, one rung shorter: ``SSL_CERT_FILE``, then
``REQUESTS_CA_BUNDLE``, then the config file's ``ca_bundle``, then the
library's own trust store. Those two variable names are not invented here —
they are what OpenSSL and requests already read, and a service on a private CA
is the normal case for this client, not the exotic one.

A BAD VALUE IS REPORTED, NEVER SWALLOWED
========================================
A config file that is missing is a fact; a config file that exists and is
unreadable, is not JSON, is not an object, or holds a URL with no scheme is a
BUG, and this module raises for it. The tempting shape — ``except Exception:
return {}`` — turns a typo into the same message as no config at all and sends
the reader to fix something that was never broken.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional, Tuple

__all__ = [
    "ConfigError",
    "UnresolvedError",
    "CONFIG_ENV",
    "URL_ENV",
    "TOKEN_ENV",
    "CA_ENVS",
    "config_path",
    "load_config",
    "resolve_url",
    "resolve_token",
    "resolve_ca_bundle",
    "write_config",
    "resolution_report",
]

#: The environment variable that MOVES the config file. Named so a test, a
#: container and a second account on one machine can each have their own
#: without touching the real one.
CONFIG_ENV = "AWFIND_CONFIG"
URL_ENV = "AWFIND_URL"
TOKEN_ENV = "AWFIND_TOKEN"

#: Read in this order. Both are pre-existing conventions: OpenSSL reads the
#: first, requests the second. Inventing a third name would mean a machine
#: already configured for a private CA still failed here for no reason.
CA_ENVS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")

#: Keys this client understands in the config file. Unknown keys are LEFT
#: ALONE on a write and ignored on a read: the file belongs to the user, and a
#: client that pruned what it did not recognise would delete the next version's
#: settings every time an older one ran.
KNOWN_KEYS = ("url", "token", "ca_bundle")


class ConfigError(RuntimeError):
    """The config file exists and is wrong.

    Distinct from :class:`UnresolvedError`, which means nothing was configured
    anywhere. "You have no config" and "your config is broken" are different
    problems with different fixes, and one message for both sends half the
    readers to the wrong place.
    """


class UnresolvedError(RuntimeError):
    """Nothing supplied the value, at any rung. Carries the whole trail."""


def config_path(explicit: Optional[str] = None) -> Path:
    """The config file this run would read. Never touches the disk."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env).expanduser()
    return Path.home() / ".aither" / "awfind.json"


def _check_url(value: Any, where: str) -> str:
    """A URL this client can actually dial, or an error naming where it came from.

    Checked at the SOURCE rather than at the socket: `httpx` given "search:8114"
    raises about a missing protocol from somewhere deep in a request, which
    reads as a transport failure rather than as a typo in a file.
    """
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: url must be a non-empty string, got {value!r}")
    url = value.strip()
    if not url.startswith(("http://", "https://")):
        raise ConfigError(
            f"{where}: url must start with http:// or https:// — got {url!r}. "
            "These services usually serve TLS, and plain http into a TLS "
            "listener closes the socket rather than answering, which reads as "
            "the service being down while it is perfectly healthy."
        )
    rest = url.split("://", 1)[1]
    if not rest or rest.startswith("/"):
        raise ConfigError(f"{where}: url has no host — got {url!r}")
    return url.rstrip("/")


def load_config(path: Optional[str] = None) -> dict:
    """The config file's contents, or ``{}`` when there is no file.

    Raises :class:`ConfigError` for a file that exists and cannot be used. That
    is the whole point of the function: an absent file and a broken file must
    not produce the same answer.
    """
    p = config_path(path)
    if not p.exists():
        return {}
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{p}: cannot read config: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"{p}: config is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: config must be a JSON object, got {type(data).__name__}")
    if "url" in data:
        _check_url(data["url"], str(p))
    return data


def resolve_url(flag: Optional[str] = None, *, path: Optional[str] = None,
                config: Optional[dict] = None) -> Tuple[str, str]:
    """(url, where it came from). Raises :class:`UnresolvedError` naming every rung."""
    tried: list[str] = []
    if flag:
        return _check_url(flag, "--url"), "--url"
    tried.append("--url (not given)")

    env = os.environ.get(URL_ENV)
    if env:
        return _check_url(env, URL_ENV), URL_ENV
    tried.append(f"{URL_ENV} (not set)")

    cfg = load_config(path) if config is None else config
    p = config_path(path)
    if cfg.get("url"):
        return _check_url(cfg["url"], str(p)), str(p)
    tried.append(f"{p} ({'no url key' if cfg else 'no file'})")

    raise UnresolvedError(
        "no service URL. Tried, in order:\n"
        + "".join(f"  {i}. {t}\n" for i, t in enumerate(tried, 1))
        + "Fix it once, for every future run:\n"
        + f"  awfind config set --url https://<host>:<port>   (writes {p})\n"
        + "or for this shell only:\n"
        + f"  export {URL_ENV}=https://<host>:<port>"
    )


def resolve_token(flag: Optional[str] = None, *, path: Optional[str] = None,
                  config: Optional[dict] = None) -> Tuple[Optional[str], str]:
    """(token, where it came from). ``(None, "unauthenticated")`` is a legal answer.

    Unlike the URL, an absent token is not an error: plenty of these services
    are open on a trusted network, and refusing to run without one would make
    the client unusable exactly where it is easiest to use.
    """
    if flag:
        return flag, "--token"
    env = os.environ.get(TOKEN_ENV)
    if env:
        return env, TOKEN_ENV
    cfg = load_config(path) if config is None else config
    tok = cfg.get("token")
    if tok:
        if not isinstance(tok, str):
            raise ConfigError(f"{config_path(path)}: token must be a string")
        return tok, str(config_path(path))
    return None, "unauthenticated"


def resolve_ca_bundle(flag: Optional[str] = None, *, path: Optional[str] = None,
                      config: Optional[dict] = None) -> Tuple[Any, str]:
    """(verify value for httpx, where it came from).

    ``True`` means the library's own trust store. A path means a private CA —
    the normal case for a service on an internal CA, and the reason this is
    read from the environment at all rather than left to the caller.

    Never returns ``False``. Turning verification off makes an expired or
    swapped certificate indistinguishable from a good one, and a search client
    is exactly the thing an attacker would want to answer for.
    """
    if flag:
        return flag, "--ca-bundle"
    for name in CA_ENVS:
        val = os.environ.get(name)
        if val:
            return val, name
    cfg = load_config(path) if config is None else config
    bundle = cfg.get("ca_bundle")
    if bundle:
        if not isinstance(bundle, str):
            raise ConfigError(f"{config_path(path)}: ca_bundle must be a path string")
        return bundle, str(config_path(path))
    return True, "default trust store"


def write_config(url: Optional[str] = None, token: Optional[str] = None, *,
                 ca_bundle: Optional[str] = None, path: Optional[str] = None) -> Path:
    """Merge these settings into the config file and return where it landed.

    A MERGE, not a rewrite: writing only what was passed means setting a URL
    does not silently drop a token set last week. Unknown keys survive too —
    see :data:`KNOWN_KEYS`.
    """
    p = config_path(path)
    data = load_config(path)
    if url is not None:
        data["url"] = _check_url(url, "--url")
    if token is not None:
        data["token"] = token
    if ca_bundle is not None:
        data["ca_bundle"] = ca_bundle
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        # The file may hold a bearer. Best-effort on platforms where it means
        # something; never fatal, because a config that could not be chmod'd is
        # still a config, and failing here would leave it written but unreported.
        p.chmod(0o600)
    except OSError:
        return p
    return p


def resolution_report(flag_url: Optional[str] = None, flag_token: Optional[str] = None, *,
                      path: Optional[str] = None) -> list:
    """Display lines describing what each rung would supply, for `doctor`.

    Tokens are reported as present/absent with a length, never echoed. A
    diagnostic that prints the credential it is diagnosing is a leak with a
    helpful tone.
    """
    lines: list = []
    p = config_path(path)
    lines.append(f"config     {p} {'exists' if p.exists() else 'ABSENT'}")
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        lines.append(f"config     UNUSABLE: {exc}")
        return lines
    try:
        url, where = resolve_url(flag_url, path=path, config=cfg)
        lines.append(f"url        {url}  (from {where})")
    except (UnresolvedError, ConfigError) as exc:
        lines.append(f"url        UNRESOLVED: {str(exc).splitlines()[0]}")
    try:
        tok, twhere = resolve_token(flag_token, path=path, config=cfg)
        shown = f"set, {len(tok)} chars" if tok else "none"
        lines.append(f"token      {shown}  (from {twhere})")
    except ConfigError as exc:
        lines.append(f"token      UNUSABLE: {exc}")
    try:
        bundle, bwhere = resolve_ca_bundle(path=path, config=cfg)
        shown = "library default" if bundle is True else str(bundle)
        lines.append(f"ca bundle  {shown}  (from {bwhere})")
    except ConfigError as exc:
        lines.append(f"ca bundle  UNUSABLE: {exc}")
    return lines
