r"""The labelling sheet as one self-contained HTML page (``cpt sessions page``).

For people who would rather click than type: the same rows as ``sessions.csv``,
with drop-down lists, filters, sorting and bulk edits, saved back as the same CSV
for ``cpt sessions import``. There is no server and no dependency: the page is a
file, opened from disk.

The page holds session titles and branch names, so it is built to be unable to
leak them:

- it loads nothing: no script, style, font or image comes from outside the file
- it can connect to nothing: the Content-Security-Policy is ``default-src 'none'``
  and allows only this module's own script and style, by SHA-256 hash, so even
  markup smuggled in through a session title could not run or phone home
- text from transcripts reaches the DOM through ``textContent`` only, and the data
  block is JSON with ``<``, ``>`` and ``&`` escaped
- nothing is stored in the browser; entries live in the tab until they are saved,
  and saving is a save dialog or a plain download
- cells that would open as a formula in Excel get the same apostrophe guard as
  ``write_sheet`` gives them

The template lives in this module, not in a data file, so that it cannot be left
out of a wheel.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from .importers.claude_sessions import _YES, SHEET_COLUMNS
from .labels import _FAIL_WORDS, _PASS_WORDS

STYLE = r"""
:root {
  --bg: #ffffff; --fg: #1c1c1c; --muted: #5f6368; --line: #d8d8d8; --head: #f3f4f6;
  --accent: #0b57d0; --field: #ffffff; --row: #fafafa;
  --new: #0b57d0; --open: #8a5a00; --labelled: #18723a; --imported: #5f6368; --gone: #a50e0e;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161616; --fg: #e9e9e9; --muted: #a3a3a3; --line: #3a3a3a; --head: #222222;
    --accent: #8ab4f8; --field: #1f1f1f; --row: #1b1b1b;
    --new: #8ab4f8; --open: #f2c55c; --labelled: #7bd88f; --imported: #a3a3a3; --gone: #f28b82;
  }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 16px 20px 32px; background: var(--bg); color: var(--fg);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
h1 { font-size: 20px; margin: 0 0 4px; }
.lead { margin: 0 0 14px; color: var(--muted); max-width: 110ch; }
.bar { display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: end; margin: 0 0 10px;
  padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--head); }
.bar label { display: flex; flex-direction: column; gap: 3px; font-size: 12px; color: var(--muted); }
.bar .grow { flex: 1 1 220px; }
input[type=text], input[type=search], select { font: inherit; color: var(--fg); background: var(--field);
  border: 1px solid var(--line); border-radius: 6px; padding: 5px 7px; min-width: 0; }
input:disabled, select:disabled { opacity: .55; }
button { font: inherit; color: var(--fg); background: var(--field); border: 1px solid var(--line);
  border-radius: 6px; padding: 6px 12px; cursor: pointer; }
button.primary { background: var(--accent); border-color: var(--accent); color: var(--bg); font-weight: 600; }
button:focus-visible, input:focus-visible, select:focus-visible, th:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 1px; }
#counts { margin: 6px 2px 8px; color: var(--muted); }
.wrap { overflow: auto; max-height: calc(100vh - 300px); min-height: 240px; border: 1px solid var(--line);
  border-radius: 8px; }
