# cost-per-task

Measure the real cost per completed task of LLM agents.

`cost-per-task` captures token usage from every API call an agent makes, groups calls into steps, attempts and tasks, prices them from a dated pricing table, and computes cost per attempt, cost per solved task, and risk-adjusted cost per task. Output is a JSONL log plus a CLI report.

## Why

Per-token pricing is the wrong unit for agentic workloads. Agentic tasks consume orders of magnitude more tokens than single completions, with heavy run-to-run variance, and a cheaper model that fails more often can cost more per solved task than an expensive one. The right unit is the completed, correct task.

This tool implements the measurement framework published by DoiT: [Cost Per Task, Not Cost Per Token: A Measurement Framework for the Real Economics of Claude, OpenAI and Grok](https://www.doit.com/research/economics-of-claude-openai-and-grok) (13 August 2026). Capturing usage per call is a solved problem (LiteLLM, Helicone, Langfuse, OpenTelemetry GenAI conventions). The missing half is attempts, outcomes, success division and the risk term. That is what this tool adds.

## How it works

The primary capture mode is a local reverse proxy. You point your agent at it with an environment variable; no change to the agent's code is needed, so it works with Claude Code, LangGraph, or any agent you did not write.

```
agent  ->  cpt proxy (localhost)  ->  api.anthropic.com
               |
               v
        cpt-log.jsonl  (usage metadata only)
```

The proxy forwards each request unchanged, returns the response unchanged (streaming included), and records only the usage metadata: token counts by class, model, provider, task and attempt ids, tool call names, latency.

The proxy never logs API keys, headers, prompts or completions. Prompts may contain client data; they do not belong in a metrics log.

## Install

```
pip install cost-per-task
```

Python 3.11 or later. No runtime dependencies.

## Quick start

Run an agent command through the proxy, tagged with a task id:

```
cpt run --task-id issue-142 -- claude -p "fix the failing test in api/tests"
```

Each `cpt run` invocation is one attempt. Run it again for a second attempt at the same task. Then price the log:

```
cpt report --log cpt-log.jsonl --prices prices/anthropic.json
```

You can also run the proxy standalone and point any process at it:

```
cpt serve --port 4000 --task-id issue-142
# in another shell:
# set ANTHROPIC_BASE_URL=http://127.0.0.1:4000 and start your agent
```

## What gets logged

One JSONL line per API call, aligned with the OpenTelemetry GenAI semantic conventions:

| Field | Meaning |
| --- | --- |
| `task_id`, `attempt_id`, `step_id` | grouping keys: which task, which try, which call |
| `model`, `provider` | from the API response (`gen_ai.request.model`, `gen_ai.provider.name`) |
| `input_tokens` | fresh, uncached input |
| `cache_read_tokens`, `cache_write_tokens` | prompt cache traffic; 1h-TTL writes broken out in `cache_write_1h_tokens` |
| `reasoning_tokens` | null for providers that bill reasoning inside output (Anthropic) |
| `output_tokens` | visible output; includes reasoning where the provider does not split it |
| `tool_call_count`, `tool_names` | tool use in the response |
| `latency_ms`, `effort`, `outcome_label`, `timestamp` | timing, effort setting, pass/fail/pending |

Token counts always come from the provider API response, never from a local tokenizer.

## Pricing

Prices live in a local JSON table, per model and token class, expressed per million tokens. A table without an `as_of` date is rejected: the framework's disclosure checklist requires prices with dates.

```json
{
  "currency": "USD",
  "as_of": "2026-08-26",
  "source": "where these numbers came from",
  "models": {
    "claude-sonnet-5": {
      "input_per_mtok": 0.0,
      "cache_read_per_mtok": 0.0,
      "cache_write_5m_per_mtok": 0.0,
      "cache_write_1h_per_mtok": 0.0,
      "output_per_mtok": 0.0,
      "reasoning_per_mtok": null
    }
  }
}
```

The file shipped in `prices/example.json` contains zeros on purpose. Fill in current prices from your provider's price list, or from the [OptimNow AI Pricing Hub](https://github.com/OptimNow/ai-pricing-hub-mcp).

## Roadmap

Implemented (Phase 1): Anthropic capture (plain and streaming responses), JSONL schema, pricing, cost per attempt, per-attempt report.

Phase 2: OpenAI capture, labelling CLI (`pass`/`fail` plus CSV import and leak flags), cost per solved task with Wilson intervals, bootstrap intervals on CPT, P90, capped retries, pass^k, risk-adjusted CPT and the two-model break-even comparison, full disclosure-checklist report.

Phase 3: Langfuse and LiteLLM importers, AI Pricing Hub integration, MCP server exposing the report.

## Licence

MIT. See [LICENSE](LICENSE).
