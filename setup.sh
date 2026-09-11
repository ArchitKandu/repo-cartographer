#!/usr/bin/env bash
#
# One-command setup for the monorepo.
#
#   ./setup.sh            install everything, create env files, apply migrations
#   ./setup.sh --check    change nothing; just report what is and is not ready
#
# Safe to run repeatedly. Nothing here overwrites a file you already have: env
# files are only created when missing, and the migrations are written to be
# applied twice (see backend/db/001_runs.sql).
#
# Windows users: this runs under Git Bash, which ships with Git for Windows.
# There is a PowerShell equivalent in setup.ps1 if you would rather stay native.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK_ONLY=false
[ "${1:-}" = "--check" ] && CHECK_ONLY=true

bold() { printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$1"; }
die()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
#
# Checked before anything is installed, so a missing tool is one clear message
# rather than a failure halfway through a dependency tree.

bold "Prerequisites"

if command -v uv >/dev/null 2>&1; then
  ok "uv $(uv --version | awk '{print $2}')"
else
  die "uv is not installed. It manages Python and the backend's dependencies.
        macOS/Linux:  curl -LsSf https://astral.sh/uv/install.sh | sh
        Windows:      powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"
        Then reopen your shell and run this again."
fi

if command -v node >/dev/null 2>&1; then
  NODE_MAJOR="$(node --version | sed 's/^v//' | cut -d. -f1)"
  if [ "$NODE_MAJOR" -ge 20 ]; then
    ok "node $(node --version)"
  else
    die "node $(node --version) is too old — Next.js needs 20 or newer. https://nodejs.org"
  fi
else
  die "node is not installed. The frontend needs it. https://nodejs.org"
fi

command -v npm >/dev/null 2>&1 || die "npm is not installed (it normally ships with node)."
ok "npm $(npm --version)"

# ---------------------------------------------------------------------------
# Environment files
# ---------------------------------------------------------------------------
#
# Created from their templates when absent and never touched otherwise — this
# script must be safe to re-run against a machine that is already configured,
# and the one thing that would make it unsafe is clobbering real credentials.

bold "Environment files"

seed_env() {
  local target="$1" template="$2" label="$3"
  if [ -f "$target" ]; then
    ok "$label exists (left alone)"
  elif [ "$CHECK_ONLY" = true ]; then
    warn "$label is missing — run without --check to create it from the template"
  elif [ -f "$template" ]; then
    cp "$template" "$target"
    warn "$label created from the template — open it and fill in your keys"
  else
    warn "$label is missing and so is its template ($template)"
  fi
}

seed_env "$ROOT/backend/.env" "$ROOT/backend/.env.example" "backend/.env"
seed_env "$ROOT/frontend/.env.local" "$ROOT/frontend/.env.local.example" "frontend/.env.local"

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

if [ "$CHECK_ONLY" = false ]; then
  bold "Backend dependencies"
  (cd "$ROOT/backend" && uv sync --quiet)
  ok "backend/.venv is in sync with uv.lock"

  bold "Frontend dependencies"
  # From the root, so npm resolves the workspace declared in package.json rather
  # than treating frontend/ as an unrelated project.
  (cd "$ROOT" && npm install --silent)
  ok "node_modules installed"
fi

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
#
# Skipped rather than failed when POSTGRESQL_URI is absent. On a fresh clone the
# .env was created moments ago and is still a template, and stopping here would
# hide the summary that explains what to do about it.

bold "Database"

if grep -qs '^POSTGRESQL_URI=.\+' "$ROOT/backend/.env"; then
  if [ "$CHECK_ONLY" = true ]; then
    ok "POSTGRESQL_URI is set (migrations not applied in --check mode)"
  else
    (cd "$ROOT/backend" && uv run scripts/apply_migrations.py >/dev/null)
    ok "migrations applied"
  fi
else
  warn "POSTGRESQL_URI is not set in backend/.env — skipping migrations.
        Use Supabase's *session pooler* URI from Dashboard -> Connect.
        The direct db.<ref>.supabase.co host is IPv6-only and unreachable from
        most networks. Then re-run this script."
fi

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
#
# The part that earns the script. Everything above can succeed against an .env
# full of placeholders; this connects and finds out.

bold "Checking configuration"
set +e
(cd "$ROOT/backend" && uv run scripts/check_env.py)
CHECK_STATUS=$?
set -e

bold "Next steps"
cat <<'EOF'
  Two terminals, or `npm run dev` to start both at once:

    npm run dev:api     the Python API on http://localhost:8000
    npm run dev:web     the Next.js app on http://localhost:3000

  Useful:

    npm run test:api                       253 tests, no model or network
    npm run lint:api                       ruff and mypy
    cd backend && uv run main.py "..."     the CLI, no database required
    cd backend && uv run scripts/check_env.py    re-run the checks above
EOF

exit $CHECK_STATUS
