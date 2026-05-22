# Publish OpenGate to GitHub via REST API (UTF-8 safe).
param(
    [string]$Owner = 'Burashka44',
    [string]$Repo = 'OpenGate',
    [string]$Branch = 'main',
    [switch]$CreateRepo,
    [switch]$PushOnly
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

function Get-GitHubToken {
    if ($env:GITHUB_PERSONAL_ACCESS_TOKEN) { return $env:GITHUB_PERSONAL_ACCESS_TOKEN }
    $mcpPath = Join-Path $env:USERPROFILE '.cursor\mcp.json'
    if (Test-Path $mcpPath) {
        $cfg = Get-Content $mcpPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $t = $cfg.mcpServers.'github-agent'.env.GITHUB_PERSONAL_ACCESS_TOKEN
        if ($t) { return $t }
    }
    throw 'Set GITHUB_PERSONAL_ACCESS_TOKEN or configure github-agent in ~/.cursor/mcp.json'
}

function Invoke-GitHubApi {
    param([string]$Method, [string]$Uri, [object]$Body)
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    $headers = @{
        Authorization = "Bearer $(Get-GitHubToken)"
        Accept        = 'application/vnd.github+json'
        'User-Agent'  = 'OpenGate-publish'
    }
    if ($null -ne $Body) {
        $json = $Body | ConvertTo-Json -Compress -Depth 20
        $bytes = $utf8NoBom.GetBytes($json)
        return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $headers -Body $bytes -ContentType 'application/json; charset=utf-8'
    }
    return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $headers
}

$Description = (Get-Content (Join-Path $PSScriptRoot 'repo-description.txt') -Raw -Encoding UTF8).Trim()
$root = Split-Path $PSScriptRoot -Parent

if ($CreateRepo -and -not $PushOnly) {
    try {
        $created = Invoke-GitHubApi -Method Post -Uri 'https://api.github.com/user/repos' -Body @{
            name        = $Repo
            description = $Description
            private     = $false
            auto_init   = $false
        }
        Write-Host "Repository created: $($created.html_url)"
    } catch {
        $msg = $_.ErrorDetails.Message
        if ($msg -notmatch 'name already exists') {
            Write-Error "Cannot create repository. Add Account Administration (read/write) to PAT, or create empty repo at https://github.com/new?name=$Repo and run with -PushOnly. API: $msg"
        } else {
            Write-Host 'Repository already exists, continuing...'
        }
    }
}

try {
    Invoke-GitHubApi -Method Get -Uri "https://api.github.com/repos/$Owner/$Repo" | Out-Null
} catch {
    throw "Repository $Owner/${Repo} not found. Create it on GitHub first, then run with -PushOnly"
}

Push-Location $root
$files = @(git ls-files)
if ($files.Count -eq 0) { throw 'No tracked files (git ls-files empty)' }

Write-Host "Uploading $($files.Count) files..."

$tree = @()
foreach ($rel in $files) {
    $path = Join-Path $root $rel
    $bytes = [System.IO.File]::ReadAllBytes($path)
    $b64 = [Convert]::ToBase64String($bytes)
    $blob = Invoke-GitHubApi -Method Post -Uri "https://api.github.com/repos/$Owner/$Repo/git/blobs" -Body @{
        content  = $b64
        encoding = 'base64'
    }
    $tree += @{ path = ($rel -replace '\\', '/'); mode = '100644'; type = 'blob'; sha = $blob.sha }
    Write-Host "  blob $($rel -replace '\\', '/')"
}

$treeObj = Invoke-GitHubApi -Method Post -Uri "https://api.github.com/repos/$Owner/$Repo/git/trees" -Body @{ tree = $tree }

$commit = Invoke-GitHubApi -Method Post -Uri "https://api.github.com/repos/$Owner/$Repo/git/commits" -Body @{
    message = "OpenGate"
    tree    = $treeObj.sha
}

try {
    Invoke-GitHubApi -Method Patch -Uri "https://api.github.com/repos/$Owner/$Repo/git/refs/heads/$Branch" -Body @{
        sha   = $commit.sha
        force = $true
    }
} catch {
    Invoke-GitHubApi -Method Post -Uri "https://api.github.com/repos/$Owner/$Repo/git/refs" -Body @{
        ref = "refs/heads/$Branch"
        sha = $commit.sha
    }
}

Invoke-GitHubApi -Method Patch -Uri "https://api.github.com/repos/$Owner/$Repo" -Body @{ description = $Description } | Out-Null

Write-Host "Done: https://github.com/$Owner/$Repo"
Pop-Location
