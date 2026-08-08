<#
.SYNOPSIS
    The Makefile targets, for machines without make.

.DESCRIPTION
    Windows does not ship make, and neither will most people evaluating this.
    Telling them to install a 2006-era GnuWin32 build to run a demo is a worse
    answer than not depending on make in the first place, so every target in the
    Makefile has an equivalent here.

    Same names, same behaviour:

        .\demo.ps1 reset
        .\demo.ps1 baseline
        .\demo.ps1 break
        .\demo.ps1 blast-radius

.EXAMPLE
    .\demo.ps1 help
#>
param(
    [Parameter(Position = 0)]
    [string]$Target = "help"
)

$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"
# Keeps the verdict boxes from wrapping in a narrow terminal.
if (-not $env:COLUMNS) { $env:COLUMNS = "95" }

$FRAUD = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,fraud_detector_v3,PROD)"
$CHURN = "urn:li:mlModel:(urn:li:dataPlatform:sagemaker,churn_predictor_v1,PROD)"

function Show-Help {
    Write-Host ""
    Write-Host "Undertow demo targets" -ForegroundColor Cyan
    Write-Host ""
    $rows = @(
        @("demo-offline",     "The whole gate on a recorded graph. No DataHub needed."),
        @("seed",             "Build the fixture graph in DataHub"),
        @("baseline",         "Capture the approved state of both models"),
        @("break",            "Drop transaction_amount  (CERTAIN -> BLOCK)"),
        @("break-stats",      "Shift a distribution 4.8 sigma  (PROBABLE -> WARN)"),
        @("break-governance", "Deprecate upstream, tag staging PII  (BLOCK + WARN)"),
        @("check",            "Gate the fraud model"),
        @("check-churn",      "Gate the churn model"),
        @("blast-radius",     "Gate both - one column, two teams"),
        @("check-warn",       "Gate after drift: WARN, exit 0"),
        @("check-write",      "Gate and write the verdict back to DataHub"),
        @("check-mcp",        "Resolve through the DataHub MCP server"),
        @("check-investigate","Add the agent investigation loop"),
        @("history",          "Every recorded verdict, read back from DataHub"),
        @("impact",           "Check a proposed SQL change before it merges"),
        @("reset",            "Restore the graph"),
        @("record",           "Re-record the offline fixture from a live instance"),
        @("test",             "Run the test suite"),
        @("lint",             "ruff + mypy")
    )
    foreach ($r in $rows) { Write-Host ("  {0,-18} {1}" -f $r[0], $r[1]) }
    Write-Host ""
    Write-Host "  .\demo.ps1 <target>" -ForegroundColor DarkGray
    Write-Host ""
}

switch ($Target) {
    "help"             { Show-Help }
    "demo-offline"     { undertow demo }
    "seed"             { python scripts/seed_datahub.py }
    "baseline"         {
        undertow baseline --model "$FRAUD"
        undertow baseline --model "$CHURN"
    }
    "break"            { python scripts/break_schema.py }
    "break-stats"      { python scripts/drift_stats.py }
    "break-governance" { python scripts/break_governance.py }
    "check"            { undertow check --model "$FRAUD" }
    "check-churn"      { undertow check --model "$CHURN" }
    "check-warn"       { undertow check --model "$FRAUD" }
    "blast-radius"     {
        undertow check --model "$FRAUD"
        undertow check --model "$CHURN"
    }
    "check-write"      { undertow check --model "$FRAUD" --write-back }
    "check-mcp"        { undertow check --model "$FRAUD" --mcp }
    "check-investigate"{ undertow check --model "$FRAUD" --mcp --investigate }
    "history"          { undertow history --model "$FRAUD" }
    "impact"           { undertow impact examples/pr-drops-amount.sql }
    "reset"            { python scripts/reset_demo.py }
    "record"           { python scripts/record_fixture.py }
    "test"             {
        python -m pytest tests/ -q
        python -m pytest contrib/datahub-mlmodel-patch-builder/ -q
    }
    "lint"             {
        python -m ruff check src/ tests/ scripts/
        python -m mypy src/
    }
    default {
        Write-Host "Unknown target '$Target'." -ForegroundColor Red
        Show-Help
        exit 2
    }
}
