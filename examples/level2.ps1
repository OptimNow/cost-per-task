# Level 2 test: three small coding tasks solved by Claude Code in print mode,
# each attempt in a fresh copy of the task folder, judged by the task's tests.
#
#   .\examples\level2.ps1 -Model claude-fable-5-1
#   .\examples\level2.ps1 -Model claude-fable-5
#
# Each attempt costs real money (Claude Code writes a large system prompt into
# the prompt cache on every run). See docs/testing-guide.md for the budget.
# Windows PowerShell 5.1.

param(
    [Parameter(Mandatory = $true)] [string] $Model,
    [int] $Attempts = 3,
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
$tasks = Get-ChildItem -Path $taskRoot -Directory

foreach ($task in $tasks) {
    $prompt = Get-Content -Raw (Join-Path $task.FullName "TASK.md")
    for ($i = 1; $i -le $Attempts; $i++) {
        $work = Join-Path $Scratch "$($task.Name)-$Model-$i"
        if (Test-Path $work) { Remove-Item -Recurse -Force $work }
        New-Item -ItemType Directory -Path $work | Out-Null
        Copy-Item -Path (Join-Path $task.FullName "*") -Destination $work -Recurse

        Write-Host ""
        Write-Host "== $($task.Name) attempt $i of $Attempts on $Model  ($work)" -ForegroundColor Cyan
        Push-Location $work
        try {
            python -m cost_per_task.cli run --task-id $task.Name --task-type coding --log (Join-Path $repo $Log) -- `
                claude -p $prompt --model $Model --permission-mode acceptEdits `
                --allowedTools "Read,Write,Edit,Glob,Grep,Bash(python:*)"
            $agentExit = $LASTEXITCODE

            python -m pytest -q 2>&1 | Select-Object -Last 3
            $testExit = $LASTEXITCODE
        } finally {
            Pop-Location
        }

        if ($agentExit -ne 0) {
            Write-Warning "Claude Code exited with $agentExit; check the output above before labelling"
        }
        if ($testExit -eq 0) {
            python -m cost_per_task.cli label pass --task $task.Name --log $Log --labels $Labels
        } else {
            python -m cost_per_task.cli label fail --task $task.Name --log $Log --labels $Labels
        }
    }
}

Write-Host ""
Write-Host "Done. Now run:" -ForegroundColor Green
Write-Host "  python -m cost_per_task.cli report --log $Log --labels $Labels --prices prices\anthropic.json --seed 1 --harness `"Claude Code, -p mode, default effort`""
