# Publish only the reviewed project files through your normal GitHub Git login.
# This never uploads local config.json, state.json, caches, or Git metadata.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'Install Git for Windows before publishing.'
}

# This folder was created by the Codex sandbox. Trust this exact project for
# these commands without changing Git's global safe-directory configuration.
$projectSafeDirectory = 'safe.directory=' + $PSScriptRoot.Replace('\', '/')
function Invoke-ProjectGit {
    param([string[]]$GitArguments)
    & git -c $projectSafeDirectory @GitArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'Git failed. Nothing is force-pushed; review the error above.'
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.git'))) {
    Invoke-ProjectGit -GitArguments @('init', '-b', 'main')
}

$currentBranch = & git -c $projectSafeDirectory branch --show-current
if ($LASTEXITCODE -ne 0 -or $currentBranch -ne 'main') {
    throw 'Publish from the main branch only.'
}

$expectedOrigin = 'https://github.com/natycodes/ovh-vps3-monitor.git'
$remoteNames = & git -c $projectSafeDirectory remote
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to inspect Git remotes.'
}
if ($remoteNames -notcontains 'origin') {
    Invoke-ProjectGit -GitArguments @('remote', 'add', 'origin', $expectedOrigin)
} else {
    $currentOrigin = & git -c $projectSafeDirectory remote get-url origin
    if ($LASTEXITCODE -ne 0 -or $currentOrigin -ne $expectedOrigin) {
        throw 'Origin points to another repository; nothing was uploaded.'
    }
}

$publicFiles = @(
    '.github/workflows/monitor.yml', '.gitignore', 'README.md',
    'ovh_vps3_monitor.py', 'run.ps1', 'deploy.ps1', 'config.example.json',
    'config/monitor.json', 'data/state.json',
    'tests/test_monitor.py', 'tests/fixtures/stock.json'
)
Invoke-ProjectGit -GitArguments (@('add', '--') + $publicFiles)
Invoke-ProjectGit -GitArguments @('diff', '--cached', '--check')
$stagedPaths = & git -c $projectSafeDirectory diff --cached --name-only
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to inspect staged file names.'
}
foreach ($stagedPath in $stagedPaths) {
    if ($stagedPath -notin $publicFiles) {
        throw ('An unexpected file is staged; nothing was published: ' + $stagedPath)
    }
}
if ($stagedPaths) {
    Invoke-ProjectGit -GitArguments @(
        '-c', 'user.name=natycodes',
        '-c', 'user.email=327937009+natycodes@users.noreply.github.com',
        'commit', '-m', 'Run OVH VPS-3 stock checks every five minutes on GitHub Actions'
    )
}

Invoke-ProjectGit -GitArguments @('push', '-u', 'origin', 'main')
Write-Output 'Published. The first GitHub Actions run should start automatically.'
Write-Output 'https://github.com/natycodes/ovh-vps3-monitor/actions'
