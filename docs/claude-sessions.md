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
| Model, token counts (input, output, cache reads, cache writes), effort, speed (fast mode or standard), tool names, timestamps | yes | the usage log |
| Product (desktop, terminal, Cowork) and Claude Code version | yes | the sheet and the disclosure checklist |
| Name of the folder the session worked in | yes | the sheet |
| Git branch and pull request number of the session | yes | the sheet, to suggest a task; the log, only as part of a task id you imported (pass `--no-infer` to leave them out) |
| Session title (desktop) or names of files a Cowork session produced | yes, unless you pass `--no-titles` | the sheet only, never the log |
| Your prompts, Claude's answers, tool inputs, file contents | never | nowhere |

The sheet can contain session titles and branch names, so treat it as private. The log
and labels files hold no prompt, answer or title. They do hold the task names you
imported (a suggested one is built from the folder and branch names), model names, tool
names (which include the names of your MCP servers) and the notes you typed, so read
them once before sharing them with the numbers. `SECURITY.md` has the full inventory.

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

Install cost-per-task once, version 0.7.0 or later (`cpt --version` tells):

```
python -m pip install cost-per-task
```

The price tables come with it. To work from a copy of the repository instead, run
`python -m pip install -e .` in its folder. Either way, the commands below run from any
folder.

## Quick path

Six commands, run from a folder of your own (they write their files there), in
PowerShell or any terminal. `cpt` and `python -m cost_per_task.cli` are the same
command. Under each one, what you should see.

1. **The overview, before any labelling.**
   ```
   cpt sessions summary --since 2026-09-01 --subscription 180
   ```
   The number of sessions and model calls, the cost at API list prices, the tokens by
   class with the share read from cache, then four tables: by product, by model, by
   month with the cost as a multiple of your subscription, and the five most expensive
   sessions. Nothing is written to disk.

