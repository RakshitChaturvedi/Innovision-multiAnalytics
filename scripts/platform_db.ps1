<#
.SYNOPSIS
  Create database innovision_analytics on the platform Postgres (if missing), enable pgvector,
  run alembic upgrade head and print the alembic heads. Never touches innovision_platform.
.EXAMPLE
  .\scripts\platform_db.ps1
#>
. "$PSScriptRoot\_venv.ps1"
& $Python -m tools.platform.platform_db
exit $LASTEXITCODE
