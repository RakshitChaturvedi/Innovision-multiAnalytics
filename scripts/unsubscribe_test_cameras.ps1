<#
.SYNOPSIS
  Remove all use cases (PUT /cameras/{id}/config {"use_cases": []}) from every platform camera
  whose name starts with "Test Camera", and print what changed. Prompts for the platform admin
  email/password (never stored). -DryRun only prints.
.EXAMPLE
  .\scripts\unsubscribe_test_cameras.ps1 -DryRun
  .\scripts\unsubscribe_test_cameras.ps1
#>
param(
    [switch]$DryRun,
    [string]$Prefix
)
. "$PSScriptRoot\_venv.ps1"
$argsList = @("-m", "tools.platform.unsubscribe_test_cameras")
if ($DryRun) { $argsList += "--dry-run" }
if ($Prefix) { $argsList += @("--prefix", $Prefix) }
& $Python @argsList
exit $LASTEXITCODE
