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

__version__ = "0.1.0"

__all__ = [
    "FindClient",
    "FindError",
    "Answer",
    "Result",
    "SEARCH_FIELDS",
    "MODES",
    "QUERY_MAX_CHARS",
    "search_body",
    "__version__",
]
