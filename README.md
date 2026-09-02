# cost-per-task

Measure the real cost per completed task of LLM agents.

`cost-per-task` captures token usage from every API call an agent makes, groups calls into steps, attempts and tasks, prices them from a dated pricing table, and computes cost per attempt, cost per solved task, and risk-adjusted cost per task. Output is a JSONL log plus a CLI report that doubles as a disclosure checklist.

## Why

Per-token pricing is the wrong unit for agentic workloads. Agentic tasks consume orders of magnitude more tokens than single completions, with heavy run-to-run variance, and a cheaper model that fails more often can cost more per solved task than an expensive one. The right unit is the completed, correct task.

This tool implements the measurement framework published by DoiT: [Cost Per Task, Not Cost Per Token: A Measurement Framework for the Real Economics of Claude, OpenAI and Grok](https://www.doit.com/research/economics-of-claude-openai-and-grok) (13 August 2026). Capturing usage per call is a solved problem (LiteLLM, Helicone, Langfuse, OpenTelemetry GenAI conventions). The missing half is attempts, outcomes, success division and the risk term. That is what this tool adds.

## How it works

The primary capture mode is a local reverse proxy. You point your agent at it with an environment variable; no change to the agent's code is needed, so it works with Claude Code, LangGraph, or any agent you did not write.

```
agent  ->  cpt proxy (localhost)  ->  api.anthropic.com / api.openai.com
               |
               v
        cpt-log.jsonl  (usage metadata only)
```

The proxy forwards each request, returns the response unchanged (streaming included), and records only the usage metadata: token counts by class, model, provider, task and attempt ids, tool call names, latency. One port serves both providers; requests are routed by path.

The proxy never logs API keys, headers, prompts or completions. Prompts may contain client data; they do not belong in a metrics log.

The one deliberate change the proxy makes to a request: streaming OpenAI Chat Completions calls get `stream_options.include_usage` set, because without it OpenAI never reports usage on a stream. Disable with `--no-inject-usage`.

## Install

```
pip install cost-per-task
```

Python 3.11 or later. No runtime dependencies.

## Quick start

Run an agent command through the proxy, tagged with a task id and optional task type:

```
cpt run --task-id issue-142 --task-type coding -- claude -p "fix the failing test in api/tests"
```

Each `cpt run` invocation is one attempt. Run it again for a second attempt at the same task. The child process sees `ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL` pointing at the proxy; it must authenticate with an API key (for Claude Code, set `ANTHROPIC_API_KEY`).

Label the outcome once you have checked the result:

```
cpt label pass --task issue-142            # latest attempt of that task
cpt label fail --task issue-142 --attempt a20260901T212657Z
cpt label pass --leak --task issue-142     # accepted at the time, later found wrong
cpt label --import labels.csv              # columns: task_id, attempt_id, outcome, leaked, note
```

Then report:

```
cpt report --prices prices/anthropic.json --cleanup-cost 25 --harness "Claude Code 2.1"
cpt compare claude-haiku-4-5 claude-sonnet-5 --prices prices/anthropic.json --cleanup-cost 25
```

You can also run the proxy standalone with `cpt serve --port 4000 --task-id issue-142` and point any process at it.

## What the report contains

Per model and task type (the paper's Section 4 estimators):

| Quantity | Definition |
| --- | --- |
| C_attempt | sum over steps of priced tokens: input, cache read, cache write, reasoning, output |
| mean and P90 of C_attempt | agentic cost is heavy-tailed; the mean alone understates budget risk |
| success rate p | labelled passes over labelled attempts, with a Wilson score interval (z = 1.96) |
| CPT_solved | E[C_attempt] / p, with a percentile bootstrap interval from 10,000 task resamples |
| cost per task attempted | total cost / distinct tasks, failures included |
| p_N | 1 - (1 - p)^N, success within N capped retries |
| pass^k | share of tasks solved on every one of their first k attempts |
| leak rate L | leaked passes / passes (accepted outputs that were actually wrong) |
| CPT_risk | CPT_solved + L x K, where K is the cleanup cost per leaked failure (`--cleanup-cost`) |
| K* | (CPT_B - CPT_A) / (L_A - L_B), the cleanup cost at which two models break even (`cpt compare`) |

Every report ends with the disclosure checklist: model versions, prices with dates and source, harness, cache hit rate, effort settings, n and k, the intervals used, leak rate, K assumed, and K* for comparisons.

An attempt that mixes models (Claude Code uses a small model for side calls) is attributed to the model carrying the largest share of its cost.

## What gets logged

One JSONL line per API call, aligned with the OpenTelemetry GenAI semantic conventions:

| Field | Meaning |
| --- | --- |
| `task_id`, `attempt_id`, `step_id` | grouping keys: which task, which try, which call |
| `task_type` | free-text category from `--task-type`, used to group the report |
| `model`, `provider` | from the API response (`gen_ai.request.model`, `gen_ai.provider.name`) |
| `input_tokens` | fresh, uncached input |
| `cache_read_tokens`, `cache_write_tokens` | prompt cache traffic; Anthropic 1h-TTL writes broken out in `cache_write_1h_tokens`; OpenAI reports writes only from GPT-5.6 |
| `reasoning_tokens` | separated out for OpenAI; null for Anthropic, which bills reasoning inside output |
| `output_tokens` | visible output (reasoning excluded where the provider reports it separately) |
| `tool_call_count`, `tool_names` | tool use in the response |
| `latency_ms`, `effort`, `outcome_label`, `timestamp` | timing, effort setting, pass/fail/pending |

Token counts always come from the provider API response, never from a local tokenizer. Labels live in a separate `cpt-labels.jsonl`, so the usage log stays append-only.

## Pricing

Prices live in a local JSON table, per model and token class, expressed per million tokens. A table without an `as_of` date is rejected: the framework's disclosure checklist requires prices with dates. Model ids in responses often carry a date suffix (`claude-haiku-4-5-20251001`); the longest matching table key wins.

```json
{
  "currency": "USD",
  "as_of": "2026-08-27",
  "source": "where these numbers came from",
  "models": {
    "claude-sonnet-5": {
      "input_per_mtok": 2.0,
      "cache_read_per_mtok": 0.2,
      "cache_write_5m_per_mtok": 2.5,
      "cache_write_1h_per_mtok": 4.0,
      "output_per_mtok": 10.0,
      "reasoning_per_mtok": null
    }
  }
}
```

`reasoning_per_mtok: null` means reasoning tokens are billed at the output rate, which is how both providers price today. `prices/anthropic.json` and `prices/openai.json` ship with verified, dated rates; `prices/example.json` is a zero-value template. Refresh them from your provider's price list or the [OptimNow AI Pricing Hub](https://github.com/OptimNow/ai-pricing-hub-mcp).

Known limitation: the table holds one rate per token class. Long-context price tiers (Anthropic above 200K input tokens, OpenAI above 272K) and batch discounts are not modelled; if your attempts cross those thresholds the report understates cost, and the disclosure checklist states which prices were applied.

## Roadmap

Implemented: Anthropic and OpenAI capture (plain and streaming), JSONL schema, dated pricing, labelling CLI with CSV import and leak flags, Wilson and bootstrap intervals, P90, capped retries, pass^k, CPT_solved, CPT_risk, two-model comparison with K*, disclosure checklist.

Phase 3: Langfuse and LiteLLM importers, AI Pricing Hub integration, MCP server exposing the report so an AI ROI calculator can use CPT_risk as its denominator, and further providers (Bedrock, Vertex, xAI).

## Licence

MIT. See [LICENSE](LICENSE).
