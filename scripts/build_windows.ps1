param([string]$OutputDir = "", [string]$BuildRoot = "")
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $OutputDir) { $OutputDir = Join-Path $Root 'fronted/release' }
if (-not $BuildRoot) { $BuildRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('jingyuan-build-' + [guid]::NewGuid().ToString('N')) }
$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
$BuildRoot = [System.IO.Path]::GetFullPath($BuildRoot)
Push-Location $Root
try {
    $Version = (Get-Content -Raw -LiteralPath 'fronted/package.json' | ConvertFrom-Json).version
    $ZipName = "JingYuan-$Version-win-x64-portable.zip"
    $Zip = Join-Path $OutputDir $ZipName
    foreach ($Target in @($Zip, (Join-Path $OutputDir "JingYuan-Setup-$Version.exe"), (Join-Path $OutputDir "JingYuan-Portable-$Version.exe"))) {
        if (Test-Path -LiteralPath $Target) { throw "Release file already exists: $Target" }
    }
    New-Item -ItemType Directory -Force -Path $OutputDir, $BuildRoot | Out-Null
    if (-not $env:UV_PROJECT_ENVIRONMENT) { $env:UV_PROJECT_ENVIRONMENT = Join-Path $BuildRoot 'venv' }
    pnpm --dir fronted install --frozen-lockfile --ignore-scripts
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    pnpm --dir fronted typecheck
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    $env:JINGYUAN_FRONTEND_DIST = Join-Path $BuildRoot 'web'
    pnpm --dir fronted build --outDir $env:JINGYUAN_FRONTEND_DIST
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    $PythonDist = Join-Path $BuildRoot 'python'
    uv run --project backend --locked --extra packaging --python 3.13 pyinstaller backend/packaging/finresearch.spec --noconfirm --distpath $PythonDist --workpath (Join-Path $BuildRoot 'pyinstaller')
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    uv run --project backend --locked --python 3.13 python scripts/smoke_sidecar.py (Join-Path $PythonDist 'FinResearch/FinResearch.exe')
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    $Candidate = Join-Path $BuildRoot 'electron'
    node scripts/package_desktop.cjs win x64 (Join-Path $PythonDist 'FinResearch') $Candidate
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    $Unpacked = Join-Path $Candidate 'win-unpacked'
    New-Item -ItemType File -Path (Join-Path $Unpacked '.portable') | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $CandidateZip = Join-Path $Candidate $ZipName
    [System.IO.Compression.ZipFile]::CreateFromDirectory($Unpacked, $CandidateZip, [System.IO.Compression.CompressionLevel]::Optimal, $false)
    $Names = @("JingYuan-Setup-$Version.exe", "JingYuan-Portable-$Version.exe", $ZipName)
    foreach ($Name in $Names) { Copy-Item -LiteralPath (Join-Path $Candidate $Name) -Destination $OutputDir }
    Write-Output $Names
} finally {
    Pop-Location
}
