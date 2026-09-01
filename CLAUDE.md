# cost-per-task

Vendor-neutral Python tool measuring the real cost per completed task of LLM agents. Implements the DoiT "Cost Per Task, Not Cost Per Token" measurement framework (13 August 2026): a local reverse proxy captures token usage per API call, calls are grouped into steps, attempts and tasks, priced from a dated table, and reported as cost per attempt, cost per solved task (CPT_solved = E[C_attempt] / p) and risk-adjusted cost (CPT_risk = CPT_solved + L x K).

## Architecture

- `src/cost_per_task/proxy.py`: local reverse proxy; the agent's SDK is pointed at it via ANTHROPIC_BASE_URL. Forwards requests unchanged, logs only usage metadata.
- `src/cost_per_task/providers/`: per-provider usage extraction (JSON and SSE). Anthropic today; OpenAI in Phase 2; design allows Bedrock, Vertex, xAI.
- `src/cost_per_task/schema.py`: StepRecord (OpenTelemetry GenAI aligned) + JSONL persistence.
- `src/cost_per_task/pricing.py`: dated pricing tables, per-step and per-attempt cost.
- `src/cost_per_task/report.py`: plain-text summary per attempt and per model.
- `src/cost_per_task/cli.py`: `cpt serve`, `cpt run` (wraps an agent command, tags task/attempt ids), `cpt report`.
- `prices/anthropic.json`: verified, dated Anthropic rates; `prices/example.json` is a zero-value template.

## Hard rules

- **Never log secrets or content.** The proxy must never write API keys, headers, prompts or completions to the log. Only token counts, model, provider, ids, latency, tool names. Tests enforce this (`test_no_secrets_or_content_in_log`); keep them passing.
- **Token counts come from the provider API response, never a local tokenizer.**
- **No invented prices.** Pricing tables carry `as_of` dates and a source; an undated table is rejected at load. Do not add or update a price without a verifiable source.
- **Zero runtime dependencies.** Standard library only; pytest is the sole dev dependency. Any new dependency needs explicit justification and agreement.

## Conventions

- Python 3.11+, src/ layout, hatchling build, entry point `cpt`.
- Tests with pytest (`python -m pytest -q`); proxy tests run against the fake upstream in `tests/conftest.py`, never the real API.
- Documentation and user-facing text: British English, no em dashes, no emojis, facts before interpretation.
- Semver: user-visible feature changes bump the minor digit (0.1.x to 0.2.0), not patch.
- Streaming: Anthropic reports input usage in `message_start` and final output usage in `message_delta`; OpenAI (Phase 2) needs `stream_options.include_usage`. Anthropic does not report reasoning tokens separately (billed inside output_tokens), so `reasoning_tokens` is null for Anthropic rows.

## Roadmap

Phase 2: OpenAI capture, labelling CLI (pass/fail, CSV import, leak flags), statistics (Wilson interval, 10k bootstrap, P90, capped retries, pass^k), CPT_solved / CPT_risk / break-even K* report and two-model comparison, full disclosure-checklist output.
Phase 3: Langfuse and LiteLLM importers, AI Pricing Hub integration (github.com/OptimNow/ai-pricing-hub-mcp), MCP server exposing the report.
