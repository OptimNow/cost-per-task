# Changelog

All notable changes to cost-per-task. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow semantic
versioning, with the minor digit bumped for any user-visible feature.

## [Unreleased]

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

[Unreleased]: https://github.com/OptimNow/cost-per-task/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/OptimNow/cost-per-task/releases/tag/v0.5.0
[0.4.0]: https://github.com/OptimNow/cost-per-task/releases/tag/v0.4.0
[0.3.0]: https://github.com/OptimNow/cost-per-task/releases/tag/v0.3.0
[0.2.0]: https://github.com/OptimNow/cost-per-task/releases/tag/v0.2.0
[0.1.0]: https://github.com/OptimNow/cost-per-task/releases/tag/v0.1.0
