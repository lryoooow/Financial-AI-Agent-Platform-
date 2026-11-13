param([int]$Port = 0, [string]$BindAddress = "")
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $Root
try {
    pnpm --dir fronted install --frozen-lockfile --ignore-scripts
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    pnpm --dir fronted build
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    if ($Port -gt 0) { $env:PORT = [string]$Port }
    if ($BindAddress) { $env:FIN_HOST = $BindAddress }
    $env:FIN_WEB = '1'
    $env:FRONTEND_DIST = Join-Path $Root 'fronted/dist'
    if (Test-Path -LiteralPath '.env' -PathType Leaf) {
        uv run --project backend --locked --no-dev --extra server --env-file .env python backend/run_web.py
    } else {
        uv run --project backend --locked --no-dev --extra server python backend/run_web.py
    }
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
