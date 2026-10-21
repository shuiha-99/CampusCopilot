$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $projectRoot "config\memurai-dev.conf"
$localServerPathFile = Join-Path $projectRoot "config\cache-server.path"
$dataPath = Join-Path $projectRoot "data\memurai"
$logPath = Join-Path $dataPath "memurai.log"
$pidPath = Join-Path $dataPath "memurai.pid"
$cachePort = 6380

$serverCandidates = @()
if ($env:CAMPUS_CACHE_SERVER) {
    $serverCandidates += $env:CAMPUS_CACHE_SERVER
}
if (Test-Path -LiteralPath $localServerPathFile) {
    $serverCandidates += (Get-Content -LiteralPath $localServerPathFile -Raw).Trim()
}
$projectRedis = Join-Path $projectRoot "tools\redis\redis-server.exe"
if (Test-Path -LiteralPath $projectRedis) {
    $serverCandidates += $projectRedis
}
$pathRedis = Get-Command redis-server.exe -ErrorAction SilentlyContinue
if ($pathRedis) {
    $serverCandidates += $pathRedis.Source
}
$serverCandidates += "C:\Program Files\Memurai\memurai.exe"
$cacheServerExe = $serverCandidates |
    Where-Object { $_ -and (Test-Path -LiteralPath $_) } |
    Select-Object -First 1

function Test-CachePort {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $pending = $client.BeginConnect("127.0.0.1", $cachePort, $null, $null)
        return $pending.AsyncWaitHandle.WaitOne(300) -and $client.Connected
    }
    finally {
        $client.Dispose()
    }
}

if (Test-CachePort) {
    Write-Output "CampusCopilot cache is already listening on 127.0.0.1:$cachePort"
    exit 0
}

if (-not $cacheServerExe) {
    throw "No Redis-compatible server was found. Set CAMPUS_CACHE_SERVER or config/cache-server.path."
}

New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
$cacheProcess = Start-Process `
    -FilePath $cacheServerExe `
    -ArgumentList @($configPath) `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -PassThru
$cacheProcess.Id | Set-Content -LiteralPath $pidPath -Encoding ascii

for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 200
    if (Test-CachePort) {
        Write-Output "CampusCopilot cache started on 127.0.0.1:$cachePort (PID $($cacheProcess.Id))"
        Write-Output "Server: $cacheServerExe"
        exit 0
    }
    if ($cacheProcess.HasExited) {
        break
    }
}

if (-not $cacheProcess.HasExited) {
    Stop-Process -Id $cacheProcess.Id -Force
}
throw "CampusCopilot cache did not start with $cacheServerExe; inspect $logPath."