2. **The sheet.**
   ```
   cpt sessions list
   ```
   `wrote sessions.csv: N rows`. One row per session: what is left to do first, newest
   first. Prefer the terminal to a spreadsheet? Skip steps 2 to 4 and run
   `cpt sessions label` instead: see [Labelling in the terminal](#labelling-in-the-terminal).
   Prefer clicking? `cpt sessions page` writes the same sheet as a page for your browser:
   see [Labelling in a browser page](#labelling-in-a-browser-page).

3. **Label in Excel.** Open `sessions.csv`. The `task` column is already filled in from
   the issue number, pull request or git branch of each session; correct any that is
   wrong (the same name for sessions that attempted the same job). For the sessions you
   want to measure, fill `outcome` (`pass` or `fail`), `task_type` (for example `code`,
   `analysis`, `writing`) and, if you later found an accepted result wrong, `leaked`
   (`yes`). Save as CSV and close Excel.

4. **The import.**
   ```
   cpt sessions import
   ```
   `imported N sessions (M model calls) into cpt-log.jsonl; N labels written to
   cpt-labels.jsonl`, followed by the report command with `--harness` already filled in.
   Copy that command.

5. **The report.** The printed command, or:
   ```
   cpt report --cleanup-cost 25 --seed 1
   ```
   The attempts, then one row per task (attempts, passes, fails, cost, cost to the first
   pass; `cpt tasks` prints that table alone), then one group per model and task type. `CPT_solved` is the headline: the cost per solved
   task, failed attempts included. With few sessions the intervals are wide; that is the
   tool telling you how little the sample proves. The report prints to the screen; keep it
   with `cpt report --cleanup-cost 25 --seed 1 | Out-File -Encoding utf8 report.txt`
   (PowerShell) or `> report.txt` elsewhere, or add `--json`.

6. **Where one session's money went.** The first characters of its id, from the sheet or
   the summary, are enough:
   ```
   cpt explain --attempt 18645423
   ```
   The cost by token class, by model when several were used, and the most expensive steps
   with their tools.

What to look for: fresh input is a rounding error; the cost is cache writes (tool
outputs added to the context), cache reads (the whole history re-read at every call) and
output, which includes thinking. Long sessions cost far more than short ones on the same
model, high effort levels show up as output, and P90 is the budgeting figure, not the
mean.

## First look: what your sessions would cost

Before labelling anything:

```
python -m cost_per_task.cli sessions summary --since 2026-09-01 --subscription 180
```

This reads every transcript and prints the sessions active on or after the date: their
number, their model calls, the tokens by class with the share read from cache, and the cost
at API list prices, split by product (Claude Code desktop, Claude Code CLI, Cowork), by
model and by month, then the five most expensive sessions. With `--subscription`, the
monthly price of your plan, each month also shows the list-price cost as a multiple of it:
`5.8x` means the month's usage would have cost 5.8 subscriptions on the API.

`--by day` or `--by week` changes the period, `--top 10` lengthens the last table and
`--top 0` drops it. Sessions are kept whole: a session started before the date and still
active after it appears with all its calls, and its earlier calls land in their own
period rows. Read the month row for a calendar month.

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
| `--no-infer` | leave the `task` column empty instead of suggesting one, and the `branch` column with it |
| `--new` | a short sheet: only the sessions since last time (see below) |
| `--min-calls 4` | leave out sessions with fewer than 4 model calls, which are rarely a task |
| `--log FILE` | the log that says what is already imported, when it is not `cpt-log.jsonl` |
| `--out other.csv` | write the sheet somewhere else |
| `--path FOLDER` | read transcripts from this folder instead of the usual places |

Running it again later adds new sessions and keeps everything you typed.

**Since last time.** Every run says how many sessions are new: not in the log, and still
active after the last session that is. With `--new` the sheet holds only those, plus any
row whose entries have not been imported yet, so a weekly pass is twenty rows and not
five hundred. Rows already in the log are left out of that short sheet; their outcomes
live in `cpt-labels.jsonl` and can still be changed with `cpt label`. For a first pass,
when nothing is in the log yet, use `--since` instead.

## Step 2: fill in the sheet

Open `sessions.csv` in Excel. Most columns come from the tool; leave them as they
are. `status` comes first and orders the rows: `new` (first time in the sheet), `open`
(seen before, still no outcome), `labelled` (has an outcome, not imported yet), `imported`
(in the log) and `gone` (its transcript was deleted). What helps you recognise a session
(`started`, `minutes`, `project`, `branch`, `title`, `calls`, cost) sits left of what you
fill in; ids and other reference columns are at the far right. Five columns are yours:

| Column | What to write |
|---|---|
| `task` | a short name for the job. Give the same name to every session that attempted the same job. It comes pre-filled (see below); overwrite it whenever you know better. |
| `task_type` | a category such as `coding`, `writing` or `analysis`, so the report compares like with like |
| `outcome` | `pass` if you accepted the result, `fail` if you gave up or had to redo it. `ok`, `yes`, `oui` and `passed` read as pass; `ko`, `no`, `non` and `failed` as fail. Any other word is reported with its row number and the row stays unlabelled. |
| `leaked` | `yes` if you accepted the result and later found it was wrong |
| `note` | anything you want to remember |

Save it as CSV. Excel in any language is fine, including versions that save with
semicolons or in the older Windows character set.

**The suggested task.** Typing a task for every session does not survive weekly use, so
the tool suggests one from what the session already knows. `task_source` says which
signal it used, strongest first:

| `task_source` | The task is | Example |
|---|---|---|
| `issue` | the issue number that opens the branch name | branch `fix/123-login` gives `shop/issue-123` |
| `pr` | the pull request the session worked on | `shop/pr-14` |
| `branch` | the git branch, unless it is `main`, `master` or similar | `shop/docs/quick-path` |
| `session` | the session on its own, when there is nothing better: project, day and the start of its id | `shop/2026-09-10-1a2b3c4d` |
| `manual` | what you typed | |

Three rules keep the guess honest. The outcome is never guessed: pass or fail is your
call. A session whose task is still the suggested one and that has no outcome was never
looked at, so the import leaves it out (`--include-unlabelled` brings it in, to measure
cost without a success rate). And the report's disclosure checklist states how many
tasks were stated by hand and how many were inferred, by signal. The suggestion never
merges two sessions on a hunch: a `session` task stands alone until you give two sessions
the same name, because a false merge would count unrelated work as retries of one task.
Session titles are never used for the suggestion, since a task id goes to the log.

Two tips. A session that did one job measures best; a session that mixed several
jobs counts as one attempt at whichever task you name. And label only sessions you
can judge honestly: an outcome you are unsure of is better left empty.

## Labelling in the terminal

```
python -m cost_per_task.cli sessions label --new
```

This replaces the sheet, Excel and the import with one command. It shows the sessions
that have no outcome yet, oldest first, one at a time:

```
[3/18]  shop  2026-09-19 14:02  41 min  23 calls  12.40 USD  claude-fable-5-1
       task: shop/pr-14 (pr)
       branch: docs/quick-path
       title: Quick path for the guide
       >
```

Type one letter and Enter:

| Key | Effect |
|---|---|
| `p`, `f` | pass or fail (the words `ok`, `ko`, `oui`, `non` work too); the answer is saved at once |
| `l` | leak: you accepted the result and it turned out wrong |
| `s` | skip this session; it comes back next time unless `--new` leaves it behind |
| `t` | type another task name |
| `m` | same task as the session you labelled just before, which is how a retry joins its task |
| `y`, `n` | set the task type, add a note |
| `q` | stop; everything answered so far is kept |

Each answer goes straight to `cpt-log.jsonl` and `cpt-labels.jsonl`, through the same
code as the sheet import, so the two ways can be mixed. Titles are shown on screen to
jog your memory and are written nowhere; `--no-titles` hides them. The options
`--since`, `--new`, `--min-calls`, `--no-infer`, `--source` and `--path` work as they do
for `cpt sessions list`. The command ends by printing the report command.

## Labelling in a browser page

```
python -m cost_per_task.cli sessions page --new
```

This writes `sessions.html`, one file that you open by double-clicking it. It shows the
same rows as the sheet, with a drop-down list for the outcome, a tick box for a leak,
task names and task types that complete as you type, and:

- **filters**: a search box over project, branch, title, task and note, and lists for
  status, project, product and outcome (`without an outcome` is the to-do list);
- **sorting**: click a column heading, click again to reverse;
- **changes in bulk**: tick rows (the box in the heading ticks every row shown), then
  apply one task name or one outcome to all of them. That is the quick way to give two
  sessions the same task when the second was a retry of the first.

When you are done, press **Save sessions.csv**. Chrome and Edge ask where to save: choose
the folder of your log. Other browsers put the file in your downloads folder. Then:

```
python -m cost_per_task.cli sessions import            # sessions.csv in the current folder
python -m cost_per_task.cli sessions import PATH\sessions.csv
```

The file it saves is the labelling sheet, so the page, Excel and `cpt sessions list` can
be mixed: the page starts from what `sessions.csv` already holds (`--sheet` names another
file). The options of `cpt sessions list` apply: `--since`, `--new`, `--min-calls`,
`--no-titles`, `--no-infer`, `--source`, `--path`, and `--out` for the name of the page.

**What the page can and cannot do.** There is no server: the page is a file. It loads
nothing from the network, not even a font, and it cannot connect to anything: its content
security policy forbids every connection and lets only the tool's own script run,
identified by its hash, so text hidden in a session title can neither run nor send
anything. It stores nothing in the browser: what you enter lives in the tab until you
save, and the page warns you before you close it with unsaved changes. It does hold your
session titles and branch names, like the sheet: treat `sessions.html` as private and
delete it when you are done.

## Step 3: import and read the report

```
python -m cost_per_task.cli sessions import
```

This adds the usage of every session you gave a task or an outcome to `cpt-log.jsonl`, and your
outcomes to `cpt-labels.jsonl`, the files every other command reads by default, so the
report needs no file options:

```
python -m cost_per_task.cli report --cleanup-cost 25 --seed 1
```

The import prints this command for you, with `--harness` already filled in from the
products and Claude Code versions it saw; keep that part, because the disclosure
checklist needs it. `--cleanup-cost` is what one leaked failure costs you to repair, in
the currency of the price table; 25 is a placeholder. Running the import again adds only
sessions not yet imported, and updates outcomes you changed; a task name changed on an
imported session is ignored with a warning, so to rename a task delete the two files and
import again. The report is read exactly as in the [testing guide](testing-guide.md).

Version 0.5.0 wrote `sessions-log.jsonl` and `sessions-labels.jsonl`. A folder that holds
only those keeps using them, and the import says so; rename them to the new names to run
the report with no options.

To see where one session's cost went, by token class, by model and by step:

```
python -m cost_per_task.cli explain --attempt 18645423
```

The first characters of the session id are enough when they match one session.

To compare two models on real work, label sessions of the same `task_type` done on
each model, then:

```
python -m cost_per_task.cli compare claude-fable-5 claude-fable-5-1 --seed 1
```

A session that switched models is attributed to the model with the largest share of
its cost; the sheet shows it in the `main_model` column.

## What the cost means

The figures are what the same work would cost at Anthropic's API list prices, from a
dated table. On a Pro, Max, Team or Enterprise subscription you pay a flat fee, not per
token, so this is a shadow cost: useful to compare models and kinds of task, to see
what a subscription is worth to you, or to budget a move to the API. If you run Claude
Code with an API key, it is your real cost before any negotiated discount.

Fast mode is priced: each response says which speed served it, and a fast response takes
the model's fast rates (Opus 5.5, Opus 5 and Opus 4.8 in the shipped table, at about twice
the standard rates). A fast response on a model without fast rates is priced at the
standard rates and the summary says so on its `speed` line. Not modelled, and reported when
detected: a priority service tier and data residency surcharges, both of which cost more
than list price. Negotiated discounts are not modelled either. Long context is not a gap:
Anthropic bills Claude 4.6 and later models at
the standard rate over the whole 1M-token window (pricing page, read on 2026-09-14), so a
session whose prompts grow past 200K tokens is priced correctly.

Web searches are not in the transcripts. Claude Code's WebSearch and WebFetch tools run
through separate requests that the transcripts do not record: on one computer, 402
WebSearch calls appeared among the tool names while every usage block reported zero
server-side searches. Their tokens and the API's web search fee (10 USD per 1,000
searches; web fetch has no fee beyond tokens) are therefore missing from a session's cost,
which is a floor by that amount.

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
