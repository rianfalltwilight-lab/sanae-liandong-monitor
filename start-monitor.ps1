$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Config = Get-Content -LiteralPath (Join-Path $Root 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$Python = Join-Path $Config.sanae_root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw 'Sanae Python not found' }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
# The worker's urllib openers explicitly use the native network path.
& $Python -X utf8 (Join-Path $Root 'shop_monitor.py') --config (Join-Path $Root 'config.json')
exit $LASTEXITCODE
