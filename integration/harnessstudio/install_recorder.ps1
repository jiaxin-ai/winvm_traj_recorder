<#
.SYNOPSIS
    Installs the trajectory recorder for HarnessStudio / vm_manager into a Windows VM or image.

.DESCRIPTION
    1. Copies this repository to C:\trajrec\recorder (without .git, samples and caches).
    2. Creates C:\trajrec\venv from the agent service's Python 3.10 and installs requirements.txt,
       offline from -Wheels when given.
    3. Checks ffmpeg (C:\ffmpeg\bin in the image) and git (file snapshots; optional).
    4. Installs the PowerShell transcript hook (install_profile.ps1; needs administrator).
    5. Copies trajrec_routes.py next to the agent service's main.py; with -PatchAgent also adds
       the two lines that register /recording/start|stop|milestone|status in main.py.
    6. Checks that every recorder module imports.

    Safe to re-run. Run it as administrator in the VM (or the image-building VM), e.g.
        powershell -ExecutionPolicy Bypass -File F:\trajrec-bundle\recorder\integration\harnessstudio\install_recorder.ps1 -Wheels F:\trajrec-bundle\wheels -PatchAgent

    -PatchAgent edits C:\oem\server\main.py. The agent service runs with Flask's debug reloader,
    which restarts the service when main.py changes; restart the VM afterwards (when building the
    image: shut it down normally) instead of relying on that reload.
#>
param(
    [string]$Source = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$InstallDir = "C:\trajrec",
    [string]$Python = "C:\Users\Docker\AppData\Local\Programs\Python\Python310\python.exe",
    [string]$Wheels = "",
    [string]$AgentDir = "C:\oem\server",
    [string]$FfmpegDir = "C:\ffmpeg\bin",
    [switch]$PatchAgent
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Step($text) { Write-Host "[install] $text" }
function Invoke-Checked {
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$File $($Arguments -join ' ') exited with $LASTEXITCODE" }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not (Test-Path (Join-Path $Source "main.py"))) { throw "recorder not found in $Source (expected main.py)" }
if (-not (Test-Path $Python)) { throw "Python not found: $Python" }

# 1. recorder files
$recorderDir = Join-Path $InstallDir "recorder"
New-Item -ItemType Directory -Path $recorderDir -Force | Out-Null
if ((Resolve-Path $Source).Path.TrimEnd("\") -ne (Resolve-Path $recorderDir).Path.TrimEnd("\")) {
    Step "copy $Source -> $recorderDir"
    & robocopy $Source $recorderDir /MIR /NFL /NDL /NJH /NJS /NP /XD .git __pycache__ samples /XF .DS_Store *.pyc | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed with $LASTEXITCODE" }
    $global:LASTEXITCODE = 0
}

# 2. Python environment, separate from the agent service's own packages
$venvPython = Join-Path $InstallDir "venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Step "create venv $InstallDir\venv"
    Invoke-Checked $Python @("-m", "venv", (Join-Path $InstallDir "venv"))
}
$pipArgs = @("-m", "pip", "install", "--disable-pip-version-check", "-r", (Join-Path $recorderDir "requirements.txt"))
if ($Wheels) {
    if (-not (Test-Path $Wheels)) { throw "wheel directory not found: $Wheels" }
    $pipArgs += @("--no-index", "--find-links", $Wheels)
}
Step "pip install -r requirements.txt $(if ($Wheels) { "(offline: $Wheels)" } else { '(online)' })"
Invoke-Checked $venvPython $pipArgs

# 3. external tools
if (Test-Path (Join-Path $FfmpegDir "ffmpeg.exe")) {
    Step "ffmpeg: $FfmpegDir\ffmpeg.exe"
} elseif (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Step "ffmpeg: $((Get-Command ffmpeg).Source) (on PATH; trajrec_routes.py adds $FfmpegDir only)"
} else {
    throw "ffmpeg not found. Put ffmpeg.exe in $FfmpegDir (screen.py records recording.mp4 with it)."
}
if (Get-Command git -ErrorAction SilentlyContinue) {
    Step "git: $((Get-Command git).Source)"
} else {
    Write-Warning "git not found: recordings will have no file snapshots (snapshot.py turns itself off)."
}

# 4. PowerShell transcripts (raw/terminal)
if ($isAdmin) {
    Step "PowerShell transcript hook"
    & (Join-Path $recorderDir "install_profile.ps1")
} else {
    Write-Warning "not administrator: skipped install_profile.ps1 (PowerShell windows will not be transcribed)."
}
New-Item -ItemType Directory -Path "C:\ProgramData\trajrec" -Force | Out-Null

# 5. agent service routes
$routes = Join-Path $recorderDir "integration\harnessstudio\trajrec_routes.py"
$agentMain = Join-Path $AgentDir "main.py"
if (-not (Test-Path $agentMain)) { throw "agent service not found: $agentMain" }
Step "copy trajrec_routes.py -> $AgentDir"
Copy-Item $routes (Join-Path $AgentDir "trajrec_routes.py") -Force
if ($PatchAgent) {
    $text = [IO.File]::ReadAllText($agentMain)
    if ($text.Contains("trajrec_routes.register(app)")) {
        Step "main.py already registers trajrec_routes"
    } else {
        $guard = "if __name__ == '__main__':"
        $index = $text.LastIndexOf($guard)
        if ($index -lt 0) { throw "cannot find `"$guard`" in $agentMain; add the two lines by hand (README.md)" }
        $backup = "$agentMain.before-trajrec"
        if (-not (Test-Path $backup)) { Copy-Item $agentMain $backup }
        $newline = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
        $lines = "import trajrec_routes  # HarnessStudio trajectory recording (/recording/*)$newline" +
                 "trajrec_routes.register(app)$newline$newline"
        # Same encoding as the original (UTF-8 without BOM, which Python reads by default).
        [IO.File]::WriteAllText($agentMain, $text.Insert($index, $lines), (New-Object Text.UTF8Encoding($false)))
        Step "patched $agentMain (backup: $backup). Restart the VM so the agent service starts with the routes."
    }
} else {
    Step "main.py not changed (use -PatchAgent, or register the routes by hand; see README.md)"
}

# 6. self-check: every module the recorder imports at start
Step "self-check"
Push-Location $recorderDir
try {
    Invoke-Checked $venvPython @("-c", "import main, pynput, uiautomation, mss, watchdog, psutil, win32api; print('[install] recorder imports ok')")
} finally {
    Pop-Location
}
Step "done. Recorder: $venvPython $recorderDir\main.py"
