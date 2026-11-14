# Uses Codex's bundled Python on this computer, or py -3 elsewhere.
$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'ovh_vps3_monitor.py'
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if ($env:OVH_MONITOR_PYTHON) {
    & $env:OVH_MONITOR_PYTHON $scriptPath @args
} elseif (Test-Path -LiteralPath $bundledPython) {
    & $bundledPython $scriptPath @args
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $scriptPath @args
} else {
    Write-Error 'Install Python 3.10+ or set OVH_MONITOR_PYTHON to its executable path.'
}
exit $LASTEXITCODE
