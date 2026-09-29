<#
.SYNOPSIS
  Stop the use case services: graceful first, force-kill after -Timeout seconds.
.EXAMPLE
  .\scripts\down.ps1
#>
param([int]$Timeout = 20)
. "$PSScriptRoot\_venv.ps1"
& $Python -m tools.platform.services down --timeout $Timeout
exit $LASTEXITCODE
