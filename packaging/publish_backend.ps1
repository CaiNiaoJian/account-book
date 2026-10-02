# Replace only managed application files. Portable data and flags stay in place.
function Publish-AccountBook([string]$Source, [string]$Target, [string]$Workspace) {
    $root = [IO.Path]::GetFullPath($Workspace).TrimEnd('\') + '\'
    $sourcePath = [IO.Path]::GetFullPath($Source)
    $targetPath = [IO.Path]::GetFullPath($Target)
    foreach ($path in @($sourcePath, $targetPath)) {
        if (-not $path.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Application publication path outside workspace: $path"
        }
    }
    foreach ($name in @('AccountBook.exe', '_internal')) {
        if (-not (Test-Path -LiteralPath (Join-Path $sourcePath $name))) {
            throw "Missing application component: $name"
        }
    }
    New-Item -ItemType Directory -Force -Path $targetPath | Out-Null
    $previous = Join-Path $sourcePath ('previous-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $previous | Out-Null
    $installed = @()
    $saved = @()
    try {
        foreach ($name in @('AccountBook.exe', '_internal')) {
            $destination = Join-Path $targetPath $name
            if (Test-Path -LiteralPath $destination) {
                Move-Item -LiteralPath $destination -Destination (Join-Path $previous $name)
                $saved += $name
            }
            Move-Item -LiteralPath (Join-Path $sourcePath $name) -Destination $destination
            $installed += $name
        }
    }
    catch {
        foreach ($name in $installed) {
            Move-Item -LiteralPath (Join-Path $targetPath $name) -Destination (Join-Path $sourcePath $name)
        }
        foreach ($name in $saved) {
            Move-Item -LiteralPath (Join-Path $previous $name) -Destination (Join-Path $targetPath $name)
        }
        throw
    }
}
