# cost-per-task

> Measure what an AI agent really costs per task it completes correctly, not per
> million tokens. A small, dependency-free Python tool that sits between your agent
> and the model API, records the tokens every call actually used, prices them with
> dated prices, and reports cost per attempt, cost per solved task and risk-adjusted
> cost per task, with the statistics to trust the numbers.
> Built by [OptimNow](https://optimnow.io), implementing the measurement framework
> published by [DoiT](https://www.doit.com/research/economics-of-claude-openai-and-grok).

[![CI](https://github.com/OptimNow/cost-per-task/actions/workflows/ci.yml/badge.svg)](https://github.com/OptimNow/cost-per-task/actions/workflows/ci.yml)
[![GitHub Stars](https://img.shields.io/github/stars/OptimNow/cost-per-task?style=flat)](https://github.com/OptimNow/cost-per-task/stargazers)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![No runtime dependencies](https://img.shields.io/badge/dependencies-none-2C2C2C)](#design-principles)
[![Methodology: DoiT Cost Per Task](https://img.shields.io/badge/methodology-DoiT%20Cost%20Per%20Task-ACE849?labelColor=2C2C2C)](https://www.doit.com/research/economics-of-claude-openai-and-grok)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

---

## Why cost per task

Model vendors price tokens. Businesses buy outcomes: a merged pull request, a resolved
ticket, a correctly filed invoice. For an agent that runs dozens of model calls, retries
when it fails, and sometimes returns a wrong answer that looks right, the two are far
apart:

- A cheaper model that fails more often costs **more** per solved task, because you pay
  for the failed attempts too.
- Agent runs are heavy-tailed: the same task can cost several times more on a bad run,
  so the average alone understates what you will actually budget.
- A wrong answer that gets accepted is not free. Someone cleans it up later, and that
  cleanup cost belongs in the price of the task.

The formulas that turn token counts into those business numbers are set out in DoiT's
paper *Cost Per Task, Not Cost Per Token: A Measurement Framework for the Real Economics
of Claude, OpenAI and Grok* (13 August 2026). Capturing usage per call was already a
solved problem (LiteLLM, Helicone, Langfuse, OpenTelemetry). The missing half was
attempts, outcomes, dividing by the success rate, and the risk term. This tool adds
that half, and it works with any model.

**Who this is for:** FinOps and finance teams who need a defensible cost per unit of AI
work, engineers comparing two models on a real workload, and anyone publishing agent
cost figures who wants them to carry the disclosures needed to be believed. If you can
run a command in a terminal, you can use this.

---

## Quick start

Three steps: run the agent through the proxy, say whether it succeeded, read the report.

```
pip install cost-per-task
```

**1. Run.** Wrap the command that starts your agent. Nothing in the agent changes; it
just talks to `localhost` instead of the vendor.

```
cpt run --task-id issue-142 --task-type coding -- claude -p "fix the failing test in api/tests"
```

Every `cpt run` is one attempt at that task. Run it again for a second attempt.

**2. Label.** Once you have checked the result, record the outcome. A *leak* is an
answer you accepted at the time and later found to be wrong.

```
cpt label pass --task issue-142
cpt label fail --task issue-142 --attempt a20260901T212657Z-3f9c
cpt label pass --leak --task issue-142
```

**3. Report.** Price the log and, if you can put a figure on cleaning up a wrong
answer, pass it as the cleanup cost.

```
cpt report --prices prices/anthropic.json --cleanup-cost 25 --harness "Claude Code 2.1"
```

#Want a guided first run? [docs/testing-guide.md](docs/testing-guide.md) compares
two models step by step, from five-cent questions to a realistic workload, and
explains how to read every line of the report.

### What a report looks like

This is a real run: a Claude Code session answering a trivial prompt on Haiku, twice.

```
group: claude-haiku-4-5-20251001
  attempts 2 over 1 tasks; labelled 2 (pass 2, fail 0, leaked 1)
  attempt cost C: mean 0.0549 USD, P90 0.0557 USD, total 0.1099 USD
  success rate p: 1.000 (Wilson 95%: 0.342 to 1.000)
  CPT_solved = E[C] / p: 0.0549 USD (bootstrap 95%: 0.0549 to 0.0549, 10000 resamples)
  cost per task attempted (failures included): 0.1099 USD
  capped retries N=2: p_N 1.000
  pass^2: 1.000
  leak rate L: 0.500
  CPT_risk = CPT_solved + L x K: 12.5549 USD (K = 25.0000 USD)
  cache hit rate: 0.0%

disclosure checklist
  model versions: claude-haiku-4-5-20251001
  prices: as of 2026-08-27 in USD; source: OptimNow AI Pricing Hub, cross-checked against the vendor price list
  harness: Claude Code 2.1, -p mode
  ...
```

Two things this run shows that a per-token view hides. The question and answer were 68
tokens; 99% of the 5.5 cents went on Claude Code writing its 43,000-token system context
into the prompt cache. And with one of two answers flagged as a leak and a $25 cleanup
cost, the risk-adjusted cost per task is $12.55, two hundred times the raw cost. The
risk term is the whole point of the method.

---

## What you get

Per model and per task type, the paper's estimators with plain-language meaning:

| Reported | Formula | What it tells you |
|---|---|---|
| Attempt cost, mean and P90 | C_attempt = sum over calls of priced tokens (input, cache read, cache write, reasoning, output) | What one try costs, and what a bad try costs. Agent costs are heavy-tailed, so P90 is your budgeting number |
| Success rate p, with a Wilson 95% interval | passes / labelled attempts | How often a try works, and how sure you can be given how few tries you measured |
| Cost per solved task, with a bootstrap 95% interval | CPT_solved = E[C_attempt] / p | What a *correct* result costs once failed tries are paid for. The headline number |
| Cost per task attempted | total cost / distinct tasks | Same thing seen from the budget side: failures included, whether or not the task ever got solved |
| Capped-retry success p_N | 1 - (1 - p)^N | Chance of success within N tries |
| Consistency pass^k | share of tasks solved on every one of their first k tries | Whether the agent is reliable or merely lucky; single-try success rates hide collapse here |
| Leak rate L | leaked passes / passes | How often an accepted answer was actually wrong |
| Risk-adjusted cost per task | CPT_risk = CPT_solved + L x K | The cost once cleanup of leaked failures (K per leak) is counted |
| Break-even cleanup cost K* | (CPT_B - CPT_A) / (L_A - L_B) | For two models: below K* the cheaper, leakier one wins; above it the reliable one does |

Every report ends with the paper's **disclosure checklist**: model versions, prices with
dates and source, harness, cache hit rate, effort settings, sample size and k, which
intervals were used, leak rate, the cleanup cost assumed, and K* for comparisons. A cost
figure without these is an opinion; with them it is a measurement someone else can check.

Compare two models directly:

```
cpt compare claude-haiku-4-5 claude-sonnet-5 --prices prices/anthropic.json --cleanup-cost 25
```

---

## How it works

```
your agent  ->  cpt proxy on localhost  ->  api.anthropic.com / api.openai.com / a gateway
                        |
                        v
                 cpt-log.jsonl   one line per call: token counts, model, ids, latency
                 cpt-labels.jsonl   pass / fail / leak per attempt
```

**The proxy.** `cpt run` starts a small web server on your machine and points the
agent at it through `ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL`, the standard variables
every SDK honours. Each request is forwarded to the real API and the response is handed
back unchanged, streaming included. On the way through, the proxy copies the vendor's
own token counts out of the response. Token counts are never estimated with a local
tokenizer; they are what the vendor billed.

**What is never logged.** API keys, headers, prompts and completions. Prompts often
contain client data and have no place in a metrics file. Only token counts, model,
provider, task and attempt ids, tool names and latency are written. A test asserts this
on every commit.

**Labels live apart from the log.** The usage log is append-only and never rewritten;
outcomes go to `cpt-labels.jsonl` and can be corrected at any time.

**Attempts that mix models** (an agent using a small model for side calls) are
attributed to the model carrying the largest share of the cost.

---

## What it works with

| Source | How | Notes |
|---|---|---|
| **Anthropic** | proxy, any Claude model, plain or streaming | cache reads and 5-minute / 1-hour cache writes priced separately; reasoning is billed inside output |
| **OpenAI** | proxy, any model, Chat Completions and Responses APIs, plain or streaming | reasoning tokens split out of output; the proxy adds `stream_options.include_usage` so streams report usage |
| **OpenRouter** and other OpenAI-compatible gateways | proxy, `--openai-upstream https://openrouter.ai/api` | usage accounting requested so OpenRouter returns native counts and the cost it charged; the report reconciles that against list price. Covers Gemini, Grok, Mistral, DeepSeek and anything else the gateway routes |
| **Langfuse** exports | `cpt import langfuse observations.json` | for teams already logging usage; session as task, trace as attempt by default |
| **LiteLLM** spend logs | `cpt import litellm spend_logs.jsonl` | spend logs carry no cache breakdown, so imports show 0% cache hits |

Native adapters for Bedrock, Vertex AI and xAI are on the roadmap; each is a small file,
because the only thing that differs between vendors is the shape of the usage block.
The statistics, labels, report and comparison are model-agnostic already.

---

## Commands

| Command | What it does |
|---|---|
| `cpt run --task-id T [--task-type X] -- <command>` | run an agent command through the proxy, tagging every call with the task and a fresh attempt id |
| `cpt serve --port 4000 --task-id T` | run the proxy standalone and point any process at it |
| `cpt label pass\|fail [--task T] [--attempt A] [--leak]` | label the latest (or a named) attempt; `--import labels.csv` for batch labelling |
| `cpt report --prices P [--cleanup-cost K] [--harness H] [--json]` | the report above, per model and task type; repeat `--prices` to merge vendor tables |
| `cpt compare A B --prices P [--cleanup-cost K] [--json]` | two-model comparison with K* |
| `cpt import langfuse\|litellm FILE [--task-field F] [--attempt-field F]` | convert an export into cpt records |
| `cpt prices refresh --provider anthropic\|openai [--write]` | diff a pricing table against the OptimNow AI Pricing Hub; write only when asked |
| `cpt mcp` | serve the report to AI assistants over MCP (optional extra) |

Useful options on `report` and `compare`: `--leak-rate` to override the measured L,
`--retry-cap N`, `--k`, `--seed` for reproducible bootstrap intervals, `--task-type` to
filter, `--by-model-only` to ignore task types.

---

## Prices

Prices live in local JSON tables, one per vendor, per model and token class, per million
tokens. Every table carries an `as_of` date and a `source`; a table without a date is
rejected, because the disclosure checklist requires prices with dates.

```json
{
  "currency": "USD",
  "as_of": "2026-08-27",
  "source": "OptimNow AI Pricing Hub, cross-checked against platform.claude.com on 2026-08-27",
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

`prices/anthropic.json` and `prices/openai.json` ship with verified, dated rates.
`reasoning_per_mtok: null` means reasoning is billed at the output rate, which is how
both vendors price today. Model ids reported with a date suffix
(`claude-haiku-4-5-20251001`) or a gateway prefix (`anthropic/claude-haiku-4.5`) match
the table automatically.

**Keeping prices current.** The [OptimNow AI Pricing Hub](https://github.com/OptimNow/ai-pricing-hub-mcp)
(the [OptimToken](https://optimtoken.optimnow.io) catalogue, 250+ models, refreshed
daily) is the upstream:

```
cpt prices refresh --provider anthropic            # shows what would change
cpt prices refresh --provider anthropic --write    # accepts it
```

The default run prints new, removed and repriced models with the catalogue date, warns
about models the hub cannot price fully, and flags cache-read prices that look wrong.
Nothing is written without `--write`, so an upstream feed error never lands unseen.

**Known limitation.** One rate per token class. Long-context tiers (Anthropic above
200K input tokens, OpenAI above 272K) and batch discounts are not modelled; if your
attempts cross those thresholds the report understates cost, and says which prices it
applied.

---

## For AI assistants and other tools

`cpt report --json` and `cpt compare --json` emit the summaries as JSON. The same
numbers are available over MCP, so an assistant can answer "what does a solved ticket
cost us on Sonnet, with the interval?" from your own log, and so the
[OptimNow AI ROI Calculator](https://airoicalculator.optimnow.io) can use CPT_risk as
its cost denominator instead of a per-token guess:

```
pip install "cost-per-task[mcp]"
cpt mcp
```

Tools: `cpt_report`, `cpt_compare`, and `cpt_risk_denominator` (CPT_risk for one model
with sample size, intervals and price date). The `mcp` extra is the only optional
dependency; the core has none.

---

## Directory structure

```
cost-per-task/
├── README.md                     <- This file
├── CLAUDE.md                     <- Project context and hard rules for AI assistants
├── prices/                       <- Dated pricing tables (anthropic.json, openai.json, example.json)
├── src/cost_per_task/
│   ├── proxy.py                  <- The local capture proxy
│   ├── providers/                <- Per-vendor usage extraction (anthropic.py, openai.py)
│   ├── schema.py                 <- The JSONL record (OpenTelemetry GenAI aligned)
│   ├── labels.py                 <- pass / fail / leak labels and CSV import
│   ├── pricing.py, prices_hub.py <- Dated tables, per-step cost, Pricing Hub refresh
│   ├── stats.py                  <- Wilson, bootstrap, percentile, p_N, pass^k
│   ├── metrics.py                <- Attempts, groups, CPT_solved, CPT_risk, K*
│   ├── report.py                 <- Text and JSON report, disclosure checklist
│   ├── importers/                <- Langfuse and LiteLLM
│   ├── mcp_server.py             <- MCP tools (optional extra)
│   └── cli.py                    <- The cpt command
└── tests/                        <- pytest; proxy tests run against fake vendor servers
```

---

## Design principles

- **Token counts come from the vendor, never from a local tokenizer.** The paper's
  first rule, and the difference between a measurement and an estimate.
- **No number without a date and a source.** Prices carry `as_of` and `source`; the
  report repeats them. A price table without a date will not load.
- **Never invent a price.** The Hub refresh skips models it cannot fully price and
  flags anomalies rather than guessing; the human decides with `--write`.
- **Never log content.** Keys, headers, prompts and completions stay out of the log,
  enforced by a test.
- **Report the spread, not just the mean.** P90, Wilson and bootstrap intervals are
  always shown; small samples are visible as wide intervals rather than hidden.
- **The log is append-only.** Outcomes and corrections live in the labels file.
- **Nothing to install around it.** Standard library only. The MCP server is the one
  optional extra, and it is only imported when asked for.

---

## Status

Version 0.4.0, alpha. The Anthropic path has been validated end to end with a real
Claude Code session. OpenAI, OpenRouter and the importers are implemented against the
vendors' documented formats and tested against fake servers, not yet against live
traffic; run with `--dry-run` or a small task first and check the numbers against your
invoice. Confidence notes are in the code where a format was documented rather than
observed.

Roadmap: live validation of OpenAI and OpenRouter, importers checked on real exports,
Bedrock / Vertex / xAI adapters, long-context price tiers, PyPI release.

---

## Contributing

The most valuable contributions are real logs and real exports: "we ran this through
the proxy and the invoice said X" is worth more than any feature. Also welcome:
provider adapters, corrections to pricing rules with a source, and adversarial review of
the statistics. Open an issue first for anything structural.

---

## About OptimNow

OptimNow is a boutique FinOps consultancy helping organisations connect cloud and AI
spend to measurable business value. Based in France with European reach.

- Website: [optimnow.io](https://optimnow.io)
- LinkedIn: [OptimNow](https://linkedin.com/company/optimnow)
- GitHub: [github.com/OptimNow](https://github.com/OptimNow)

**Open-source tools built by OptimNow:**

| Tool | What it does |
|---|---|
| [OptimToken](https://optimtoken.optimnow.io) | Compare what 250+ models cost per request, with caching and batch factored in, plus compute instance rates across seven clouds. Also an MCP connector; `cpt prices refresh` reads from it |
| [Cloud FinOps Skill & MCP](https://github.com/OptimNow/cloud-finops-skills) | FinOps knowledge for AI agents: cloud cost, AI inference economics, allocation, chargeback, waste detection runbooks |
| [AI ROI Calculator](https://airoicalculator.optimnow.io) | Whether an AI project pays for itself: three-layer cost model, payback, break-even, sensitivity. Also an [MCP server](https://github.com/OptimNow/ai-roi-calculator-mcp); `cpt mcp` feeds it CPT_risk |
| [AI Cost Readiness Assessment](https://aicostsfinops.optimnow.io) | Where your organisation stands on AI cost management |

---

## Acknowledgements

The measurement model implemented here, including the token classes, cost per solved
task, the capped-retry variant, the risk-adjusted cost with leak rate and cleanup cost,
the break-even cleanup cost K*, the choice of Wilson and bootstrap intervals, pass^k,
and the disclosure checklist, is the work of **[DoiT](https://www.doit.com)**, published
as *Cost Per Task, Not Cost Per Token: A Measurement Framework for the Real Economics of
Claude, OpenAI and Grok* (DoiT Research, 13 August 2026), released under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/):
<https://www.doit.com/research/economics-of-claude-openai-and-grok>. Read the paper
for the reasoning behind each estimator; this repository only makes them runnable, and
the formula names in the report (C_attempt, CPT_solved, CPT_risk, K*) follow the paper
so the two can be read side by side.

Prices are sourced from the OptimNow AI Pricing Hub and cross-checked against the
vendors' published price lists, with the date of each check recorded in the tables.

This tool is independently maintained by OptimNow and is not affiliated with or
endorsed by DoiT, Anthropic, OpenAI or OpenRouter. Any implementation errors are ours.

---

## License

Licensed under the [MIT License](./LICENSE). You are free to use, modify and
redistribute this software, including commercially, provided the copyright notice is
kept. The methodology remains DoiT's; please cite their paper when you publish figures
produced with this tool.
