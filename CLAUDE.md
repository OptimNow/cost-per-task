# cost-per-task

Vendor-neutral Python tool measuring the real cost per completed task of LLM agents. Implements the DoiT "Cost Per Task, Not Cost Per Token" measurement framework (13 August 2026): a local reverse proxy captures token usage per API call, calls are grouped into steps, attempts and tasks, priced from a dated table, and reported as cost per attempt, cost per solved task (CPT_solved = E[C_attempt] / p) and risk-adjusted cost (CPT_risk = CPT_solved + L x K).

## Architecture

- `src/cost_per_task/proxy.py`: local reverse proxy serving Anthropic and OpenAI on one port (routed by path, then auth-header style); the agent is pointed at it via ANTHROPIC_BASE_URL / OPENAI_BASE_URL. Upstreams may carry a path prefix (OpenRouter: `https://openrouter.ai/api`); any OpenAI-compatible gateway works as the `openai` upstream and the log's `provider` becomes the gateway host. Forwards requests unchanged except for injecting `stream_options.include_usage` (streaming OpenAI chat) and `usage.include` (OpenRouter); logs only usage metadata plus `reported_cost` when the gateway states what it charged. It also records the effort level each request asked for (Anthropic `output_config.effort`, OpenAI `reasoning_effort` or `reasoning.effort`) and nothing else from the body.
- `src/cost_per_task/providers/`: per-provider usage extraction (JSON and SSE): `anthropic.py`, `openai.py` (Chat Completions and Responses; subtracts cached and reasoning tokens out of the totals). Design allows Bedrock, Vertex, xAI.
- `src/cost_per_task/schema.py`: StepRecord (OpenTelemetry GenAI aligned) + JSONL persistence.
- `src/cost_per_task/pricing.py`: dated pricing tables, per-step cost; `reasoning_per_mtok: null` means output rate; `load_many` merges vendor tables (oldest `as_of` wins); `rates_for` strips gateway vendor prefixes and dot/hyphen differences.
- `src/cost_per_task/labels.py`: pass/fail/leak labels in a separate append-only `cpt-labels.jsonl` (latest wins), CSV import.
- `src/cost_per_task/stats.py`: Wilson interval, percentile, task-cluster bootstrap, p_N, pass^k. Standard library only.
- `src/cost_per_task/metrics.py`: records to Attempt (primary model = largest cost share) to GroupSummary per model and task type; CPT_solved, CPT_risk, K*.
- `src/cost_per_task/report.py`: text report, two-model comparison, disclosure checklist, JSON output.
- `src/cost_per_task/analysis.py`: files-to-summaries loader shared by the CLI and the MCP server.
- `src/cost_per_task/importers/`: Langfuse and LiteLLM export importers (`common.py` reads CSV/JSON/JSONL and groups rows into attempts). Built from documented schemas, not validated against live exports yet. `claude_sessions.py` (`cpt sessions list` / `import`) reads Claude Code transcripts (`~/.claude/projects`, or `CLAUDE_CONFIG_DIR`) and Cowork sessions (`<app data>/Claude/local-agent-mode-sessions/*/*/local_*/.claude/projects`): one attempt per session id (Cowork: per `local_*` folder), sub-agent transcripts folded in by their parent sessionId, each response counted once by (message.id, requestId) with its highest counts, `<synthetic>` skipped; titles and Cowork output file names go to the labelling sheet only, never the log. The format is Anthropic-internal and was checked on Claude Code 2.1.2xx.
- `src/cost_per_task/prices_hub.py`: `cpt prices refresh`; fetches `optimtoken.optimnow.io/api/llm-models` with urllib, maps ids per provider (Anthropic dots to hyphens), derives cache-write rates from documented rules, diffs by default, writes only with `--write`, flags anomalous cache-read ratios.
- `src/cost_per_task/mcp_server.py`: MCP tools (`cpt_report`, `cpt_compare`, `cpt_risk_denominator`); the SDK is the optional `[mcp]` extra (mcp 2.x `MCPServer`, 1.x `FastMCP` fallback) and is imported lazily.
- `src/cost_per_task/cli.py`: `cpt serve`, `run`, `label`, `report`, `compare`, `import`, `prices refresh`, `mcp`. Attempt ids are `aYYYYMMDDTHHMMSSZ-xxxxxxxx` (random suffix); `cpt label` without `--attempt` takes the task's last attempt in log order.
- `prices/anthropic.json`, `prices/openai.json`: verified, dated rates; `prices/example.json` is a zero-value template. `tests/fixtures/hub_sample.json` is a recorded hub sample. Claude Fable 5.1's cache-read rate is a documented special 0.025x (0.25 USD/MTok); the refresh command flags it as suspicious by design, and Anthropic's table confirms it. They ship inside the wheel as `cost_per_task/prices/` (hatch force-include); `cpt sessions list` looks in the current folder, then the package, then the repository.
- `docs/testing-guide.md` plus `examples/` (`ask.py` stdlib client, `level1.ps1`, `level2.ps1`, `level2/` tasks): the guided model comparison for non-developers. The scripts name each attempt (`cpt run --attempt-id`) and label it by that id, never by "latest"; each level writes its own log and labels files; Level 2 runs Claude Code with `--bare --strict-mcp-config`, a pinned `--effort` and `--max-budget-usd`, and prints the harness string for `--harness`.
- `docs/claude-sessions.md`: the guide to measuring Claude Code and Cowork sessions from their transcripts, for any user.

