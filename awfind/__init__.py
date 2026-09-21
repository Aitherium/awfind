"""awfind — a portable client for AitherSearch-shaped web search.

    from awfind import FindClient

    f = FindClient("https://search.example.com", token="...")
    answer = f.quick("what changed in podman 5.4")
    for hit in answer:
        print(hit.score, hit.title, hit.url)

Read `client.py` before adding a request field: this service IGNORES unknown
keys rather than rejecting them, so a wrong field name costs you a wrong answer
instead of an error.
"""

from __future__ import annotations

from awfind.client import (
    MODES,
    QUERY_MAX_CHARS,
    SEARCH_FIELDS,
    Answer,
    FindClient,
    FindError,
    Result,
    search_body,
)
from awfind.config import (
    ConfigError,
    UnresolvedError,
    config_path,
    load_config,
    resolve_ca_bundle,
    resolve_token,
    resolve_url,
    write_config,
)

__version__ = "0.2.0"

__all__ = [
    "FindClient",
    "FindError",
    "Answer",
    "Result",
    "SEARCH_FIELDS",
    "MODES",
    "QUERY_MAX_CHARS",
    "search_body",
    "ConfigError",
    "UnresolvedError",
    "config_path",
    "load_config",
    "resolve_url",
    "resolve_token",
    "resolve_ca_bundle",
    "write_config",
    "__version__",
]
