[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$program = Join-Path $workspace 'dist/AccountBook'
$output = Join-Path $workspace 'release'
$match = Select-String -LiteralPath (Join-Path $workspace 'src/accountbook/__init__.py') -Pattern '^__version__ = "([^"]+)"'
$version = $match.Matches[0].Groups[1].Value
if (-not (Test-Path -LiteralPath (Join-Path $program 'AccountBook.exe'))) { throw 'Build the desktop program first.' }
$stage = Join-Path $workspace ('.smoke-test/package-' + [guid]::NewGuid().ToString('N'))
$portable = Join-Path $stage 'AccountBook'
New-Item -ItemType Directory -Path $portable,$output -Force | Out-Null
# Allowlist the program, never recursively copy the installation with its private data.
Copy-Item -LiteralPath (Join-Path $program 'AccountBook.exe') -Destination $portable
Copy-Item -LiteralPath (Join-Path $program '_internal') -Destination $portable -Recurse
Set-Content -LiteralPath (Join-Path $portable 'portable.flag') -Value 'portable' -Encoding ascii
Copy-Item -LiteralPath (Join-Path $workspace 'docs/UPDATE.md') -Destination (Join-Path $portable 'UPDATE.md')
$zip = Join-Path $output "AccountBook-$version-Windows-x64.zip"
Compress-Archive -LiteralPath $portable -DestinationPath $zip -Force
$hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath (Join-Path $output 'SHA256SUMS.txt') -Value "$hash  $([IO.Path]::GetFileName($zip))" -Encoding ascii
Write-Host "Portable package: $zip"
