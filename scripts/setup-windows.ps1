<#
.SYNOPSIS
    One-time setup for the Griffin radioSHARK on Windows.

.DESCRIPTION
    Windows ships the radioSHARK's USB-audio capture endpoint ("Analog Connector
    (RadioSHARK)") DISABLED, so the device looks dead even though it's healthy.
    This script finds that endpoint by its hardware ID (USB VID 077d), force-enables
    it in the registry, also attempts a PnP enable, and restarts the audio service
    so the change takes effect. It self-elevates (UAC prompt) because it writes to
    HKLM and restarts a service.

.NOTES
    Run from an elevated or normal PowerShell:
        powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
    Plug the radioSHARK in first.
#>

# --- self-elevate to Administrator ---
$admin = ([Security.Principal.WindowsPrincipal] `
          [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "Requesting administrator rights..." -ForegroundColor Yellow
    Start-Process powershell -Verb RunAs `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    return
}

Write-Host "`n=== radioSHARK Windows setup ===`n" -ForegroundColor Cyan

# --- 1. find + enable the capture endpoint(s) in the registry ---
$base   = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture'
$found  = @()
foreach ($key in (Get-ChildItem $base -ErrorAction SilentlyContinue)) {
    $props = Join-Path $key.PSPath 'Properties'
    $match = $false
    foreach ($v in ((Get-Item $props -ErrorAction SilentlyContinue).Property)) {
        $val = (Get-ItemProperty -Path $props -Name $v -ErrorAction SilentlyContinue).$v
        if ("$val" -match '077[dD]' -or "$val" -match 'RadioSHARK') { $match = $true; break }
    }
    if ($match) {
        # DEVICE_STATE_ACTIVE = 1  (DISABLED = 2, NOTPRESENT = 4, UNPLUGGED = 8)
        Set-ItemProperty -Path $key.PSPath -Name 'DeviceState' -Value 1 -Type DWord -ErrorAction SilentlyContinue
        $name = (Get-ItemProperty -Path $props -Name '{a45c254e-df1c-4efd-8020-67d146a850e0},2' -ErrorAction SilentlyContinue).'{a45c254e-df1c-4efd-8020-67d146a850e0},2'
        $found += "$name [$($key.PSChildName)]"
    }
}

if ($found.Count -eq 0) {
    Write-Host "No radioSHARK capture endpoint found in the registry." -ForegroundColor Red
    Write-Host "Make sure the radioSHARK is plugged in, then run this again." -ForegroundColor Red
} else {
    Write-Host "Enabled radioSHARK capture endpoint(s):" -ForegroundColor Green
    $found | ForEach-Object { Write-Host "  - $_" }
}

# --- 2. also try a PnP-level enable (belt and suspenders) ---
Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue |
    Where-Object { $_.FriendlyName -match 'RadioSHARK' -and $_.Status -ne 'OK' } |
    ForEach-Object {
        try { Enable-PnpDevice -InstanceId $_.InstanceId -Confirm:$false -ErrorAction Stop }
        catch {}
    }

# --- 3. restart the audio service so the endpoint reappears ---
Write-Host "`nRestarting Windows Audio..." -ForegroundColor Yellow
try {
    Restart-Service -Name 'AudioEndpointBuilder' -Force -ErrorAction Stop   # restarts Audiosrv too
    Start-Sleep -Seconds 2
    Write-Host "Audio service restarted." -ForegroundColor Green
} catch {
    Write-Host "Could not restart the audio service automatically: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Reboot, or restart 'Windows Audio Endpoint Builder' in services.msc." -ForegroundColor Red
}

# --- 4. verify ---
Start-Sleep -Seconds 1
$active = Get-PnpDevice -Class MEDIA -ErrorAction SilentlyContinue |
          Where-Object { $_.InstanceId -match 'VID_077D' -and $_.Status -eq 'OK' }
Write-Host ""
if ($active) {
    Write-Host "radioSHARK audio is ENABLED and ready." -ForegroundColor Green
    Write-Host "If you still capture silence, open mmsys.cpl -> Recording ->" -ForegroundColor Gray
    Write-Host "Analog Connector (RadioSHARK) -> Properties -> Levels, and unmute / raise it." -ForegroundColor Gray
} else {
    Write-Host "Setup ran. If the device still looks dead, unplug/replug it and re-run." -ForegroundColor Yellow
}
Write-Host "`nDone. You can now run:  python shark_gui.py`n" -ForegroundColor Cyan
if ($Host.Name -eq 'ConsoleHost') { Read-Host "Press Enter to close" }
