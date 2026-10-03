"""The operating manual `awfind agent-readme` prints for an LLM.

Kept as a module constant, not a docs page: an agent that has the binary has
the manual, offline, at the version it is actually running.
"""

AGENT_README = """\
# awfind: operating manual for an agent

awfind is a CLI over an AitherSearch-shaped service you host. It searches the
web and model hubs by meaning, extracts clean page contents, finds pages like a
URL, and answers a question with citations. Every verb takes --json for jq.

## Verbs

  awfind q "<query>" [--limit N] [--provider P]     quick search, ranked hits
  awfind deep "<query>" [--limit N] [--provider P]  slower, broader search
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
- Neural (meaning) ranking            -> --provider exa, if `providers` lists
                                         it available.
- Results empty                       -> run `providers` before concluding the
                                         query has no answers.

## Contracts you can rely on

- Queries over 512 characters are refused (exit 2). Distill a prompt into a
  query first.
- A failure RAISES (exit 1, message on stderr). An empty result list means the
  search ran and matched nothing; it never stands in for an outage.
- Exit codes: 0 ok, 1 the service failed, 2 you asked wrongly or nothing is
  configured.
- --account NAME picks a named service + bearer from ~/.aither/awfind.json.
  An unknown name is an error, never a silent fall back to the default.
- Tokens are never printed. `accounts list` and `config show` report presence
  and length only.

## jq one-liners

  awfind --json q "podman 6 release notes" | jq -r '.results[].url'
  awfind --json answer "latest stable podman" | jq -r '.citations[]'
  awfind --json contents https://example.com | jq -r '.[0].content'
  awfind --json similar https://example.com | jq -r '.results[] | "\\(.score) \\(.url)"'
"""
