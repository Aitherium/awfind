# awfind for agents

Read this if you are an agent (or a human) editing this package. Short on
purpose: the commands, the traps that cost a session, and where the rest lives.
Nothing here is read at runtime — it is for you.

## What this is

PyPI distribution **`awfind`** (version in `pyproject.toml`), import package
`awfind`, Python >= 3.10. A portable search client — query, results, ranking —
over pluggable providers.

This repository is a **synced mirror** of the AitherOS monorepo (lane
`.github/workflows/sync-awfind.yml`). Hand edits made here are overwritten on
the next sync — change the source and let the lane publish.

## Build, test, verify

```bash
python -m pytest tests -q        # the suite: 63 tests, green at v0.3.0
pip install -e .                 # editable install for developing against it
```

The suite was run from a source checkout with no prior install. The publish
lane (`publish-brick.yml`) additionally builds the wheel, installs it and
imports it — a tree that tests green can still ship a broken wheel.

## Rules that keep this useful

- **A provider's config is data, and the linter's config must agree with it.**
  `tests/test_lint_config_agrees.py` exists because two lists that claim the
  same thing is the defect shape this family keeps paying for. A config edit
  lands with that test still green.
- **Parity is tested, not asserted.** `tests/test_exa_parity.py` pins
  behaviour against the provider it mirrors; a ranking change that breaks
  parity fails there rather than in somebody's search results.
- **The registry drives the public surface.** This repo's README header,
  `llms.txt` and `aither-manifest.json` are generated from the ecosystem
  registry (one yaml in the AitherOS monorepo) and rewritten on every sync.
  Change the registry; do not hand-edit the generated blocks.
- **The install line is a measured claim.** `check_ecosystem_install_lines`
  asserts the advertised `pip install` channel is real and ours. A rename or
  a move lands with the registry entry in the same change.

## Read next

- `llms.txt` — the install/use card written for an agent to execute
- `README.md` — the human front door
- `docs/` — the generated docs site source
