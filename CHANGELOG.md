# Changelog

All notable changes to cost-per-task. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow semantic
versioning, with the minor digit bumped for any user-visible feature.

## [Unreleased]

### Added
- Claude Opus 5.5 in `prices/anthropic.json` (input 4, cache read 0.20 at a documented
  special 0.05x, cache writes 5 and 8, output 20 USD per million tokens), plus Claude
  Sonnet 4, from `cpt prices refresh` on 2026-09-26 and checked against the
  platform.claude.com pricing page the same day. The earlier snapshots stay under
  `history`; fast mode on Opus 5.5 (8 / 40) remains a documented limitation.

### Fixed
- A model id the table lacks was priced at the longest shorter id it started with:
  `claude-opus-5-5` took the Opus 5 rates with no warning, 25% too high on input and
  output and 2.5x on cache reads. The fallback now applies only to dated snapshots of a
  table entry (`claude-sonnet-5-20250929`, `gpt-5.5-2026-04-01`); any other unknown id
  is reported as unpriced, as it should have been.

## [0.7.0] - 2026-09-20

Answers the feedback of the measurement framework's author (task labelling, dated prices,
log scrub), adds a security and privacy statement for users in regulated settings, and makes
labelling a weekly habit: in the terminal, in a spreadsheet or in a browser page.

### Added
- `cpt sessions label`: label sessions one by one in the terminal, with no spreadsheet. Each
  session shows its project, start, duration, calls, cost, suggested task, branch and title;
  one letter answers (pass, fail, leak, skip, rename the task, same task as the previous one,
  task type, note, quit) and each answer is saved at once through the same code as the sheet
  import. Titles are shown on screen only.
- `cpt sessions page`: the labelling sheet as one self-contained HTML page for the browser,
  with a drop-down list per outcome, filters (search, status, project, product, outcome),
  sorting, completion of task names and types, and bulk changes on the ticked rows. It saves
  the same `sessions.csv` that `cpt sessions import` reads, and starts from the entries an
  existing sheet holds. No server and no dependency. The page loads nothing and can connect
  to nothing (Content-Security-Policy `default-src 'none'`, script and style allowed by
  hash only), stores nothing in the browser, puts transcript text in the page as text only,
  and gives exported cells the same formula guard as the sheet.
- `--new` on `cpt sessions list`, `cpt sessions page` and `cpt sessions label`: only the
  sessions since last time (not in the log, and still active after the last session that is).
  Every run says how many there are. `--min-calls N` leaves tiny sessions out.
- A table of tasks in `cpt report`, on its own as `cpt tasks`, and under `tasks` in the JSON
  output: attempts, passes, fails, leaks, open attempts, cost and cost to the first pass,
  most expensive task first. The MCP tools still return aggregates only.
- The labelling sheet gains `status` (new, open, labelled, imported, gone) and `minutes`.
- Task ids inferred from the environment, so labelling is one column instead of two.
  `cpt sessions list` pre-fills `task` from the issue number in the branch name, else the pull
  request, else the branch, else the session on its own (project, day and the start of its
  id, so two sessions are never merged on a hunch); the new `branch` and `task_source`
  columns show the signal. `--no-infer` keeps the old empty column. Session titles are never used.
- `cpt run` no longer needs `--task-id` inside a git repository: the task comes from the
  branch. `--label-from-exit` labels the attempt pass or fail from the command's exit code,
  for commands that end with their own check.
- The log records `task_source` for inferred task ids, and the disclosure checklist gains a
  `task identity` line: tasks stated by hand, tasks inferred, by signal. Outcomes are never
  inferred.
- Dated price snapshots. `cpt prices refresh --write` keeps the previous table under `history`
  in the same file instead of overwriting it, and every command prices each call with the
  snapshot in force on the day the call ran. A price change no longer moves the cost of past
  sessions. When no rate moved, the new table carries `effective_from` (the date the rates
  are known from) rather than an identical snapshot. A snapshot may state `effective_from`
  by hand when the vendor's date is known from a citable source.
