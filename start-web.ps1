param(
    [string]$BindAddress = '127.0.0.1',
    [ValidateRange(1, 65535)][int]$Port = 8000
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create Python environment.' }
}
& $pythonPath -c "import importlib.util,sys; sys.exit(not all(importlib.util.find_spec(x) for x in ('fastapi', 'uvicorn', 'pypdf', 'openpyxl', 'defusedxml', 'charset_normalizer', 'striprtf')))"
if ($LASTEXITCODE -ne 0) {
    & $pythonPath -m pip install -e '.[web]'
    if ($LASTEXITCODE -ne 0) { throw 'Unable to install Web dependencies.' }
}
& $pythonPath -m evotask serve --host $BindAddress --port $Port
exit $LASTEXITCODE
