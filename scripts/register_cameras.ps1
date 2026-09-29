<#
.SYNOPSIS
  Register (or, with -Update, update) one camera in the platform registry for this use case.
  Prompts for the platform admin email/password; they are never stored.
.EXAMPLE
  .\scripts\register_cameras.ps1 -Name "Phone 1" -Location "Lobby" -Url http://192.168.1.20:4747/video -Fps 5 -PlatformPath C:\innovision\platform
#>
param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$Location,
    [Parameter(Mandatory = $true)][string]$Url,
    [int]$Fps = 5,
    [switch]$Update,
    [string]$PlatformPath
)
. "$PSScriptRoot\_venv.ps1"
$argsList = @("-m", "tools.platform.register_camera", "--name", $Name, "--location", $Location,
              "--url", $Url, "--fps", $Fps)
if ($Update) { $argsList += "--update" }
if ($PlatformPath) { $argsList += @("--platform-path", $PlatformPath) }
& $Python @argsList
exit $LASTEXITCODE