- The disclosure checklist, `cpt explain`, `cpt sessions summary` and the JSON output list the
  snapshots used with the calls each one priced, and count the calls older than the first
  snapshot of their model (priced with that first snapshot).
- `--prices-as-of DATE|latest` on `report`, `compare`, `explain`, `sessions list` and
  `sessions summary`: price every call at the rates of one day, on purpose.
- `prices/anthropic.json` carries its two earlier committed versions (2026-08-27, 2026-09-02)
  as history, restored from the repository. No rate differs between them.

### Changed
- The labelling sheet is ordered by what is left to do (status, then newest first) and its
  columns are regrouped: what identifies a session, then what you fill in, then ids and
  other reference columns. Sheets written by earlier versions still import.
- Outcomes typed as `ok`, `yes`, `oui`, `passed`, `ko`, `no`, `non` or `failed` are understood,
  in the sheet and in `cpt label --import`; a word that is neither is reported with its row
  number. `cpt label --import` checks every row before writing the first one.
- The report's attempt table widens its task column up to 36 characters and shortens a longer
  id from the left, so `project/issue-142` stays readable.
- `cpt sessions import` leaves out a session whose task is still the suggested one and that
  has no outcome (it was never looked at); `--include-unlabelled` imports it anyway. A task
  typed by hand imports with or without an outcome, as before.
- The `project` column of a session run in a Claude Code worktree is the repository's name,
  not the worktree folder's.
- A model the hub no longer lists keeps its last known rates (they stay under `history`);
  it used to become unpriced after `--write`.
- `as_of` and `effective_from` must be `YYYY-MM-DD` dates; anything else is rejected at load.

### Fixed
- In a folder without a log yet, `cpt report`, `tasks`, `compare`, `explain` and `label` answered
  with the operating system's `[Errno 2] No such file or directory`. They now say that there is
  no usage log yet, how one comes to be (an outcome in `cpt sessions label`, an import, or
  `cpt run`) and that `--log` reads one kept elsewhere. `cpt sessions label` says that nothing
  was written when no session got an outcome.

### Security
- `SECURITY.md`: what the tool reads, keeps and sends, what it does not protect against, the
  settings for a sensitive environment, how releases are built, and commands to check each
  statement.
- The labelling sheet can no longer carry a formula: a cell that starts with `=`, `+`, `-` or
  `@` (a session title is written by a model from what it read) is written behind an
  apostrophe and read back without it.
- A request whose path `http.client` rejects gets a 502 instead of a traceback that quoted the
  URL, query string included.
- The proxy refuses an upstream URL that carries credentials, without echoing it, and warns
  when an upstream outside the machine is plain `http://`.
- `cpt prices refresh --url` accepts web addresses only; `urlopen` would also have read
  `file://`.
- `cpt sessions list --no-infer` leaves the `branch` column empty too.
- `ci.yml` pins its Actions to commits and runs with a read-only token, as the release
  workflow already did; Dependabot keeps the pins current; `.gitignore` lists `.env`, `*.pem`
  and `*.key`.
- The proxy's two console messages (upstream error, stream without a usage block) print the
  request path without its query string. A gateway that takes the key as `?api_key=` could
  otherwise leave it in a redirected stderr. The usage log never held URLs and is unchanged.

## [0.6.0] - 2026-09-16

### Added
- `cpt sessions summary`: what the Claude Code and Cowork sessions on the computer would cost at
  API list prices, by product, model and period (day, week or month), with the token classes,
  the cache hit rate and the most expensive sessions. `--subscription PRICE` shows each month as
  a multiple of a monthly subscription. No labelling needed.
- `cpt explain`: where one attempt's cost went, by token class (input, cache reads, cache writes,
  output, reasoning), by model when several were used, and the most expensive steps. The attempt
  is named by its id or a unique prefix of it, or defaults to the latest one in the log.

