$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path $PSScriptRoot -Parent
$buildRoot = Join-Path $sourceRoot 'build'
$releaseRoot = Join-Path $buildRoot 'mcp-release'
$stage = Join-Path $buildRoot 'mcp-distribution'
if ((Test-Path $stage) -or (Test-Path $releaseRoot)) {
    throw 'Release staging already exists; use a fresh build directory.'
}
New-Item -ItemType Directory -Path $releaseRoot, $stage | Out-Null
# Extract the pristine archive, not the GUI-test directory containing user state.
$portableZip = @(Get-ChildItem (Join-Path $buildRoot 'Aegisub-*-portable.zip'))
if ($portableZip.Count -ne 1) { throw 'Expected one portable archive' }
Expand-Archive -LiteralPath $portableZip[0].FullName -DestinationPath $stage
$portable = Join-Path $stage 'aegisub-portable'
if (!(Test-Path (Join-Path $portable 'aegisub.exe'))) { throw 'Missing portable executable' }
$mcpDir = Join-Path $portable 'mcp'
New-Item -ItemType Directory -Path $mcpDir | Out-Null
Copy-Item (Join-Path $sourceRoot 'mcp/pyproject.toml') $mcpDir
$pythonSource = Join-Path $mcpDir 'src/aegisub_mcp'
New-Item -ItemType Directory -Path $pythonSource -Force | Out-Null
Copy-Item (Join-Path $sourceRoot 'mcp/src/aegisub_mcp/*.py') $pythonSource
Copy-Item (Join-Path $sourceRoot 'docs/mcp.md') (Join-Path $portable 'MCP-README.md')
Copy-Item (Join-Path $sourceRoot 'LICENCE') (Join-Path $portable 'LICENCE')
Copy-Item (Join-Path $sourceRoot 'packages/win_installer/license.txt') (Join-Path $portable 'THIRD-PARTY-LICENSES.txt')
Copy-Item (Join-Path $PSScriptRoot 'mcp-setup-windows.cmd') (Join-Path $portable 'setup-mcp.cmd')
Copy-Item (Join-Path $PSScriptRoot 'mcp-setup-windows.py') (Join-Path $portable 'setup-mcp.py')
$revision = (git -C $sourceRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE) { throw 'Cannot determine source revision' }
"Source: https://github.com/F767-R/aegisub-mcp/tree/$revision`nCommit: $revision`nArchitecture: Windows x64`nUnsigned community build" | Set-Content (Join-Path $portable 'BUILD-INFO.txt')
Compress-Archive -LiteralPath $portable -DestinationPath (Join-Path $releaseRoot 'Aegisub-MCP-Windows-x64-portable.zip')
$installer = @(Get-ChildItem (Join-Path $buildRoot 'Aegisub-*.exe'))
if ($installer.Count -ne 1) { throw 'Expected one installer' }
Copy-Item $installer[0].FullName (Join-Path $releaseRoot 'Aegisub-MCP-Windows-x64-setup.exe')
$sourceArchive = Join-Path $releaseRoot 'Aegisub-MCP-source.zip'
git -C $sourceRoot archive --format=zip --prefix=aegisub-mcp/ "--output=$sourceArchive" HEAD
if ($LASTEXITCODE) { throw 'Source archive failed' }
Get-ChildItem $releaseRoot -File | Sort-Object Name | ForEach-Object {
    $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $($_.Name)"
} | Set-Content (Join-Path $releaseRoot 'SHA256SUMS.txt') -Encoding ascii