table { border-collapse: collapse; width: 100%; }
th, td { padding: 6px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
thead th { position: sticky; top: 0; z-index: 1; background: var(--head); font-size: 12px;
  white-space: nowrap; user-select: none; }
th.sortable { cursor: pointer; }
th[aria-sort=ascending]::after { content: " \2191"; }
th[aria-sort=descending]::after { content: " \2193"; }
tbody tr:nth-child(even) { background: var(--row); }
tr.done td { border-bottom-color: var(--labelled); }
td.num, th.num { text-align: right; white-space: nowrap; }
td.when { white-space: nowrap; }
.sub { display: block; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
td.title { min-width: 180px; max-width: 320px; overflow-wrap: anywhere; }
td.task input { width: 230px; } td.type input { width: 110px; } td.note input { width: 200px; }
.badge { font-size: 12px; font-weight: 600; }
.st-new { color: var(--new); } .st-open { color: var(--open); } .st-labelled { color: var(--labelled); }
.st-imported { color: var(--imported); } .st-gone { color: var(--gone); }
.save { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-top: 12px; }
#saved { color: var(--muted); }
code { font-family: ui-monospace, Consolas, monospace; font-size: 13px; }
.empty { padding: 24px; color: var(--muted); }
"""

BODY = r"""
<h1>Label your sessions</h1>
<p class="lead">This page is a file on your computer. It loads nothing from the network and cannot
send anything: its content security policy blocks every connection. What you enter stays in this
tab until you save it, so save before you close. Generated <span id="generated"></span>.</p>

<div class="bar" role="search" aria-label="Filters">
  <label class="grow">Search project, branch, title, task or note
    <input type="search" id="f-search" autocomplete="off"></label>
  <label>Status <select id="f-status"></select></label>
  <label>Project <select id="f-project"></select></label>
  <label>Product <select id="f-product"></select></label>
  <label>Outcome <select id="f-outcome">
    <option value="">all</option><option value="none">without an outcome</option>
    <option value="pass">pass</option><option value="fail">fail</option></select></label>
  <button type="button" id="f-reset">Clear filters</button>
</div>

<div class="bar" aria-label="Change the selected rows">
  <label class="grow">Task for the selected rows
    <input type="text" id="b-task" list="task-names" autocomplete="off"></label>
  <button type="button" id="b-task-apply">Apply task</button>
  <label>Outcome for the selected rows <select id="b-outcome">
    <option value="">no outcome</option><option value="pass">pass</option>
    <option value="fail">fail</option></select></label>
  <button type="button" id="b-outcome-apply">Apply outcome</button>
</div>

<p id="counts" role="status" aria-live="polite"></p>

<div class="wrap">
<table id="grid">
  <thead><tr>
    <th><input type="checkbox" id="pick-all" aria-label="Select every row shown"></th>
    <th class="sortable" data-key="status" tabindex="0">Status</th>
    <th class="sortable" data-key="started" tabindex="0">Started</th>
    <th class="sortable num" data-key="minutes" tabindex="0">Min</th>
    <th class="sortable" data-key="project" tabindex="0">Project and branch</th>
    <th class="sortable" data-key="title" tabindex="0">Title</th>
    <th class="sortable num" data-key="calls" tabindex="0">Calls</th>
    <th class="sortable num" data-key="api_cost_usd" tabindex="0">Cost <span id="currency"></span></th>
    <th class="sortable" data-key="task" tabindex="0">Task</th>
    <th>Type</th>
    <th class="sortable" data-key="outcome" tabindex="0">Outcome</th>
    <th>Leaked</th>
    <th>Note</th>
  </tr></thead>
  <tbody></tbody>
</table>
<p class="empty" id="empty" hidden>No session matches the filters.</p>
</div>

<div class="save">
  <button type="button" class="primary" id="save">Save sessions.csv</button>
  <span id="saved" role="status" aria-live="polite">Save the file next to your log, then run
    <code>cpt sessions import</code>.</span>
</div>

<datalist id="task-names"></datalist>
<datalist id="type-names"></datalist>
"""

SCRIPT = r"""
(() => {
  "use strict";
  const data = JSON.parse(document.getElementById("cpt-data").textContent);
  const rows = data.rows;
  const FORMULA = /^[=+\-@\t\r]/;
  const NUMERIC = new Set(["minutes", "calls", "api_cost_usd"]);
  const $ = (id) => document.getElementById(id);
  const tbody = $("grid").querySelector("tbody");
  let dirty = false;
  let sortKey = "";
  let sortDir = 1;

  // Text from transcripts only ever enters the page through textContent.
  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function option(value, label) {
    const node = make("option", "", label === undefined ? value : label);
    node.value = value;
    return node;
  }

  function word(text) {
    return String(text || "").trim().toLowerCase();
  }

  function sourceOf(row) {
    if (!row.task) return "";
    return row.suggested_task && row.task === row.suggested_task ? row.suggested_source : "manual";
  }

  function fixedTask(row) {
    return row.status === "imported";
  }

  function renderRow(row) {
    const typed = word(row.outcome);
    if (data.pass_words.includes(typed)) row.outcome = "pass";
    else if (data.fail_words.includes(typed)) row.outcome = "fail";
    row.leakedFlag = data.yes_words.includes(word(row.leaked));

    const tr = make("tr");
    const what = "the session started " + row.started;
    row.tr = tr;

    row.pick = make("input");
    row.pick.type = "checkbox";
    row.pick.setAttribute("aria-label", "Select " + what);
    row.pick.addEventListener("change", counts);
    tr.appendChild(make("td")).appendChild(row.pick);

    tr.appendChild(make("td")).appendChild(make("span", "badge st-" + row.status, row.status));
    tr.appendChild(make("td", "when", row.started));
    tr.appendChild(make("td", "num", row.minutes));
    const project = tr.appendChild(make("td", "", row.project || row.product));
    if (row.branch) project.appendChild(make("span", "sub", row.branch));
    tr.appendChild(make("td", "title", row.title));
    tr.appendChild(make("td", "num", row.calls));
    tr.appendChild(make("td", "num", row.api_cost_usd));

    const taskCell = tr.appendChild(make("td", "task"));
    row.taskInput = make("input");
    row.taskInput.type = "text";
    row.taskInput.value = row.task || "";
    row.taskInput.setAttribute("list", "task-names");
    row.taskInput.setAttribute("autocomplete", "off");
    row.taskInput.setAttribute("aria-label", "Task of " + what);
    if (fixedTask(row)) {
      row.taskInput.disabled = true;
      row.taskInput.title = "Already in the log under this task: the name can no longer change.";
    }
    row.sourceTag = make("span", "sub", sourceOf(row));
    row.taskInput.addEventListener("input", () => {
      row.task = row.taskInput.value.trim();
      touched(row);
    });
    row.taskInput.addEventListener("change", names);
    taskCell.appendChild(row.taskInput);
    taskCell.appendChild(row.sourceTag);

    const typeInput = make("input");
    typeInput.type = "text";
    typeInput.value = row.task_type || "";
    typeInput.setAttribute("list", "type-names");
    typeInput.setAttribute("autocomplete", "off");
    typeInput.setAttribute("aria-label", "Task type of " + what);
    typeInput.addEventListener("input", () => {
      row.task_type = typeInput.value.trim();
      touched(row);
    });
    typeInput.addEventListener("change", names);
    tr.appendChild(make("td", "type")).appendChild(typeInput);

    row.outcomeSelect = make("select");
    row.outcomeSelect.setAttribute("aria-label", "Outcome of " + what);
    row.outcomeSelect.appendChild(option("", "-"));
    row.outcomeSelect.appendChild(option("pass"));
    row.outcomeSelect.appendChild(option("fail"));
    if (row.outcome && row.outcome !== "pass" && row.outcome !== "fail") {
      row.outcomeSelect.appendChild(option(row.outcome, row.outcome + " (as typed)"));
    }
    row.outcomeSelect.value = row.outcome || "";
    row.outcomeSelect.addEventListener("change", () => {
      row.outcome = row.outcomeSelect.value;
      touched(row);
    });
    tr.appendChild(make("td")).appendChild(row.outcomeSelect);

    row.leakBox = make("input");
    row.leakBox.type = "checkbox";
    row.leakBox.checked = row.leakedFlag;
    row.leakBox.title = "You accepted the result and later found it wrong. Only a pass can leak.";
    row.leakBox.setAttribute("aria-label", "Leaked: " + what);
    row.leakBox.addEventListener("change", () => {
      row.leakedFlag = row.leakBox.checked;
      touched(row);
    });
    tr.appendChild(make("td")).appendChild(row.leakBox);

    const noteInput = make("input");
    noteInput.type = "text";
    noteInput.value = row.note || "";
    noteInput.setAttribute("aria-label", "Note on " + what);
    noteInput.addEventListener("input", () => {
      row.note = noteInput.value;
      touched(row);
    });
    tr.appendChild(make("td", "note")).appendChild(noteInput);

    paint(row);
    tbody.appendChild(tr);
  }

  // Keep a row's derived display in step with its values.
  function paint(row) {
    row.leakBox.disabled = row.outcome !== "pass";
    row.sourceTag.textContent = fixedTask(row) ? "in the log" : sourceOf(row);
    row.tr.classList.toggle("done", Boolean(row.outcome));
  }

  function touched(row) {
    dirty = true;
    paint(row);
    counts();
  }

  function names() {
    const fill = (id, values) => {
      const list = $(id);
      list.textContent = "";
      Array.from(new Set(values.filter(Boolean))).sort().forEach((value) => list.appendChild(option(value)));
    };
    fill("task-names", rows.map((row) => row.task));
    fill("type-names", data.task_types.concat(rows.map((row) => row.task_type)));
  }

  function shown(row) {
    return !row.tr.hidden;
  }

  function counts() {
    const visible = rows.filter(shown);
    const judged = rows.filter((row) => row.outcome === "pass" || row.outcome === "fail");
    const passed = judged.filter((row) => row.outcome === "pass").length;
    const picked = rows.filter((row) => row.pick.checked && shown(row)).length;
    $("counts").textContent =
      "Showing " + visible.length + " of " + rows.length + " sessions. With an outcome: " + judged.length +
      " (pass " + passed + ", fail " + (judged.length - passed) + "). Selected: " + picked + "." +
      (dirty ? " Unsaved changes." : "");
    $("empty").hidden = visible.length > 0 || rows.length === 0;
    $("pick-all").checked = visible.length > 0 && picked === visible.length;
  }

  function filter() {
    const text = word($("f-search").value);
    const status = $("f-status").value;
    const project = $("f-project").value;
    const product = $("f-product").value;
    const outcome = $("f-outcome").value;
    rows.forEach((row) => {
      const hay = [row.project, row.branch, row.title, row.task, row.note, row.session_id].join(" ").toLowerCase();
      const keep =
        (!text || hay.includes(text)) &&
        (!status || row.status === status) &&
        (!project || row.project === project) &&
        (!product || row.product === product) &&
        (!outcome || (outcome === "none" ? !row.outcome : row.outcome === outcome));
      row.tr.hidden = !keep;
    });
    counts();
  }

  function choices(id, key) {
    const select = $(id);
    select.appendChild(option("", "all"));
    Array.from(new Set(rows.map((row) => row[key]).filter(Boolean))).sort().forEach((value) => {
      select.appendChild(option(value));
    });
    select.addEventListener("change", filter);
  }

  function sortBy(key) {
    sortDir = sortKey === key ? -sortDir : 1;
    sortKey = key;
    const value = (row) => (NUMERIC.has(key) ? parseFloat(row[key]) || 0 : String(row[key] || "").toLowerCase());
    rows
      .slice()
      .sort((a, b) => (value(a) < value(b) ? -sortDir : value(a) > value(b) ? sortDir : 0))
      .forEach((row) => tbody.appendChild(row.tr));
    document.querySelectorAll("th.sortable").forEach((th) => {
      if (th.dataset.key === key) th.setAttribute("aria-sort", sortDir > 0 ? "ascending" : "descending");
      else th.removeAttribute("aria-sort");
    });
  }

  // The CSV that cpt sessions import reads: the same columns, guard and line ends as the tool writes.
  function exported(row, column) {
    if (column === "leaked") return row.leakedFlag && row.outcome === "pass" ? "yes" : "";
    if (column === "task_source") return sourceOf(row);
    if (column === "status") {
      return row.outcome && row.status !== "imported" && row.status !== "gone" ? "labelled" : row.status;
    }
    return row[column] === undefined || row[column] === null ? "" : String(row[column]);
  }

  function cell(value) {
    const text = FORMULA.test(value) ? "'" + value : value;
    return /[",\r\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
  }

  function toCsv() {
    const lines = ["sep=,", data.columns.join(",")];
    rows.forEach((row) => lines.push(data.columns.map((column) => cell(exported(row, column))).join(",")));
    return "\ufeff" + lines.join("\r\n") + "\r\n";
  }

  async function save() {
    const text = toCsv();
    let where = "";
    if (window.showSaveFilePicker) {
      try {
        const handle = await window.showSaveFilePicker({
          suggestedName: "sessions.csv",
          types: [{ description: "CSV", accept: { "text/csv": [".csv"] } }],
        });
        const writable = await handle.createWritable();
        await writable.write(text);
        await writable.close();
        where = handle.name;
      } catch (error) {
        if (error && error.name === "AbortError") return;
      }
    }
    if (!where) {
      const link = make("a");
      link.href = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
      link.download = "sessions.csv";
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
      where = "sessions.csv, in your downloads folder";
    }
    dirty = false;
    counts();
    const saved = $("saved");
    saved.textContent = "Saved as " + where + ". Next, in your terminal: ";
    saved.appendChild(make("code", "", "cpt sessions import <that file>"));
  }

  // Exposed for checks from a console or a test harness; the page itself does not need them.
  window.cptPage = { toCsv, rows };

  $("generated").textContent = data.generated;
  $("currency").textContent = data.currency;
  rows.forEach(renderRow);
  names();
  choices("f-status", "status");
  choices("f-project", "project");
  choices("f-product", "product");
  $("f-search").addEventListener("input", filter);
  $("f-outcome").addEventListener("change", filter);
  $("f-reset").addEventListener("click", () => {
    ["f-search", "f-status", "f-project", "f-product", "f-outcome"].forEach((id) => { $(id).value = ""; });
    filter();
  });
  $("pick-all").addEventListener("change", () => {
    rows.filter(shown).forEach((row) => { row.pick.checked = $("pick-all").checked; });
    counts();
  });
  $("b-task-apply").addEventListener("click", () => {
    const task = $("b-task").value.trim();
    rows.filter((row) => row.pick.checked && shown(row) && !fixedTask(row)).forEach((row) => {
      row.task = task;
      row.taskInput.value = task;
      touched(row);
    });
    names();
  });
  $("b-outcome-apply").addEventListener("click", () => {
    const outcome = $("b-outcome").value;
    rows.filter((row) => row.pick.checked && shown(row)).forEach((row) => {
      row.outcome = outcome;
      row.outcomeSelect.value = outcome;
      touched(row);
    });
  });
  document.querySelectorAll("th.sortable").forEach((th) => {
    th.addEventListener("click", () => sortBy(th.dataset.key));
    th.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        sortBy(th.dataset.key);
      }
    });
  });
  $("save").addEventListener("click", save);
  window.addEventListener("beforeunload", (event) => {
    if (dirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  if (rows.length === 0) {
    $("empty").textContent = "No session to show.";
    $("empty").hidden = false;
  }
  counts();
})();
"""

TASK_TYPES = ["coding", "writing", "analysis", "research", "review", "other"]


def _hash(text: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")


def content_security_policy() -> str:
    """Nothing may load and nothing may connect; only this module's own script and style run."""
    return (
        f"default-src 'none'; script-src '{_hash(SCRIPT)}'; style-src '{_hash(STYLE)}'; "
        "base-uri 'none'; form-action 'none'"
    )


def _embed(payload: dict) -> str:
    """JSON that cannot close its script element or open a comment, whatever a title holds."""
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_page(
    rows: list[dict],
    *,
    suggestions: dict[str, tuple[str, str]] | None = None,
    currency: str = "",
    generated: str = "",
) -> str:
    """The page for these sheet rows. ``suggestions`` maps a session id to its suggested
    (task, source), so that the page can tell a suggested task from a typed one."""
    suggestions = suggestions or {}
    payload_rows = []
    for row in rows:
        task, source = suggestions.get(row.get("session_id", ""), ("", ""))
        payload_rows.append({**{c: row.get(c, "") for c in SHEET_COLUMNS},
                             "suggested_task": task, "suggested_source": source})
    payload = {
        "columns": SHEET_COLUMNS,
        "rows": payload_rows,
        "currency": currency,
        "generated": generated,
        "task_types": TASK_TYPES,
        "pass_words": sorted(_PASS_WORDS),
        "fail_words": sorted(_FAIL_WORDS),
        "yes_words": sorted(_YES),
    }
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<meta http-equiv=\"Content-Security-Policy\" content=\"{content_security_policy()}\">\n"
        "<meta name=\"referrer\" content=\"no-referrer\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>cost-per-task: label your sessions</title>\n"
        f"<style>{STYLE}</style>\n</head>\n<body>\n{BODY}\n"
        f"<script type=\"application/json\" id=\"cpt-data\">{_embed(payload)}</script>\n"
        f"<script>{SCRIPT}</script>\n</body>\n</html>\n"
    )


def write_page(path: str | Path, html: str) -> None:
    # Bytes, not text mode: the hashes in the policy are those of the exact script and style.
    Path(path).write_bytes(html.encode("utf-8"))
