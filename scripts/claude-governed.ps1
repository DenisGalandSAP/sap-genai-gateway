<#
.SYNOPSIS
  Launch Claude Code against the local governed gateway, in this shell only.

.DESCRIPTION
  Sets the ANTHROPIC_* variables for the current PowerShell session and starts
  Claude Code. Nothing is written to your user or machine environment, so any
  other Claude Code session you already have open is unaffected — which matters,
  because setting these globally would redirect a running session mid-task.

  Uses the scoped `claude-code-dev` virtual key from .env, not the proxy master
  key: limited to the two Claude aliases with a 5 CU / 30d budget (see D5).

  ANTHROPIC_DEFAULT_HAIKU_MODEL is not optional. Claude Code runs background
  work (titles, summaries) on a small model; without it those calls request a
  model the gateway does not expose and fail quietly while the main model looks
  fine.

.EXAMPLE
  .\scripts\claude-governed.ps1
#>

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root '.env'

if (-not (Test-Path $envFile)) {
    throw ".env not found at $envFile"
}

function Get-EnvValue([string]$name) {
    $line = Select-String -Path $envFile -Pattern "^$name=" | Select-Object -First 1
    if (-not $line) { return $null }
    # '$$' is the docker-compose escape for a literal '$' (D9); undo it here.
    return $line.Line.Split('=', 2)[1].Trim().Replace('$$', '$')
}

$key = Get-EnvValue 'CLAUDE_CODE_KEY'
if (-not $key) {
    throw "CLAUDE_CODE_KEY not in .env. Mint one:
  curl -X POST localhost:4000/key/generate -H `"Authorization: Bearer `$LITELLM_MASTER_KEY`" ``
    -d '{`"key_alias`":`"claude-code-dev`",`"models`":[`"claude-sonnet-governed`",`"claude-haiku-governed`"],`"max_budget`":5,`"budget_duration`":`"30d`"}'"
}

# Fail early with a clear message rather than letting Claude Code report an
# opaque connection error.
try {
    $health = Invoke-WebRequest -Uri 'http://localhost:4000/health/liveliness' `
        -UseBasicParsing -TimeoutSec 5
} catch {
    throw "Gateway not reachable at localhost:4000. Start it with: docker compose up -d"
}

$env:ANTHROPIC_BASE_URL            = 'http://localhost:4000'
$env:ANTHROPIC_AUTH_TOKEN          = $key
$env:ANTHROPIC_MODEL               = 'claude-sonnet-governed'
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = 'claude-haiku-governed'

Write-Host ''
Write-Host '  Claude Code -> SAP Gen AI Hub (governed)' -ForegroundColor Cyan
Write-Host '    base   : http://localhost:4000'
Write-Host '    model  : claude-sonnet-governed  (background: claude-haiku-governed)'
Write-Host "    key    : claude-code-dev  ($($key.Substring(0,9))...)"
Write-Host '    scope  : this shell only — other sessions unaffected'
Write-Host ''

claude @args
