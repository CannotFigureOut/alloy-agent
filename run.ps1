$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (!(Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv was not found. Install uv and reopen your terminal."
}

# Always use this project's environment, even if another environment is active.
$env:UV_PROJECT_ENVIRONMENT = Join-Path $PSScriptRoot ".venv"
& uv sync --frozen --no-dev --python 3.12
if ($LASTEXITCODE -ne 0) { throw "uv environment synchronization failed" }
if (!(Test-Path .env)) { Copy-Item .env.example .env }
& uv run --no-sync python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
exit $LASTEXITCODE
