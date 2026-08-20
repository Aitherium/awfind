# awfind

**One search interface over whichever providers you configured.**

```bash
pip install awfind
```

```python
from awfind import FindClient

f = FindClient("https://search.example.com", token="...")
answer = f.quick("what changed in podman 5.4")
print(answer.answer)                 # the synthesized answer, or None
for hit in answer:
    print(hit.score, hit.title, hit.url)
```

```bash
awfind q "what changed in podman 5.4"
awfind deep "cross-model KV cache transfer" --limit 20
awfind providers          # before concluding a query is unpopular
awfind --self-test        # prove the contract, offline
```

The service origin comes from `--url` or `AWFIND_URL`, the token from `--token`
or `AWFIND_TOKEN`. Neither is guessed — a search client that quietly falls back
to some default endpoint is sending your queries somewhere you did not choose.

---

## What this is, and what it is not

It is a **client**. Provider fan-out, ranking, caching and answer synthesis stay
in the service; this is the wire contract, packaged so anything can speak it.

That split is deliberate. The alternative was lifting a 3,900-line service with
33 private imports into a package, which produces something that
`ModuleNotFoundError`s on your machine while reading as authoritative. A broken
package is worse than no package.

| route | what it does |
|---|---|
| `POST /search` | search, with a mode |
| `POST /search/quick` · `/search/deep` | the same body, mode forced |
| `POST /fetch` | retrieve one URL through the service |
| `GET /providers` | which backends are configured |
| `GET /stats` | counters |

---

## The bug this package exists to prevent

The search model takes pydantic's default, `extra="ignore"`.

So a field name you spell wrong is **silently dropped**. Not a 422 — an ordinary
**200**, with results computed from a request that quietly lost half of what you
asked for. `synthesise` instead of `synthesize` costs you the answer and tells
you nothing.

That is the exact opposite of the browse service ([awbrowse](https://github.com/Aitherium/awbrowse)),
which forbids extras and fails loudly. Same platform, opposite failure mode — so
an assumption carried from one to the other is wrong in the direction that hurts.

The only protection is sending exactly the declared field set, so that set is a
constant the self-test asserts against, and every keyword you pass is validated
**here** rather than dropped **there**:

```python
f.search("q", mode="sideways")   # ValueError — never sent, never silently ignored
f.search("")                     # ValueError — not an empty result list
f.search("x" * 900)              # ValueError: looks like a prompt, not a query
```

---

## Two things it refuses to do

**Return `[]` on failure.** A dead backend raises `FindError`. Returned as an
empty list, an outage would be indistinguishable from an unpopular query — and
nobody investigates an unpopular query.

For the same reason the CLI says so out loud when a search genuinely matched
nothing, and points at `awfind providers`: a service with **no provider
configured** returns nothing for everything, cheerfully, forever.

**Send an empty `Authorization` header.** No token means no header at all. An
empty Bearer is rejected differently from an absent one, and the difference sends
you debugging the auth server instead of your config.

---

## `--self-test`

Every install can prove the client still holds its contract, with no service and
no network:

```console
$ awfind --self-test
  PASS  search body is exactly the declared field set, with the service's defaults
  PASS  empty/over-long queries and unknown modes are refused with a reason
  PASS  absent answer is None, missing score is 0.0, results iterate in order
SELF-TEST: awfind ok
```

The first line is the load-bearing one, and it asserts the **exact** field tuple
rather than membership — because with `extra="ignore"` there is no error to catch
a wrong name, and a test that only checks "query is in there" passes on the bug.

---

## The aw family

Standalone tools that share one idea: **replace something you would otherwise
have to _trust_ with something you can _check_.** Each installs on its own, works
offline, and needs no account.

| | instead of trusting | you check |
|---|---|---|
| **awfind** _(you are here)_ | one vendor's idea of the web | results from whichever providers you configured |
| [awbrowse](https://github.com/Aitherium/awbrowse) | that the page said what you were told | the render, the DOM and the requests it made |
| [awnix](https://github.com/Aitherium/awnix) | that the box is what you left it as | an immutable image you built, with atomic rollback |
| [awgit](https://github.com/Aitherium/awgit) | that no one else is editing this file | a lease, refused at commit time if you do not hold it |
| [awgraph](https://github.com/Aitherium/awgraph) | that grep found everything | an AST + tree-sitter call graph an agent can traverse |
| [awrelay](https://github.com/Aitherium/awrelay) | a SaaS in the middle of your agents | findings and alerts over your own transport |
| [awseal](https://github.com/Aitherium/awseal) | that the artifact came from who you think | an Ed25519 seal — the key that verifies is not the key that forges |
| [awshare](https://github.com/Aitherium/awshare) | that the download is intact | content-addressed bundles, verified on fetch |
| [awm](https://github.com/Aitherium/awm) | that memory stayed in its lane | tenant:user:project scopes |
| [awrecover](https://github.com/Aitherium/awrecover) | that the restore worked | a restore that fully lands or does not land at all |

Apache-2.0.
