# One-command setup for the monorepo, for Windows PowerShell.
#
#   .\setup.ps1            install everything, create env files, apply migrations
#   .\setup.ps1 -Check     change nothing; just report what is and is not ready
#
# A line-for-line companion to setup.sh rather than a different design, so that a
# problem reported on one platform is recognisable on the other. Everything real
# happens in `uv`, `npm`, and backend/scripts/check_env.py, which both scripts
# call — the checking logic is Python precisely so it does not have to be written
# twice and drift.
#
# Safe to run repeatedly: env files are only created when missing, and the
# migrations are written to be applied twice.

[CmdletBinding()]
param([switch]$Check)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Head($text) { Write-Host ""; Write-Host $text -ForegroundColor White }
function Write-Ok($text)   { Write-Host "  ok    $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "  warn  $text" -ForegroundColor Yellow }
function Stop-With($text)  { Write-Host "  FAIL  $text" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

Write-Head "Prerequisites"

if (Get-Command uv -ErrorAction SilentlyContinue) {
    # Index 1, not -Last: `uv --version` prints "uv 0.12.9 (hash date)", so the
    # last token is the trailing half of the build stamp rather than the version.
    Write-Ok "uv $((uv --version) -split ' ' | Select-Object -Index 1)"
} else {
    Stop-With @"
uv is not installed. It manages Python and the backend's dependencies.
        powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
        Then reopen your shell and run this again.
"@
}

if (Get-Command node -ErrorAction SilentlyContinue) {
    $nodeMajor = [int](((node --version) -replace '^v', '') -split '\.')[0]
    if ($nodeMajor -ge 20) {
        Write-Ok "node $(node --version)"
    } else {
        Stop-With "node $(node --version) is too old - Next.js needs 20 or newer. https://nodejs.org"
    }
} else {
    Stop-With "node is not installed. The frontend needs it. https://nodejs.org"
}

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Stop-With "npm is not installed (it normally ships with node)."
}
Write-Ok "npm $(npm --version)"

# ---------------------------------------------------------------------------
# Environment files
# ---------------------------------------------------------------------------

Write-Head "Environment files"

function Initialize-EnvFile($target, $template, $label) {
    if (Test-Path $target) {
        Write-Ok "$label exists (left alone)"
    } elseif ($Check) {
        Write-Warn "$label is missing - run without -Check to create it from the template"
    } elseif (Test-Path $template) {
        Copy-Item $template $target
        Write-Warn "$label created from the template - open it and fill in your keys"
    } else {
        Write-Warn "$label is missing and so is its template ($template)"
    }
}

Initialize-EnvFile "$Root\backend\.env" "$Root\backend\.env.example" "backend/.env"
Initialize-EnvFile "$Root\frontend\.env.local" "$Root\frontend\.env.local.example" "frontend/.env.local"

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

if (-not $Check) {
    Write-Head "Backend dependencies"
    Push-Location "$Root\backend"
    try { uv sync --quiet; if ($LASTEXITCODE -ne 0) { Stop-With "uv sync failed" } }
    finally { Pop-Location }
    Write-Ok "backend/.venv is in sync with uv.lock"

    Write-Head "Frontend dependencies"
    # From the root, so npm resolves the workspace declared in package.json.
    Push-Location $Root
    try { npm install --silent; if ($LASTEXITCODE -ne 0) { Stop-With "npm install failed" } }
    finally { Pop-Location }
    Write-Ok "node_modules installed"
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

Write-Head "Database"

$envFile = "$Root\backend\.env"
$hasUri = (Test-Path $envFile) -and ((Get-Content $envFile) -match '^POSTGRESQL_URI=.+')

if ($hasUri) {
    if ($Check) {
        Write-Ok "POSTGRESQL_URI is set (migrations not applied in -Check mode)"
    } else {
        Push-Location "$Root\backend"
        try {
            uv run scripts/apply_migrations.py | Out-Null
            if ($LASTEXITCODE -ne 0) { Stop-With "migrations failed" }
        } finally { Pop-Location }
        Write-Ok "migrations applied"
    }
} else {
    Write-Warn @"
POSTGRESQL_URI is not set in backend/.env - skipping migrations.
        Use Supabase's *session pooler* URI from Dashboard -> Connect.
        The direct db.<ref>.supabase.co host is IPv6-only and unreachable from
        most networks. Then re-run this script.
"@
}

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

Write-Head "Checking configuration"
Push-Location "$Root\backend"
try { uv run scripts/check_env.py; $checkStatus = $LASTEXITCODE }
finally { Pop-Location }

Write-Head "Next steps"
@"
  Two terminals, or ``npm run dev`` to start both at once:

    npm run dev:api     the Python API on http://localhost:8000
    npm run dev:web     the Next.js app on http://localhost:3000

  Useful:

    npm run test:api                       253 tests, no model or network
    npm run lint:api                       ruff and mypy
    cd backend; uv run main.py "..."       the CLI, no database required
    cd backend; uv run scripts/check_env.py      re-run the checks above
"@ | Write-Host

exit $checkStatus
