# Security and privacy

cost-per-task is a command-line tool that runs on the machine of the person measuring. There
is no server behind it, no account, no telemetry and no update check. It has no runtime
dependency: everything it executes is the Python standard library plus its own code, under
6,000 lines in 24 files, which a reviewer can read in an afternoon. The one exception is
opt-in: the `[mcp]` extra installs the MCP SDK and that SDK's own dependencies. Without it,
`cpt mcp` is unavailable and everything else works.

This page says what the tool reads, what it keeps, what leaves the machine, what it does not
protect against, and how each statement can be checked. Facts were verified on 2026-09-20.

## What it reads and what it keeps

| Data | Read | Kept |
|---|---|---|
| Token counts, model, provider, latency, tool names, requested effort level, timestamps | yes | the usage log (`cpt-log.jsonl`) |
| Task and attempt ids, task type, how the task id came about (`task_source`) | yes | the usage log |
| Pass, fail, leak and the note you type | yes | the labels file (`cpt-labels.jsonl`) |
| API keys and every other request header | in memory while a request is forwarded | never written |
| Prompts, completions, tool inputs, file contents | in memory while a request is forwarded, or while a transcript line is parsed | never written |
| Request URLs | the path, to route the request | never in the log; console messages print the path without its query string |
| Claude Code and Cowork session titles, names of files a Cowork session produced | yes, unless `--no-titles` | the labelling sheet (`sessions.csv`), the labelling page (`sessions.html`), or the screen of `cpt sessions label`, which writes neither; never the log |
| Git branch and pull request number of a session | yes, unless `--no-infer` | the sheet; the log only inside a task id you chose to import |

Two points deserve attention before a log is shared outside the team. Tool names include
the names of MCP servers (`mcp__<server>__<tool>`), which can reveal internal systems. A
suggested task id is built from a folder name and a branch name, which can name a project.
Neither is content, both are yours to review: `--no-infer` turns the suggestion off, and
the log is plain JSON Lines that can be read before it is sent anywhere.

The log records when a person's sessions ran and what they cost. A team that collects logs
from several people should treat them as activity data about staff.

## What leaves the machine

Two connections exist in the code, and no other:

1. **The proxy forwards the agent's own requests** to the API the agent was already calling
   (`https://api.anthropic.com` and `https://api.openai.com` by default, or the gateway you
   name). The provider receives what it would have received without the tool, with two
   documented additions that ask for usage figures (`stream_options.include_usage` on
   streamed OpenAI chat calls, `usage.include` for OpenRouter), both switched off by
   `--no-inject-usage`. TLS certificates are verified, as Python does by default. A plain
   `http://` upstream outside the machine triggers a warning, and an upstream URL that
   carries credentials is refused.
2. **`cpt prices refresh` downloads a public price catalogue** from
   `optimtoken.optimnow.io`, with a GET request that carries no data. It runs only when you
   type it, writes nothing without `--write`, and `--from-file` replaces it on a machine
   without internet access. The price tables ship inside the package, so the command is
   never required.

Measuring Claude Code and Cowork sessions (`cpt sessions ...`) makes no connection at all:
the transcripts are read from disk and the results are written to disk.

That holds for the labelling page too. `cpt sessions page` starts no server: it writes one
HTML file, opened from disk. The file refers to nothing outside itself, and its
Content-Security-Policy is `default-src 'none'`, with the tool's own script and style
allowed by SHA-256 hash and nothing else: the browser refuses every request the page could
make and every script that is not that one, so text planted in a session title can
neither run nor send anything. Transcript text enters the page as text, never as markup.
The page keeps nothing in the browser (no cookie, no local storage); saving is a save
dialog or a download of the labelling sheet.

The proxy listens on `127.0.0.1` only, and the command line offers no way to change that.
It does not authenticate its callers: another process on the same machine can send requests
through it with its own API key, and that usage lands in your log. It cannot obtain your key
that way. `cpt mcp` speaks over standard input and output, opens no port, reads only the
files named in a tool call and returns aggregates (costs, rates, counts, model names), not
task or attempt ids.

## Files it writes

| File | Holds | Treat as |
|---|---|---|
| `cpt-log.jsonl` | usage metadata, task ids, tool names | internal; review before sharing |
| `cpt-labels.jsonl` | outcomes and your notes | internal; review before sharing |
| `sessions.csv` | the above per session, plus titles and branch names | private |
| `sessions.html` | the same rows as the sheet, as a page for the browser (`cpt sessions page`) | private; delete it when done |
| `prices/*.json` | public list prices with dates and sources | public |

