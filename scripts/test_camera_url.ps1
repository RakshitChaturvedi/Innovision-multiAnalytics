<#
.SYNOPSIS
  Grab one frame from a camera URL (DroidCam HTTP MJPEG or RTSP), print resolution/fps,
  WARN if not 16:9. The URL is never printed with its credentials.
.EXAMPLE
  .\scripts\test_camera_url.ps1 -Url http://192.168.1.20:4747/video
#>
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [string]$Out
)
. "$PSScriptRoot\_venv.ps1"
$argsList = @("-m", "tools.platform.camera_url", "--url", $Url)
if ($Out) { $argsList += @("--out", $Out) }
& $Python @argsList
exit $LASTEXITCODE
