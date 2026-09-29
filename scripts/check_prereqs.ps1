<#
.SYNOPSIS
  PASS/FAIL checks before running the use case against the real platform.
.EXAMPLE
  .\scripts\check_prereqs.ps1 -PlatformPath C:\innovision\platform
#>
param([Parameter(Mandatory = $true)][string]$PlatformPath)
. "$PSScriptRoot\_venv.ps1"
& $Python -m tools.platform.prereqs --platform-path $PlatformPath
exit $LASTEXITCODE