The first four are listed in the repository's `.gitignore`. Cells of `sessions.csv` that
start with `=`, `+`, `-` or `@` are written behind an apostrophe, so that a session title
cannot run as a formula when the sheet is opened in Excel. Nothing is ever deleted or
rewritten in the log: outcomes go to the labels file.

## What it does not protect against

- A user or process with administrator rights on the machine can read memory and loopback
  traffic. The tool adds nothing to that exposure and removes nothing from it.
- The model provider sees prompts and completions exactly as before. The tool is not a data
  loss prevention control and makes no compliance claim (GxP, HIPAA, GDPR or other).
- `cpt run` executes the command you give it, with your rights and your environment, as your
  shell would. It uses no shell of its own and builds no command from data. The only other
  process the tool starts is `git rev-parse --abbrev-ref HEAD`, when `cpt run` has to infer
  a task id.
- The Claude Code and Cowork transcript format is internal to Anthropic's products. Lines the
  tool cannot read are skipped and counted, never guessed.
- The project is at alpha stage and has one maintainer.

## Settings for a sensitive environment

- Pin the version and check what you install:
  `pip install cost-per-task==X.Y.Z`, with `--require-hashes` if your process asks for it.
  Every release on PyPI (0.4.0, 0.5.0, 0.6.0) carries a provenance attestation that names
  this repository and its release workflow.
- `cpt sessions list --no-titles --no-infer` keeps titles, branch names and suggested task
  ids out of the sheet.
- Keep `cpt prices refresh` for a machine that may reach the internet, or use `--from-file`.
- Keep logs, labels and sheets out of version control (the shipped `.gitignore` does) and
  off shared drives until someone has read them.
- Point the proxy at `https://` upstreams only.

## How the code is built and released

- Changes reach `main` through pull requests. A ruleset on the default branch blocks
  deletion and force pushes and requires six CI checks: the full test suite on Ubuntu and
  Windows, each on Python 3.11, 3.12 and 3.13. Teams that want a second pair of eyes can
  review the diff between two tags, which is small.
- Releases are built by `publish-pypi.yml` from a tag: it tests the tagged commit, builds in
  a job that holds no publishing credential, and publishes through PyPI trusted publishing
  (short-lived OIDC identity, no stored token). Every GitHub Action in both workflows is
  pinned to a commit, and workflow permissions are read-only unless a job states otherwise.
- GitHub secret scanning with push protection and Dependabot security updates are enabled;
  neither had an alert on 2026-09-20. A gitleaks 8.30 scan of the whole history on the
  same day found nothing. Test fixtures use dummy keys (`sk-test-DUMMY-KEY`).
- The privacy rules above are enforced by tests that fail the build when broken, among them
  `test_no_secrets_or_content_in_log`, `test_console_messages_drop_the_query_string`,
  `test_a_malformed_request_path_gets_a_502_and_no_traceback`,
  `test_sheet_cells_never_open_as_formulas`, the page tests in `tests/test_review_page.py`
  (policy, no URL, no storage, no markup injection) and the transcript tests, which plant a secret
  prompt, answer, tool input and title and check that none reaches the log.

## Check it yourself

```
# the labelling page refers to nothing outside itself and allows no connection
python -c "from cost_per_task.review_page import content_security_policy as p; print(p())"

# every place the code can open a connection, listen or start a process
git grep -nE "urlopen|http\.client|http\.server|socket|subprocess|os\.system|eval\(|exec\(" -- src/

# no runtime dependency
git grep -n "^dependencies" -- pyproject.toml

# the tests, which never call a real API
python -m pytest -q

# what a log really contains
python -c "import json; print(sorted({k for l in open('cpt-log.jsonl') for k in json.loads(l)}))"
```

Running `cpt sessions summary` with the network cable unplugged is the shortest proof that
measuring sessions needs no connection.

## Reporting a vulnerability

Use "Report a vulnerability" under the repository's Security tab on GitHub, which opens a
private report, or write to jean@optimnow.io. Give the details and, if you can, a way to
reproduce. Please do not open a public issue for a vulnerability. Fixes ship in a new
release; only the latest release is supported.
