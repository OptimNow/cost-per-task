# Changelog

All notable changes to cost-per-task. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow semantic
versioning, with the minor digit bumped for any user-visible feature.

## [Unreleased]

- Release pipeline: tag `vX.Y.Z` on `main` to test, build, publish to PyPI via
  trusted publishing and create the GitHub Release.

## [0.4.0] - 2026-09-02

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

[Unreleased]: https://github.com/OptimNow/cost-per-task/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/OptimNow/cost-per-task/releases/tag/v0.4.0
[0.3.0]: https://github.com/OptimNow/cost-per-task/releases/tag/v0.3.0
[0.2.0]: https://github.com/OptimNow/cost-per-task/releases/tag/v0.2.0
[0.1.0]: https://github.com/OptimNow/cost-per-task/releases/tag/v0.1.0
