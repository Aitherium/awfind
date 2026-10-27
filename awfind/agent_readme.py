"""The operating manual `awfind agent-readme` prints for an LLM.

Kept as a module constant, not a docs page: an agent that has the binary has
the manual, offline, at the version it is actually running.
"""

AGENT_README = """\
# awfind: operating manual for an agent

awfind is a CLI over an AitherSearch-shaped service you host. It searches the
web and model hubs by meaning, extracts clean page contents, finds pages like a
URL, and answers a question with citations. `find` additionally federates the
LOCAL bricks on this machine -- awgraph code search, awm agent memory and git
history -- into the same ranked list. Every verb takes --json for jq.

## Verbs

  awfind q "<query>" [--limit N] [--provider P]     quick search, ranked hits
  awfind deep "<query>" [--limit N] [--provider P]  slower, broader search
  awfind find "<query>" [--sources LIST] [--limit N]
                                                    web + code + memory + history,
                                                    one merged list
  awfind contents <url> [<url> ...] [--max-chars N] clean text, title, headings
  awfind similar <url> [--limit N] [--provider P]   pages like this one
  awfind answer "<question>" [--sources N]          one answer + cited URLs
  awfind providers                                  which backends are live
  awfind accounts list|add|rm|default               named service accounts
  awfind config show                                what this run will dial
  awfind mcp                                        the same verbs over MCP stdio

Global flags go BEFORE the verb: awfind --json --account work q "..."

## Picking a verb

- You need sources to read            -> q, then contents on the best 1-3 URLs.
- You need one fact, with a citation  -> answer. Quote its citations; do not
                                         invent others.
- You have one good page, want more   -> similar <url>.
- You want everything this machine
  knows, not just the web            -> find. Start there when debugging: the
                                         code, the memory and the commit that
                                         introduced a line are different facts.
- Neural (meaning) ranking            -> --provider exa, if `providers` lists
                                         it available.
- Results empty                       -> run `providers` before concluding the
                                         query has no answers.

## find

  awfind find "hookd worker pool"                     all four sources
  awfind find "hookd worker pool" --sources web,code  just those two
  awfind --json find "hookd worker pool" | jq -r '.results[] | "\\(.kind) \\(.locator)"'

Every result is {rank, kind, title, passage, locator, score, score_kind,
merged_score, meta}. The locator opens the thing: path:line for code,
scope:key for memory, sha:path:line for history, URL for web. Scores are
comparable WITHIN a kind; merged_score normalises each kind's best hit to 1.0
so no source's units win by accident.

Match semantics differ, and each is literal: awm matches the query as a
substring of a memory's key or value, and git history reports changed lines
that contain every query term. Pass those sources the term itself, not a
sentence -- web and code are the relevance-ranked sources.

Every local passage is re-read from its source and checked to be a verbatim
substring before it is returned (the file line, the stored memory value, the
blob at sha:path, or sha^:path for a removed line). A row that cannot be
verified is dropped, and its source report carries the dropped count, so
"0 hit(s), 2 dropped" reads differently from a real zero. A web result with no
snippet has no passage and is dropped the same way.

`sources` in the JSON says what happened to EVERY source you asked for:
answered (with its detail and hit count), unavailable (not installed / no index,
with the install line), or failed (it ran and broke). A missing or broken brick
never aborts the search -- but it is always named, so never present a find
result as complete without checking that block.

On a large repository the code source loads awgraph's index and can take about
a minute; pass --sources web,memory,history when speed matters more.

## Contracts you can rely on

- Queries over 512 characters are refused (exit 2). Distill a prompt into a
  query first.
- A failure RAISES (exit 1, message on stderr). An empty result list means the
  search ran and matched nothing; it never stands in for an outage.
- Exit codes: 0 ok, 1 the service failed (or, for find, no source answered),
  2 you asked wrongly or nothing is configured.
- --account NAME picks a named service + bearer from ~/.aither/awfind.json.
  An unknown name is an error, never a silent fall back to the default.
- Tokens are never printed. `accounts list` and `config show` report presence
  and length only.

## jq one-liners

  awfind --json q "podman 6 release notes" | jq -r '.results[].url'
  awfind --json find "hookd pool" | jq -r '.sources[] | select(.status != "answered")'
  awfind --json answer "latest stable podman" | jq -r '.citations[]'
  awfind --json contents https://example.com | jq -r '.[0].content'
  awfind --json similar https://example.com | jq -r '.results[] | "\\(.score) \\(.url)"'
"""
