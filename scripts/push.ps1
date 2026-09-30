# push.ps1 — stage, commit and push this repo in one step.
#
# Usage (from PowerShell):
#   .\scripts\push.ps1 "fix the tone picker"
#   .\scripts\push.ps1                 # uses a timestamped message
#   .\scripts\push.ps1 -DryRun         # show what would be committed, change nothing
#
# Or, to get a `gpush` command in every new terminal, add this to your
# PowerShell profile (notepad $PROFILE):
#   function gpush { & "C:\Users\ADMIN\Documents\GitHub\nghe expanded\scripts\push.ps1" @args }
#
# Why this exists: the repo path has a space in it, so most tools need quoting.
# This script resolves the repo root from its own location, so it works no
# matter which directory you run it from, and refuses to run if it would not
# be pushing exactly what git is tracking.

param(
    [Parameter(Position = 0)]
    [string]$Message,

    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# Resolve the repo root from the script's own path (scripts/ -> repo root)
$repo = Split-Path -Parent $PSScriptRoot

# Fail loudly if this isn't a git repo, rather than half-staging something
if (-not (Test-Path (Join-Path $repo '.git'))) {
    throw "Not a git repository: $repo"
}

if (-not $Message) {
    $Message = "Update {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm')
}

Write-Host "repo:   $repo" -ForegroundColor Cyan
Write-Host "remote: $(git -C $repo remote get-url origin)" -ForegroundColor Cyan

# Show what is about to go in. `audio/` is gitignored, so the ~19,000 clip
# files never appear here — if you expect audio in a commit, get a human take
# installed via the original repo's Action instead.
$changed = git -C $repo status --porcelain
if (-not $changed) {
    Write-Host "Nothing to commit - working tree is clean." -ForegroundColor Yellow
    exit 0
}

Write-Host "`nChanges to be committed:" -ForegroundColor Cyan
$changed | ForEach-Object { Write-Host "  $_" }

if ($DryRun) {
    Write-Host "`n-DryRun: nothing staged, nothing committed." -ForegroundColor Yellow
    exit 0
}

git -C $repo add -A
git -C $repo commit -m $Message

if ($LASTEXITCODE -ne 0) {
    throw "commit failed (exit $LASTEXITCODE)"
}

git -C $repo push

if ($LASTEXITCODE -ne 0) {
    throw "push failed (exit $LASTEXITCODE) - commit is local only. If this is an auth error, run 'git push' in your own terminal so Git Credential Manager can authenticate."
}

Write-Host "`nPushed." -ForegroundColor Green
git -C $repo log --oneline -1