### Changed
- `cpt sessions import` writes to `cpt-log.jsonl` and `cpt-labels.jsonl`, the files every other
  command reads by default, so `cpt report` works right after an import with no options. A folder
  holding only the 0.5.0 files (`sessions-log.jsonl`, `sessions-labels.jsonl`) keeps using them,
  with a message; `--log` and `--labels` still choose.
- The report command printed after an import no longer names a price table: the shipped tables
  apply by default.
- README: each Get started block opens with who it is for and ends with its own report command;
  task, attempt, step, leak, harness and shadow cost are defined where they first appear;
  `python -m cost_per_task.cli` is named as the fallback when `cpt` is not on PATH; the directory
  tree stops at the top level.
- The Claude sessions guide's compare command no longer needs a copy of the repository for
  `--prices`, and the testing guide's Level 3 states what changes for sessions users.

### Documentation
- Long context is no longer listed as a cause of understated cost for Claude: Anthropic bills
  Claude 4.6 and later models at the standard rate over the full 1M-token window (pricing page,
  read on 2026-09-14). The limitation still holds for OpenAI's GPT-5.5 and GPT-5.4 above 272K
  input tokens.
- The Claude Code and Cowork guide states that web searches run outside the transcripts, so
  their tokens and the search fee are not in a session's cost.
- A "Quick path" at the top of the Claude Code and Cowork guide: the six commands in order,
  with what each one shows, linked from the README's Get started block.

### Fixed
- `cpt sessions list --since` left out transcripts last written before the date, so a session
  started earlier and still active after it lost its older sub-agent transcripts (in Cowork, its
  other transcripts): the sheet showed part of its calls and cost, sometimes the wrong main
  model, and disagreed with `cpt sessions import`. Every transcript is now read, as the import
  does, and sessions are filtered afterwards, so `--since` no longer shortens the scan.
- `--since` compares the session end with the date in local time, as the sheet shows it. East of
  UTC, a session whose last call came shortly after local midnight on that date was left out.
- Anthropic streaming: the counts in `message_delta` are cumulative, so a response that ran
  server-side tools (web search) now logs its final input and cache counts instead of the
  `message_start` values. Deltas that only carry `output_tokens` still work.
- The disclosure checklist printed an empty `cache hit rate:` line, instead of `n/a`, when no
  group had a cache hit rate.
- `cpt --version` prints the installed version.
- CPT_solved divides the mean cost over labelled attempts, while the report's `attempt cost C`
  line averages all attempts. When some attempts are unlabelled, the report now prints the
  labelled mean next to the overall one and says so on the CPT_solved line; the JSON output
  gains `labelled_mean_cost`.
- Testing guide: a garbled sentence in "What to keep from a run".

## [0.5.0] - 2026-09-13

### Added
- `cpt sessions list` and `cpt sessions import`: measure Claude Code (desktop and CLI) and Cowork
  sessions from the transcripts they keep on disk, labelled in a spreadsheet that Excel can edit
  in any locale; subscription use is priced as a shadow cost at API list prices. Guide in
  `docs/claude-sessions.md`.
- Prices for Claude Opus 4.5 to 4.8 and Sonnet 4.5 and 4.6, checked against the Anthropic
  pricing table on 2026-09-13.
- The price tables ship inside the package, so a `pip install` can price sessions without a
  copy of the repository.
- The proxy records the effort level each request asked for (Anthropic `output_config.effort`,
  OpenAI `reasoning_effort` or `reasoning.effort`); the disclosure checklist shows it.
- Level 2 of the testing guide runs Claude Code in bare mode with a pinned effort and a spending
  cap per attempt, and both level scripts label each attempt by its explicit id.

### Changed
- The package metadata names jean@optimnow.io as the author contact.
- `--prices` is optional on `cpt report` and `cpt compare` and in the MCP tools: the Anthropic
  and OpenAI tables shipped with the tool apply by default.
- README redesigned for the public repository, with a diagram of how the tool works.
- Attempt id suffixes are 8 hex digits: the 4 digits in 0.4.0 could collide when many attempts
  start in the same second, which would merge them into one.
