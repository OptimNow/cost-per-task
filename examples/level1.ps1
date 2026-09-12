# Level 1 test: five simple questions with known answers, asked several times
# each, captured and priced through the cpt proxy and labelled automatically.
#
#   .\examples\level1.ps1 -Model claude-fable-5-1
#   .\examples\level1.ps1 -Model claude-fable-5
#
# Run it once per model. Both runs write to the same log and labels files, so
# the report can compare the two models side by side. Windows PowerShell 5.1.

param(
    [Parameter(Mandatory = $true)] [string] $Model,
    [int] $Attempts = 3,
    [string] $Log = "level1-log.jsonl",
    [string] $Labels = "level1-labels.jsonl"
)

if (-not $env:ANTHROPIC_API_KEY) {
    Write-Error "Set `$env:ANTHROPIC_API_KEY first (a Console API key)."
    exit 1
}

$tasks = @(
    @{ Id = "arith";   Prompt = "What is 17 * 23? Reply with only the number.";                                                                                  Expect = "391" },
    @{ Id = "invoice"; Prompt = "Extract the invoice number from this sentence and reply with only that number: 'Invoice INV-2026-0413 was paid on 3 March.'"; Expect = "INV-2026-0413" },
    @{ Id = "days";    Prompt = "How many days are there from 1 January 2026 to 1 March 2026? Reply with only the number.";                                       Expect = "59" },
    @{ Id = "json";    Prompt = "Return a JSON object with the keys city and country for Budapest. Reply with only the JSON.";                                    Expect = "Hungary" },
    @{ Id = "sort";    Prompt = "Sort these words alphabetically and reply with them comma-separated and nothing else: pear, apple, fig, banana";                Expect = "apple, banana, fig, pear" }
)

$passes = 0
$total = 0
foreach ($task in $tasks) {
    for ($i = 1; $i -le $Attempts; $i++) {
        $total++
        Write-Host ""
        Write-Host "== $($task.Id) attempt $i of $Attempts on $Model" -ForegroundColor Cyan
        python -m cost_per_task.cli run --task-id $task.Id --task-type simple --log $Log -- `
            python examples\ask.py --model $Model --expect $task.Expect $task.Prompt
        if ($LASTEXITCODE -eq 0) {
            python -m cost_per_task.cli label pass --task $task.Id --log $Log --labels $Labels
            $passes++
        } elseif ($LASTEXITCODE -eq 1) {
            python -m cost_per_task.cli label fail --task $task.Id --log $Log --labels $Labels
        } else {
            Write-Warning "attempt did not reach the model (exit code $LASTEXITCODE); not labelled"
        }
    }
}

Write-Host ""
Write-Host "$Model : $passes of $total attempts passed. Now run:" -ForegroundColor Green
Write-Host "  python -m cost_per_task.cli report --log $Log --labels $Labels --prices prices\anthropic.json --seed 1"
