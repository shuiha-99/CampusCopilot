$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "ollama_env.ps1")

$projectRoot = Split-Path -Parent $PSScriptRoot
$ollamaExe = Join-Path $projectRoot "tools\ollama\runtime\ollama.exe"
$ollamaRoot = Join-Path $projectRoot "data\ollama"
$stdoutPath = Join-Path $ollamaRoot "logs\server.out.log"
$stderrPath = Join-Path $ollamaRoot "logs\server.err.log"
$pidPath = Join-Path $ollamaRoot "ollama.pid"
$ollamaPort = 11434

function Test-OllamaPort {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $pending = $client.BeginConnect("127.0.0.1", $ollamaPort, $null, $null)
        return $pending.AsyncWaitHandle.WaitOne(500) -and $client.Connected
    }
    finally {
        $client.Dispose()
    }
}

if (Test-OllamaPort) {
    Write-Output "Ollama is already listening on http://127.0.0.1:$ollamaPort"
    exit 0
}

if (-not (Test-Path -LiteralPath $ollamaExe)) {
    throw "Ollama was not found at $ollamaExe"
}

$ollamaProcess = Start-Process `
    -FilePath $ollamaExe `
    -ArgumentList @("serve") `
    -WorkingDirectory (Split-Path -Parent $ollamaExe) `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru
$ollamaProcess.Id | Set-Content -LiteralPath $pidPath -Encoding ascii

for ($attempt = 0; $attempt -lt 60; $attempt++) {
    Start-Sleep -Milliseconds 250
    if (Test-OllamaPort) {
        Write-Output "Ollama started on http://127.0.0.1:$ollamaPort (PID $($ollamaProcess.Id))"
        Write-Output "Models: $env:OLLAMA_MODELS"
        exit 0
    }
    if ($ollamaProcess.HasExited) {
        break
    }
}

if (-not $ollamaProcess.HasExited) {
    Stop-Process -Id $ollamaProcess.Id -Force
}
throw "Ollama did not start; inspect $stderrPath"
