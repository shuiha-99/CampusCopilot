$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "ollama_env.ps1")

$projectRoot = Split-Path -Parent $PSScriptRoot
$ollamaExe = Join-Path $projectRoot "tools\ollama\runtime\ollama.exe"

if (-not (Test-Path -LiteralPath $ollamaExe)) {
    throw "Ollama was not found at $ollamaExe"
}

& $ollamaExe @args
exit $LASTEXITCODE
