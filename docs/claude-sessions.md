# Measuring your Claude Code and Cowork sessions

Claude Code, in the terminal or the desktop app, and Cowork keep a transcript of
every session on your computer. Each model response in a transcript records the
tokens Anthropic counted for it. cost-per-task reads those records and produces the
same report as the proxy (cost per attempt, success rate, cost per solved task,
risk-adjusted cost and the disclosure checklist) for work you have already done.
There is no proxy to run, no API key to set and nothing extra to pay.

## What it reads, and what it never touches

| | Read | Where it goes |
|---|---|---|
| Model, token counts (input, output, cache reads, cache writes), effort, tool names, timestamps | yes | the usage log |
| Product (desktop, terminal, Cowork) and Claude Code version | yes | the sheet and the disclosure checklist |
| Name of the folder the session worked in | yes | the sheet |
| Session title (desktop) or names of files a Cowork session produced | yes, unless you pass `--no-titles` | the sheet only, never the log |
| Your prompts, Claude's answers, tool inputs, file contents | never | nowhere |

The sheet can contain session titles, so treat it as private. The log and labels
files contain no content and can be shared with the numbers.

## Where the transcripts are

| Product | Windows | macOS |
|---|---|---|
| Claude Code, desktop app and terminal | `%USERPROFILE%\.claude\projects` | `~/.claude/projects` |
| Cowork | `%APPDATA%\Claude\local-agent-mode-sessions` | `~/Library/Application Support/Claude/local-agent-mode-sessions` |

The tool looks in these places by itself. If you set `CLAUDE_CONFIG_DIR`, it uses
that instead of `.claude`. If your transcripts live elsewhere, point at the folder
with `--path`. The Windows locations were checked on a real machine; the macOS ones
follow the same application conventions but have not been checked on a Mac yet.

## Before you start

Install cost-per-task once, version 0.5.0 or later:

```
python -m pip install cost-per-task
```

The price tables come with it. To work from a copy of the repository instead, run
`python -m pip install -e .` in its folder. Either way, the commands below run from any
folder.

## Step 1: list your sessions

```
python -m cost_per_task.cli sessions list
```

This writes `sessions.csv` in the current folder: one row per session, newest first,
with the product, start and end time, working folder, title, main model, number of
model calls and the cost at API list prices. Several hundred sessions take about a
minute. Useful options:

| Option | Effect |
|---|---|
| `--since 2026-09-01` | only sessions active on or after that date |
| `--source code` or `--source cowork` | only Claude Code, or only Cowork |
| `--no-titles` | leave session titles and Cowork file names out of the sheet |
| `--out other.csv` | write the sheet somewhere else |
| `--path FOLDER` | read transcripts from this folder instead of the usual places |

Running it again later adds new sessions and keeps everything you typed.

## Step 2: fill in the sheet

Open `sessions.csv` in Excel. The first ten columns come from the tool; leave them
as they are. The last five are yours:

| Column | What to write |
|---|---|
| `task` | a short name for the job. Give the same name to every session that attempted the same job. Leave it empty to skip a session. |
| `task_type` | a category such as `coding`, `writing` or `analysis`, so the report compares like with like |
| `outcome` | `pass` if you accepted the result, `fail` if you gave up or had to redo it |
| `leaked` | `yes` if you accepted the result and later found it was wrong |
| `note` | anything you want to remember |

Save it as CSV. Excel in any language is fine, including versions that save with
semicolons or in the older Windows character set.

Two tips. A session that did one job measures best; a session that mixed several
jobs counts as one attempt at whichever task you name. And label only sessions you
can judge honestly: an outcome you are unsure of is better left empty.

## Step 3: import and read the report

```
python -m cost_per_task.cli sessions import
```

This adds the usage of every session you gave a task to `sessions-log.jsonl`, and
your outcomes to `sessions-labels.jsonl`. These are not the files the proxy writes
(`cpt-log.jsonl` and `cpt-labels.jsonl`), so the report command names them:

```
python -m cost_per_task.cli report --log sessions-log.jsonl --labels sessions-labels.jsonl --cleanup-cost 25 --seed 1
```

The import prints this command for you, with `--harness` already filled in from the
products and Claude Code versions it saw; keep that part, because the disclosure
checklist needs it. `--cleanup-cost` is what one leaked failure costs you to repair, in
the currency of the price table; 25 is a placeholder. Running the import again adds only
sessions not yet imported, and updates outcomes you changed. The report is read exactly
as in the [testing guide](testing-guide.md).

To compare two models on real work, label sessions of the same `task_type` done on
each model, then:

```
python -m cost_per_task.cli compare claude-fable-5 claude-fable-5-1 --log sessions-log.jsonl --labels sessions-labels.jsonl --seed 1
```

A session that switched models is attributed to the model with the largest share of
its cost; the sheet shows it in the `main_model` column.

## What the cost means

The figures are what the same work would cost at Anthropic's API list prices, from a
dated table. On a Pro, Max, Team or Enterprise subscription you pay a flat fee, not per
token, so this is a shadow cost: useful to compare models and kinds of task, to see
what a subscription is worth to you, or to budget a move to the API. If you run Claude
Code with an API key, it is your real cost before any negotiated discount.

Not modelled, and reported when detected: fast mode, a priority service tier and data
residency surcharges, all of which cost more than list price. Long-context price tiers
and negotiated discounts are not modelled either.

## Limits

- The transcript format is internal to Anthropic's products, not a published interface.
  The importer was checked against Claude Code 2.1.2xx and Cowork in September 2026. If a
  later version changes the format, lines it cannot read are counted and reported, never
  guessed.
- Claude Code can delete old transcripts (its `cleanupPeriodDays` setting), and you can
  delete them yourself. Import the sessions you want to keep; the log holds their numbers
  after the transcripts are gone.
- When a response appears in two sessions, for example after resuming, it is counted once,
  in the first session where it was found.
