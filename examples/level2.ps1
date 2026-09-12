# Level 2 test: three small coding tasks solved by Claude Code in print mode,
# each attempt in a fresh copy of the task folder, judged by the task's tests.
#
#   .\examples\level2.ps1 -Model claude-fable-5-1
#   .\examples\level2.ps1 -Model claude-fable-5
#
# Harness: Claude Code in --bare mode (no personal hooks, memory, CLAUDE.md
# files or MCP servers, so both models get exactly the same setup), effort
# pinned, and a spending cap per attempt. Each attempt costs real money; see
# docs/testing-guide.md for the budget. Windows PowerShell 5.1.

param(
    [Parameter(Mandatory = $true)] [string] $Model,
    [int] $Attempts = 3,
    [ValidateSet("low", "medium", "high", "xhigh", "max")] [string] $Effort = "high",
    [string] $MaxBudgetUsd = "5",
    [string] $Log = "level2-log.jsonl",
    [string] $Labels = "level2-labels.jsonl",
    [string] $Scratch = "$env:TEMP\cpt-level2"
)

if (-not $env:ANTHROPIC_API_KEY) {
    Write-Error "Set `$env:ANTHROPIC_API_KEY first (a Console API key)."
    exit 1
}
$repo = (Get-Location).Path
$taskRoot = Join-Path $repo "examples\level2"
if (-not (Test-Path $taskRoot)) {
    Write-Error "Run this from the cost-per-task folder."
    exit 1
}
function Resolve-RepoPath([string] $Path) {
    if ([System.IO.Path]::IsPathRooted($Path)) { return $Path }
    return (Join-Path $repo $Path)
}
$logPath = Resolve-RepoPath $Log
$labelsPath = Resolve-RepoPath $Labels
$claudeVersion = ((claude --version) -replace '\s*\(Claude Code\)', '').Trim()
$harness = "Claude Code $claudeVersion, -p --bare, effort $Effort, cap $MaxBudgetUsd USD per attempt"

$passes = 0
$total = 0
$tasks = Get-ChildItem -Path $taskRoot -Directory
foreach ($task in $tasks) {
    $prompt = Get-Content -Raw (Join-Path $task.FullName "TASK.md")
    for ($i = 1; $i -le $Attempts; $i++) {
        $attemptId = "a" + (Get-Date).ToUniversalTime().ToString("yyyyMMdd'T'HHmmss'Z'") + "-" + ("{0:x4}" -f (Get-Random -Maximum 65536))
        $work = Join-Path $Scratch "$($task.Name)-$Model-$i"
        if (Test-Path $work) { Remove-Item -Recurse -Force $work }
        New-Item -ItemType Directory -Path $work | Out-Null
        Copy-Item -Path (Join-Path $task.FullName "*") -Destination $work -Recurse

        Write-Host ""
        Write-Host "== $($task.Name) attempt $i of $Attempts on $Model  ($work)" -ForegroundColor Cyan
        Push-Location $work
        try {
            python -m cost_per_task.cli run --task-id $task.Name --attempt-id $attemptId --task-type coding --log $logPath -- `
                claude -p $prompt --model $Model --effort $Effort --bare --strict-mcp-config --no-session-persistence `
                --permission-mode acceptEdits --allowedTools "Read,Write,Edit,Glob,Grep,Bash(python:*)" `
                --max-budget-usd $MaxBudgetUsd
            $agentExit = $LASTEXITCODE
            python -m pytest -q -p no:cacheprovider 2>&1 | Select-Object -Last 3
            $testExit = $LASTEXITCODE
        } finally {
            Pop-Location
        }

        $captured = (Test-Path $logPath) -and (Select-String -Path $logPath -Pattern $attemptId -SimpleMatch -Quiet)
        if (-not $captured) {
            Write-Warning "no API calls were captured for this attempt (Claude Code exit code $agentExit); not labelled"
            continue
        }
        if ($agentExit -ne 0) {
            Write-Warning "Claude Code exited with $agentExit (for example the budget cap); the tests decide the label"
        }
        $total++
        if ($testExit -eq 0) {
            python -m cost_per_task.cli label pass --task $task.Name --attempt $attemptId --labels $labelsPath
            $passes++
        } else {
            python -m cost_per_task.cli label fail --task $task.Name --attempt $attemptId --labels $labelsPath
        }
    }
}

Write-Host ""
Write-Host "$Model : $passes of $total labelled attempts passed. Report now; comparison once both models have run:" -ForegroundColor Green
Write-Host "  python -m cost_per_task.cli report --log $Log --labels $Labels --prices prices\anthropic.json --seed 1 --harness `"$harness`""
Write-Host "  python -m cost_per_task.cli compare claude-fable-5 claude-fable-5-1 --log $Log --labels $Labels --prices prices\anthropic.json --seed 1 --harness `"$harness`""
