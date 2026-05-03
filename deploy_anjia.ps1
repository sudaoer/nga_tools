param(
    [string]$RemoteHost = "al",
    [string]$RemotePath = "/www/wwwroot/260502.anchor.gmgo.sudoer.cn",
    [string]$RemoteOwner = "www:www",
    [string]$RemoteMode = "755",
    [string]$Python = "python",
    [switch]$SkipCounter,
    [switch]$SkipBuild,
    [switch]$ForceRefresh
)

$ErrorActionPreference = "Stop"

function Invoke-Step {
    param(
        [string]$Title,
        [scriptblock]$Command
    )

    Write-Host ""
    Write-Host "==> $Title" -ForegroundColor Cyan
    & $Command
}

function Invoke-CheckedNative {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory = $PSScriptRoot
    )

    Push-Location $WorkingDirectory
    try {
        & $FilePath @ArgumentList
        if ($LASTEXITCODE -ne 0) {
            throw "$FilePath exited with code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

$repoRoot = $PSScriptRoot
$viewerDir = Join-Path $repoRoot "anjia-viewer"
$dataPath = Join-Path $viewerDir "public\data\anchors_43877379.json"
$distDir = Join-Path $viewerDir "dist"
$remoteStage = ".anjia-viewer-deploy-$((Get-Date).ToString('yyyyMMddHHmmss'))"
$pixiPython = Join-Path $repoRoot ".pixi\envs\default\python.exe"

if ($Python -eq "python" -and (Test-Path -LiteralPath $pixiPython)) {
    $Python = $pixiPython
}

if (-not (Test-Path -LiteralPath $viewerDir)) {
    throw "Viewer directory not found: $viewerDir"
}

if (-not $SkipCounter) {
    $counterArgs = @("AnchorCounter.py", "--output", $dataPath)
    if ($ForceRefresh) {
        $counterArgs += "--force-refresh"
    }
    Invoke-Step "Run anchor counter" {
        Invoke-CheckedNative -FilePath $Python -ArgumentList $counterArgs -WorkingDirectory $repoRoot
    }
}

if (-not $SkipBuild) {
    Invoke-Step "Build anjia-viewer" {
        Invoke-CheckedNative -FilePath "npm" -ArgumentList @("run", "build") -WorkingDirectory $viewerDir
    }
}

if (-not (Test-Path -LiteralPath $distDir)) {
    throw "Build output not found: $distDir"
}

$remotePrepare = "rm -rf '$remoteStage' && mkdir -p '$remoteStage' && sudo mkdir -p '$RemotePath'"
$remotePublish = "sudo cp -R '$remoteStage'/.' '$RemotePath'/ && sudo chown -R '$RemoteOwner' '$RemotePath' && sudo chmod -R '$RemoteMode' '$RemotePath' && rm -rf '$remoteStage'"

Invoke-Step "Prepare remote staging on $RemoteHost" {
    Invoke-CheckedNative -FilePath "ssh" -ArgumentList @($RemoteHost, $remotePrepare) -WorkingDirectory $repoRoot
}

Invoke-Step "Upload dist by scp to ${RemoteHost}:$remoteStage" {
    Invoke-CheckedNative -FilePath "scp" -ArgumentList @("-r", ".", "${RemoteHost}:$remoteStage/") -WorkingDirectory $distDir
}

Invoke-Step "Fix remote owner and mode" {
    Invoke-CheckedNative -FilePath "ssh" -ArgumentList @($RemoteHost, $remotePublish) -WorkingDirectory $repoRoot
}

Write-Host ""
Write-Host "Deploy complete: ${RemoteHost}:$RemotePath" -ForegroundColor Green
