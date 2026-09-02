# cost-per-task

Vendor-neutral Python tool measuring the real cost per completed task of LLM agents. Implements the DoiT "Cost Per Task, Not Cost Per Token" measurement framework (13 August 2026): a local reverse proxy captures token usage per API call, calls are grouped into steps, attempts and tasks, priced from a dated table, and reported as cost per attempt, cost per solved task (CPT_solved = E[C_attempt] / p) and risk-adjusted cost (CPT_risk = CPT_solved + L x K).

## Architecture

- `src/cost_per_task/proxy.py`: local reverse proxy serving Anthropic and OpenAI on one port (routed by path, then auth-header style); the agent is pointed at it via ANTHROPIC_BASE_URL / OPENAI_BASE_URL. Upstreams may carry a path prefix (OpenRouter: `https://openrouter.ai/api`); any OpenAI-compatible gateway works as the `openai` upstream and the log's `provider` becomes the gateway host. Forwards requests unchanged except for injecting `stream_options.include_usage` (streaming OpenAI chat) and `usage.include` (OpenRouter); logs only usage metadata plus `reported_cost` when the gateway states what it charged.
- `src/cost_per_task/providers/`: per-provider usage extraction (JSON and SSE): `anthropic.py`, `openai.py` (Chat Completions and Responses; subtracts cached and reasoning tokens out of the totals). Design allows Bedrock, Vertex, xAI.
- `src/cost_per_task/schema.py`: StepRecord (OpenTelemetry GenAI aligned) + JSONL persistence.
- `src/cost_per_task/pricing.py`: dated pricing tables, per-step cost; `reasoning_per_mtok: null` means output rate; `load_many` merges vendor tables (oldest `as_of` wins); `rates_for` strips gateway vendor prefixes and dot/hyphen differences.
- `src/cost_per_task/labels.py`: pass/fail/leak labels in a separate append-only `cpt-labels.jsonl` (latest wins), CSV import.
- `src/cost_per_task/stats.py`: Wilson interval, percentile, task-cluster bootstrap, p_N, pass^k. Standard library only.
- `src/cost_per_task/metrics.py`: records to Attempt (primary model = largest cost share) to GroupSummary per model and task type; CPT_solved, CPT_risk, K*.
- `src/cost_per_task/report.py`: text report, two-model comparison, disclosure checklist, JSON output.
- `src/cost_per_task/analysis.py`: files-to-summaries loader shared by the CLI and the MCP server.
- `src/cost_per_task/importers/`: Langfuse and LiteLLM export importers (`common.py` reads CSV/JSON/JSONL and groups rows into attempts). Built from documented schemas, not validated against live exports yet.
- `src/cost_per_task/prices_hub.py`: `cpt prices refresh`; fetches `optimtoken.optimnow.io/api/llm-models` with urllib, maps ids per provider (Anthropic dots to hyphens), derives cache-write rates from documented rules, diffs by default, writes only with `--write`, flags anomalous cache-read ratios.
- `src/cost_per_task/mcp_server.py`: MCP tools (`cpt_report`, `cpt_compare`, `cpt_risk_denominator`); the SDK is the optional `[mcp]` extra (mcp 2.x `MCPServer`, 1.x `FastMCP` fallback) and is imported lazily.
- `src/cost_per_task/cli.py`: `cpt serve`, `run`, `label`, `report`, `compare`, `import`, `prices refresh`, `mcp`.
- `prices/anthropic.json`, `prices/openai.json`: verified, dated rates; `prices/example.json` is a zero-value template. `tests/fixtures/hub_sample.json` is a recorded hub sample.

## Hard rules

- **Never log secrets or content.** The proxy must never write API keys, headers, prompts or completions to the log. Only token counts, model, provider, ids, latency, tool names. Tests enforce this (`test_no_secrets_or_content_in_log`); keep them passing.
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

## Roadmap

Phases 1 to 3 shipped (0.3.0), OpenRouter and OpenAI-compatible gateways in 0.4.0: capture for both providers, labelling, statistics, CPT_solved / CPT_risk / K*, disclosure checklist, importers, Pricing Hub refresh, JSON output, MCP server, provider-reported cost reconciliation.
Next: validate importers on real exports, further providers (Bedrock, Vertex, xAI), long-context price tiers, PyPI release and public repo (apply the OptimNow public-repo hardening standard first).
