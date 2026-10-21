$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$ollamaExe = Join-Path $projectRoot "tools\ollama\runtime\ollama.exe"
$pidPath = Join-Path $projectRoot "data\ollama\ollama.pid"

if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Output "Ollama PID file does not exist; nothing to stop."
    exit 0
}

$ollamaPid = [int](Get-Content -LiteralPath $pidPath)
$process = Get-Process -Id $ollamaPid -ErrorAction SilentlyContinue
if ($null -eq $process) {
    Remove-Item -LiteralPath $pidPath -Force
    Write-Output "Ollama is not running; removed stale PID file."
    exit 0
}

$expectedPath = [IO.Path]::GetFullPath($ollamaExe)
$actualPath = [IO.Path]::GetFullPath($process.Path)
if ($process.ProcessName -ne "ollama" -or $actualPath -ne $expectedPath) {
    throw "PID $ollamaPid is not the project Ollama process; refusing to stop it."
}

Stop-Process -Id $ollamaPid -Force
Remove-Item -LiteralPath $pidPath -Force
Write-Output "Stopped project Ollama process $ollamaPid."
