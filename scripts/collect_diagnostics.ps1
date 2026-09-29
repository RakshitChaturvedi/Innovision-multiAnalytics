<#
.SYNOPSIS
  Build diagnostics_<timestamp>.zip (logs, health, streams, alembic, docker, both .env files
  with every secret masked). No model files or photos.
.EXAMPLE
  .\scripts\collect_diagnostics.ps1 -PlatformPath C:\innovision\platform
#>
param([string]$PlatformPath)
. "$PSScriptRoot\_venv.ps1"
$argsList = @("-m", "tools.platform.diagnostics")
if ($PlatformPath) { $argsList += @("--platform-path", $PlatformPath) }
& $Python @argsList
exit $LASTEXITCODE
