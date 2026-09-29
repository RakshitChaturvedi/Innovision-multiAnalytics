<#
.SYNOPSIS
  Preflight, then start detection, recognition and event_processing in the background
  (logs and PID files in .\logs).
.EXAMPLE
  .\scripts\up.ps1 -PlatformPath C:\innovision\platform
#>
param([string]$PlatformPath, [int]$Wait = 60)
. "$PSScriptRoot\_venv.ps1"
$argsList = @("-m", "tools.platform.services", "up", "--wait", $Wait)
if ($PlatformPath) { $argsList += @("--platform-path", $PlatformPath) }
& $Python @argsList
exit $LASTEXITCODE