## Hard rules

- **Never log secrets or content.** The proxy must never write API keys, headers, prompts or completions to the log. Only token counts, model, provider, ids, latency, tool names and the requested effort level. Tests enforce this (`test_no_secrets_or_content_in_log`); keep them passing.
- **Token counts come from the provider API response, never a local tokenizer.**
- **No invented prices.** Pricing tables carry `as_of` dates and a source; an undated table is rejected at load. Do not add or update a price without a verifiable source.
- **Zero runtime dependencies in the core.** Standard library only; pytest is the sole dev dependency. The only optional extra is `[mcp]` for `cpt mcp`. Any new dependency needs explicit justification and agreement.
- **Hub prices never land unseen.** `cpt prices refresh` diffs by default; `--write` is a deliberate step after reading the diff and warnings.

## Conventions

- Python 3.11+, src/ layout, hatchling build, entry point `cpt`.
- Tests with pytest (`python -m pytest -q`); proxy tests run against the fake upstream in `tests/conftest.py`, never the real API.
- Documentation and user-facing text: British English, no em dashes, no emojis, facts before interpretation.
- Semver: user-visible feature changes bump the minor digit (0.1.x to 0.2.0), not patch.
- Streaming: Anthropic reports input usage in `message_start` and final output usage in `message_delta`; OpenAI Chat Completions only reports usage on a stream when `stream_options.include_usage` is set (the proxy injects it); the Responses API reports usage in `response.completed`. Anthropic does not report reasoning tokens separately (billed inside output_tokens), so `reasoning_tokens` is null for Anthropic rows; OpenAI reports reasoning as a subset of output and the adapter splits it out.
- The usage log is never rewritten; outcomes go to the labels file.
- Pricing tables hold one rate per token class; long-context tiers (Anthropic >200K, OpenAI >272K) and batch discounts are a documented limitation, not silently approximated.

## Workflow and releases

- Work on a branch and open a pull request; CI (`ci.yml`) must be green before merging to `main`. Do not push to `main` directly.
- Stacked pull requests: once a base pull request merges, retarget the next one to `main` before merging it, or merge from the top of the stack down. Otherwise its changes land on the intermediate branch and never reach `main`, as happened with #2 and #3 in September 2026.
- Release: bump `version` in `pyproject.toml` and `__version__` in `src/cost_per_task/__init__.py` (minor digit for features), move the `[Unreleased]` entries in `CHANGELOG.md` under the new version, merge, then `git tag vX.Y.Z` on `main` and push the tag. `publish-pypi.yml` tests the tagged commit, builds, publishes through PyPI trusted publishing (GitHub environment `pypi`, no stored token) and creates the GitHub Release. Re-publish an existing tag with `gh workflow run publish-pypi.yml -f tag=vX.Y.Z`.
- One-time setup owned by the repository owner: the PyPI pending publisher for `cost-per-task` (owner OptimNow, repo cost-per-task, workflow `publish-pypi.yml`, environment `pypi`) and the GitHub environment `pypi`.

## Roadmap

Phases 1 to 3 shipped (0.3.0), OpenRouter and OpenAI-compatible gateways in 0.4.0: capture for both providers, labelling, statistics, CPT_solved / CPT_risk / K*, disclosure checklist, importers, Pricing Hub refresh, JSON output, MCP server, provider-reported cost reconciliation.
Changes since 0.4.0 are listed under `[Unreleased]` in CHANGELOG.md. The Anthropic path is validated live (Claude Code on Haiku, Level 1 of the testing guide on Fable 5 and 5.1).
Next: live OpenAI and OpenRouter runs, importers checked on real exports, further providers (Bedrock, Vertex, xAI), long-context price tiers, PyPI release and public repo (apply the OptimNow public-repo hardening standard first).
