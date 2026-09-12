# Testing guide: Fable 5.1 against Fable 5 with cost-per-task

This guide walks through proving that cost-per-task works, using a comparison
anyone can run: the same tasks, on Claude Fable 5.1 and Claude Fable 5, measured
the same way. It starts with questions that cost a fraction of a cent, moves to
small coding tasks, then explains how to run a realistic comparison on your own
work. No programming is needed; you copy commands into PowerShell.

## What you are proving, and why these two models

Both Fable models cost the same per token: $10 per million input, $50 per million
output. On a per-token view they are identical. They differ in three things the
per-token view cannot see:

- **Reliability.** Fable 5.1 is the newer model. If it solves more tasks first time,
  its cost per *solved* task is lower even though its price list is the same.
- **How much it thinks.** Both models always think before answering, and thinking is
  billed at the output rate. If one thinks more per question, it costs more per
  attempt at the same list price. Only a measurement can tell you which.
- **Cache reads.** Fable 5.1 reads cached context at $0.25 per million tokens
  against $1.00 for Fable 5 (a documented special rate). Agent sessions re-read a
  large cached system prompt on every turn, so this shows up as a real gap in
  attempt cost on the coding tasks, and no gap at all on the simple questions.

If the tool works, the report will show these effects, with intervals, from your
own log. That is the demonstration.

