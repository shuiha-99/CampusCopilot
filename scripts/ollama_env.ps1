$projectRoot = Split-Path -Parent $PSScriptRoot
$ollamaRoot = Join-Path $projectRoot "data\ollama"

$env:OLLAMA_HOST = "127.0.0.1:11434"
$env:OLLAMA_MODELS = Join-Path $ollamaRoot "models"
$env:OLLAMA_TMPDIR = Join-Path $ollamaRoot "tmp"
$env:OLLAMA_NOHISTORY = "1"
$env:OLLAMA_CONTEXT_LENGTH = "4096"
$env:OLLAMA_KEEP_ALIVE = "0"
$env:OLLAMA_MAX_LOADED_MODELS = "1"
$env:OLLAMA_NUM_PARALLEL = "1"
# This development machine has an Intel integrated GPU. Ollama ignores iGPUs by
# default, which forced the 7B model entirely into already constrained system
# memory. Allow Vulkan to use the shared-memory iGPU for local verification.
$env:OLLAMA_IGPU_ENABLE = "1"

# Ollama also keeps its generated local key under the Windows user profile.
# Give this process a project-local profile so no Ollama state is written to C:.
$env:USERPROFILE = Join-Path $ollamaRoot "profile"
$env:TEMP = $env:OLLAMA_TMPDIR
$env:TMP = $env:OLLAMA_TMPDIR

@(
    $ollamaRoot,
    $env:OLLAMA_MODELS,
    $env:OLLAMA_TMPDIR,
    $env:USERPROFILE,
    (Join-Path $ollamaRoot "logs")
) | ForEach-Object {
    New-Item -ItemType Directory -Path $_ -Force | Out-Null
}