- The report's attempt column is wide enough for the full ids.

## [0.4.0] - 2026-09-12

Published to PyPI and as a GitHub Release on 2026-09-12, from commit 85d465e (tag `v0.4.0`).

### Added
- OpenRouter and other OpenAI-compatible gateways as the `openai` upstream, with a
  path prefix (`--openai-upstream https://openrouter.ai/api`).
- OpenRouter usage accounting requested automatically; the cost the gateway
  charged is stored as `reported_cost` and reconciled against the table price in
  the report and the disclosure checklist.
- Gateway model ids (`anthropic/claude-haiku-4.5`) match pricing tables without the
  vendor prefix and with dots as hyphens.
- `--prices` may be repeated to merge vendor tables; the merged `as_of` is the oldest.
- The log's `provider` field names the gateway host when a call did not go to the
  vendor directly.
- Testing guide (`docs/testing-guide.md`) with Level 1 and Level 2 scripts comparing two
  models, and `claude-fable-5-1` in `prices/anthropic.json`.

### Changed
- Attempt ids carry a random suffix so attempts started in the same second never merge;
  `cpt label` without `--attempt` picks the task's last attempt in log order.

### Infrastructure
- Release pipeline: tag `vX.Y.Z` on `main` to test, build, publish to PyPI via
  trusted publishing and create the GitHub Release.

## [0.3.0] - 2026-09-02

### Added
- `cpt import langfuse` and `cpt import litellm`: convert usage exports (CSV, JSON,
  JSONL) into cpt records with configurable task and attempt fields.
- `cpt prices refresh`: diff a pricing table against the OptimNow AI Pricing Hub
  catalogue; write only with `--write`; anomalous cache-read prices are flagged and
  models the hub cannot price fully are skipped.
- `cpt report --json` and `cpt compare --json`.
- `cpt mcp`: MCP server with `cpt_report`, `cpt_compare` and `cpt_risk_denominator`,
  via the optional `[mcp]` extra (the only optional dependency).
- `prices/openai.json` with verified, dated rates for GPT-5.5, 5.4, 5.4-mini, 5.4-nano.

## [0.2.0] - 2026-09-02

### Added
- OpenAI capture for the Chat Completions and Responses APIs, plain and streaming,
  on the same proxy port as Anthropic (routed by path, then auth-header style).
  `stream_options.include_usage` is injected on streaming chat requests.
- `cpt label`: pass, fail and leak labels in a separate append-only
  `cpt-labels.jsonl`, plus CSV import.
- Statistics: Wilson interval, task-cluster bootstrap, P90, capped-retry success,
  pass^k.
- Report per model and task type with CPT_solved, cost per task attempted,
  CPT_risk, and the disclosure checklist; `cpt compare` with break-even K*.
- `--task-type` on `cpt run`.

### Changed
- `reasoning_per_mtok: null` now means "billed at the output rate" rather than
  "not priced".

## [0.1.0] - 2026-09-01

### Added
- Local capture proxy for the Anthropic Messages API, plain and streaming, logging
  only usage metadata (never keys, headers, prompts or completions).
- JSONL step schema aligned with the OpenTelemetry GenAI semantic conventions.
- Dated pricing tables and per-attempt cost; `prices/anthropic.json` verified against
  the Anthropic price list.
- `cpt run`, `cpt serve`, `cpt report`.

[Unreleased]: https://github.com/OptimNow/cost-per-task/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/OptimNow/cost-per-task/releases/tag/v0.6.0
[0.5.0]: https://github.com/OptimNow/cost-per-task/releases/tag/v0.5.0
[0.4.0]: https://github.com/OptimNow/cost-per-task/releases/tag/v0.4.0
[0.3.0]: https://github.com/OptimNow/cost-per-task/releases/tag/v0.3.0
[0.2.0]: https://github.com/OptimNow/cost-per-task/releases/tag/v0.2.0
[0.1.0]: https://github.com/OptimNow/cost-per-task/releases/tag/v0.1.0