**Budget.** Level 1 costs well under $1 in total (30 questions; each answer
includes some billed thinking, typically a few cents' worth at most). Level 2 costs
real money: each Claude Code attempt writes roughly 45,000 tokens of system prompt into the cache
($0.56 at Fable prices) before doing any work, so expect $1 to $2 per attempt and
$20 to $40 for the full level. Level 3 depends on your tasks; plan $50 to $150.

## Setup (once, about ten minutes)

1. **Install the tool** from the repository folder, in PowerShell:

   ```powershell
   cd C:\Users\jlati\Documents\GitHub\cost-per-task
   python -m pip install -e .
   ```

2. **Set your API key** in the same window. This must be a Console API key
   (platform.claude.com, API keys), not a claude.ai login: the child processes we
   start cannot use a subscription login. The key lives only in this window and is
   gone when you close it.

   ```powershell
   $env:ANTHROPIC_API_KEY = "sk-ant-..."
   ```

   Fable models require the organisation's data retention setting to allow 30-day
   retention. If a call fails with a 400 error mentioning retention, that is the
   cause, and it is fixed in the Console, not here.

3. **Allow the scripts to run**, for this window only:

   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   ```

4. **Check the prices.** Both models are in `prices\anthropic.json`, dated and
   sourced. Confirm the table is still current against the Pricing Hub:

   ```powershell
   python -m cost_per_task.cli prices refresh --provider anthropic
   ```

   It prints what would change and writes nothing. If it reports changed rates for
   `claude-fable-5-1` or `claude-fable-5`, check them against
   platform.claude.com/docs (prompt caching pricing table) before accepting with
   `--write`; the disclosure checklist will carry whichever date the table has.

5. **Each level keeps its own files.** Level 1 writes `level1-log.jsonl` and
   `level1-labels.jsonl`, Level 2 writes `level2-log.jsonl` and
   `level2-labels.jsonl`, in the repository folder, so simple questions and coding
   tasks never end up averaged together. To start a level again from scratch,
   rename or delete its two files.

## Level 1: five simple questions (10 minutes, well under $1)

Five questions with one right answer each: a multiplication, an invoice number to
extract, a day count, a small JSON object, a sort. Each is asked three times per
model. The answer is checked automatically against the expected text and the
attempt is labelled pass or fail without you doing anything.

Run once per model:

```powershell
.\examples\level1.ps1 -Model claude-fable-5-1
.\examples\level1.ps1 -Model claude-fable-5
```

You will see each attempt, the model's answer, `CHECK: pass` or `CHECK: fail`, and
the label being recorded. Then:

```powershell
python -m cost_per_task.cli report --log level1-log.jsonl --labels level1-labels.jsonl --prices prices\anthropic.json --seed 1
```

**How to read it.** You get one group per model. Expect:

- `attempts 15 over 5 tasks; labelled 15 (pass 15, fail 0, leaked 0)` or close to it.
  If a model failed a question, that is data, not a problem.
- `attempt cost C: mean` of a fraction of a cent to a few cents. The question
  itself is about 40 tokens; most of the cost is the model's thinking, billed as
  output. Compare the two models here: with no cached context the cheaper cache
  reads of Fable 5.1 play no part, so any gap in mean cost comes from how much each
  model chose to think. The per-attempt table's `output` column shows it.
- `success rate p: 1.000 (Wilson 95%: 0.796 to 1.000)`. Fifteen successes out of
  fifteen still leaves a lower bound of about 80%: the interval is the tool telling
  you how little fifteen tries actually prove. It narrows as n grows.
- `CPT_solved = E[C] / p`: equal to the mean attempt cost when everything passed.
- `pass^3`: the share of tasks solved on all three tries. Expect 1.000 here.

What this level proves: capture works (tokens are counted from the API response),
pricing works (each attempt's cost is its input tokens at $10 per million plus its
output tokens, thinking included, at $50 per million; you can check one line by
hand), labelling works, and the statistics behave sensibly. Success rates should be
equal; a difference in cost, if any, is thinking volume.

Then compare them explicitly, so you see the comparison output once on data where
the answer is "no difference":

```powershell
python -m cost_per_task.cli compare claude-fable-5 claude-fable-5-1 --log level1-log.jsonl --labels level1-labels.jsonl --prices prices\anthropic.json --seed 1
```

With no leaks recorded, K* is undefined and the report says so. That is correct.

## Level 2: three small coding tasks with Claude Code (30 to 60 minutes, $20 to $40)

Three tasks in `examples\level2\`, each a folder with a task description, a Python
file to complete or fix, and tests that decide success:

| Task | What the agent must do | Why it is here |
|---|---|---|
| `fizzbuzz` | implement a classic function from a specification | easy, should pass every time; a baseline |
| `datefix` | find and fix a sign bug in a date calculation | small but requires reading the tests and reasoning |
| `csvtotal` | implement a CSV aggregation | slightly more open, several correct solutions |

For every attempt the script copies the task folder to a fresh scratch directory,
runs Claude Code in print mode through the proxy, then runs the tests. Tests
passing means `pass`, anything else means `fail`. Three attempts per task per model.

```powershell
.\examples\level2.ps1 -Model claude-fable-5-1
.\examples\level2.ps1 -Model claude-fable-5
```

Then:

```powershell
python -m cost_per_task.cli report --log level2-log.jsonl --labels level2-labels.jsonl --prices prices\anthropic.json --seed 1 --harness "Claude Code, -p mode, default effort"
python -m cost_per_task.cli compare claude-fable-5 claude-fable-5-1 --log level2-log.jsonl --labels level2-labels.jsonl --prices prices\anthropic.json --seed 1
```

**How to read it.**

- `attempt cost C: mean` is now dollars, not tenths of a cent, and dominated by
  the cache write of Claude Code's system prompt (visible in the per-attempt table
  as `cache_w` tokens). Compare the two models' means: Fable 5.1 should be lower,
  and the gap should grow with the number of turns an attempt took, because each
  turn re-reads the cached prompt at a quarter of the price.
- `P90` versus `mean`: the same task will not cost the same twice. If one attempt
  took a long detour, P90 shows it while the mean smooths it away.
- `success rate p` with its interval, and `CPT_solved`: if one model failed a task
  once, its cost per solved task jumps, because the failed attempt is paid for by
  the successful ones. This is the paper's central point and the first place the
  models may separate.
- `cache hit rate` in the checklist: how much of each model's prompt traffic was
  served from cache. Roughly equal for both; the *price* of those hits differs.
- The `disclosure checklist` at the end is what you would publish alongside any
  claim: model versions, price dates, harness, n, k, intervals. Keep it.

Two notes on the setup. The Claude Code flags `--permission-mode acceptEdits` and
`--allowedTools` let it edit files and run Python without asking; if a run stops
waiting for permission, that is the place to look (confidence on the exact flag
set is medium-high; the Claude Code documentation is the reference). And Claude
Code's own default effort for Fable models applies; if you want to test the effect
of effort, that is a Level 3 variable.

## Level 3: a realistic comparison on your own work

Levels 1 and 2 prove the tool. Level 3 produces a number you could put in front of
a client. The paper's protocol, applied:

1. **Choose 5 to 10 real tasks** from one type of work: resolving support tickets
   from a queue, fixing issues from a real repository, producing a monthly cost
   summary from an export. All tasks the same type, so the comparison is fair.
   Give each a stable `--task-id` and the same `--task-type`.
2. **Same prompt, same harness, both models.** Only the `--model` flag changes.
3. **At least four attempts per task per model.** The paper found up to 30x
   variance between runs of the same task; fewer than four attempts cannot see it.
4. **Label honestly, and separately from running.** For each attempt decide pass
   or fail against a written acceptance rule you set before starting. Keep a
   spreadsheet with columns `task_id, attempt_id, outcome, leaked, note` and import
   it in one go:

   ```powershell
   python -m cost_per_task.cli label --import labels.csv
   ```

   The attempt ids are in the log file and in the output of each `cpt run`.
5. **Record leaks.** A leak is an attempt you marked `pass` at the time and later
   found to be wrong: the ticket was reopened, the fix broke something else, the
   summary had a wrong figure. Mark it `leaked` (and keep `pass`, since it was
   accepted). Leak rate is the L in CPT_risk and it is usually the number that
   decides between a cheap model and a reliable one.
6. **Put a price on cleanup.** K is what one leaked failure costs you to repair:
   an hour of an engineer, a customer credit, a re-run. It is an assumption, so
   state it, and try two or three values:

   ```powershell
   python -m cost_per_task.cli compare claude-fable-5 claude-fable-5-1 --prices prices\anthropic.json --cleanup-cost 25 --seed 1 --harness "..."
   ```

   K* in the output is the cleanup cost at which the two models break even. If
   your real K is above K*, the more reliable model is cheaper all-in even when its
   attempts cost more.
7. **Publish the checklist with the numbers.** Every figure from `cpt report` and
   `cpt compare` comes with the disclosure checklist; a cost claim without it is an
   opinion.

Variables worth a second round once the baseline exists: effort level (Claude
Code's `--effort` or the API's `output_config.effort`), a different harness, or a
third model such as Sonnet 5 to see where the cheaper tier stops being cheaper.

## If something goes wrong

| Symptom | Meaning | Fix |
|---|---|---|
| `Not logged in · Please run /login` | the child Claude Code found no API key | set `$env:ANTHROPIC_API_KEY` in this window |
| `cpt: upstream returned HTTP 401` | key rejected | re-copy the key from the Console; check it is not revoked |
| `cpt: upstream returned HTTP 400` mentioning retention | Fable needs 30-day data retention | Console setting for the organisation |
| `captured 0 steps` | nothing reached the API, or every call failed | read the lines above it; the proxy prints every upstream error |
| `warning: no price for model` | a model id the table does not know | add it to `prices\anthropic.json` with a dated source |
| `CPT_solved: n/a` | no labelled pass yet | label the attempts, or check the labels file path |
| interval very wide | small n | it is telling the truth; add attempts |

## What to keep from a run

For your own records, and for anyone who wants to check the result: the two files
level's log and labels files, the `prices\anthropic.json` used, and the
full text of `cpt report` and `cpt compare`. Together they let someone reproduce
every number in the report; `--seed 1` makes the bootstrap intervals reproducible too.
