# openazure installer (Windows PowerShell).
# Tries pipx, then uv, then pip, installing from git+https.
$ErrorActionPreference = "Stop"

$repo = "git+https://github.com/cognis-digital/openazure.git"
Write-Host "Installing openazure from $repo ..."

function Test-Cmd($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

if (Test-Cmd "pipx") {
    Write-Host "-> using pipx"
    pipx install $repo
} elseif (Test-Cmd "uv") {
    Write-Host "-> using uv tool install"
    uv tool install $repo
} elseif (Test-Cmd "python") {
    Write-Host "-> using pip (python -m pip)"
    python -m pip install $repo
} elseif (Test-Cmd "py") {
    Write-Host "-> using pip (py -m pip)"
    py -m pip install $repo
} else {
    Write-Error "Need one of pipx, uv, or python/py on PATH."
    exit 1
}

Write-Host ""
Write-Host "Done. Try:  openazure serve --in-memory"
