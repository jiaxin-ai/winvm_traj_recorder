#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Installs the trajrec PowerShell transcript hook into the AllUsersAllHosts
    profile(s), for both Windows PowerShell 5.1 and PowerShell 7+ (if present).

.DESCRIPTION
    Appends a small block to profile.ps1 that, every time a new PowerShell
    window opens, checks C:\ProgramData\trajrec\current_episode.txt. If that
    marker file exists (main.py writes it while a recording is running), the
    hook starts `Start-Transcript -IncludeInvocationHeader` into
    raw/terminal/pwsh_<pid>.txt under the current episode directory. If the
    marker doesn't exist, the hook does nothing — so only PowerShell windows
    opened *after* a recording starts get captured, per task.md.

    Safe to re-run: it checks for its own marker comment before appending, so
    it won't install the block twice.

.NOTES
    Run this once, as Administrator, before using main.py. This script has
    not been run on a real Windows machine — verify on the VM per README.md.
#>

$ErrorActionPreference = "Stop"

$HookMarkerBegin = "# --- trajrec transcript hook: begin ---"
$HookMarkerEnd = "# --- trajrec transcript hook: end ---"

$HookBlock = @"
$HookMarkerBegin
try {
    `$trajrecMarker = "C:\ProgramData\trajrec\current_episode.txt"
    if (Test-Path `$trajrecMarker) {
        `$trajrecDir = (Get-Content `$trajrecMarker -Raw -ErrorAction Stop).Trim()
        if (`$trajrecDir -and (Test-Path `$trajrecDir)) {
            `$trajrecFile = Join-Path `$trajrecDir ("pwsh_{0}.txt" -f `$PID)
            Start-Transcript -Path `$trajrecFile -IncludeInvocationHeader | Out-Null
        }
    }
} catch {
    # never let a transcript failure block the shell from starting
}
$HookMarkerEnd
"@

function Install-Hook {
    param([string]$ProfilePath)

    $dir = Split-Path -Parent $ProfilePath
    if (-not (Test-Path $dir)) {
        Write-Host "跳过(未找到该 PowerShell 版本): $ProfilePath"
        return
    }

    if (-not (Test-Path $ProfilePath)) {
        New-Item -ItemType File -Path $ProfilePath -Force | Out-Null
    }

    $existing = Get-Content $ProfilePath -Raw -ErrorAction SilentlyContinue
    if ($existing -and $existing.Contains($HookMarkerBegin)) {
        Write-Host "已安装,跳过: $ProfilePath"
        return
    }

    Add-Content -Path $ProfilePath -Value "`n$HookBlock`n"
    Write-Host "已安装: $ProfilePath"
}

New-Item -ItemType Directory -Path "C:\ProgramData\trajrec" -Force | Out-Null

# Windows PowerShell 5.1 (AllUsersAllHosts)
Install-Hook -ProfilePath "$env:windir\System32\WindowsPowerShell\v1.0\profile.ps1"

# PowerShell 7+ (AllUsersAllHosts), if installed
Install-Hook -ProfilePath "C:\Program Files\PowerShell\7\profile.ps1"

Write-Host "完成。打开一个新的 PowerShell 窗口即可测试(见 README.md)。"
