"""awfind's own contribution to `awfind doctor`.

The generated `_doctor.py` reports the STACK — which sibling packages are
installed. That is useful and it is not what stops awfind working. What stops
awfind working is that nothing told it where the service is, and the stack
report is silent on that by construction: the generator knows the family from
the registry, and deliberately does not guess what any one brick needs at
runtime.

This is the sanctioned hook for that. It lives in its own module because
`_doctor.py` is regenerated and a check written there is deleted, silently, by
the next regeneration.

`_doctor_local_verdict` is what makes the lines above it more than decoration:
without it a doctor can print "url UNRESOLVED" and still exit 0, which teaches
an operator that the exit code carries no information.
"""

from __future__ import annotations

from awfind.config import ConfigError, UnresolvedError, resolution_report, resolve_url


def _doctor_local() -> list:
    """Display lines: where this machine thinks the service is."""
    try:
        return list(resolution_report())
    except Exception as exc:                        # noqa: BLE001
        # A diagnostic must not become the outage. Reporting the failure of the
        # report is still information; raising here would take the whole doctor
        # down over the one brick it was asked about.
        return [f"config     report raised {type(exc).__name__}: {exc}"]


def _doctor_local_verdict() -> tuple:
    """(problems, unjudged) folded into the doctor's exit code.

    An unresolved URL is a PROBLEM, not an unjudged state: it is a definite,
    measured answer — every rung was checked and none supplied one — and the
    fix is one command away. Reporting it as "could not judge" would be the
    same silence this module exists to break.
    """
    try:
        resolve_url()
    except UnresolvedError:
        return (["awfind has no service URL on this machine. Every rung was "
                 "checked and none supplied one; run `awfind config set --url "
                 "https://<host>:<port>` to end it."], [])
    except ConfigError as exc:
        return ([f"awfind's config is unusable: {exc}"], [])
    except Exception as exc:                        # noqa: BLE001
        return ([], [f"resolving the service URL raised {type(exc).__name__}: {exc}"])
    return ([], [])
