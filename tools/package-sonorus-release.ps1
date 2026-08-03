[CmdletBinding()]
param(
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutputPath) {
    $OutputPath = Join-Path $repositoryRoot (
        "dist\Sonorus-1.0.8-pre5-OmniVoice-Vulkan.zip"
    )
}
$outputFullPath = [System.IO.Path]::GetFullPath($OutputPath)
$outputDirectory = Split-Path -Parent $outputFullPath
$rootFiles = @(
    "disable_or_enable_sonorus.bat",
    "do not extract into game folder!",
    "install_sonorus.bat",
    "instructions.txt",
    "README.md",
    "uninstall_sonorus.bat"
)
$trackedFiles = @(
    $rootFiles
    git -C $repositoryRoot ls-files -- "Phoenix"
)
if ($LASTEXITCODE -ne 0) {
    throw "Could not enumerate tracked Sonorus package files"
}

$stageRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    "sonorus-release-" + [Guid]::NewGuid().ToString("N")
)
New-Item -ItemType Directory -Path $stageRoot | Out-Null
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null

try {
    foreach ($relativePath in $trackedFiles) {
        $source = Join-Path $repositoryRoot $relativePath
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Tracked package file is missing: $relativePath"
        }

        $stream = [System.IO.File]::OpenRead($source)
        try {
            $probeLength = [Math]::Min(128, $stream.Length)
            $probe = New-Object byte[] $probeLength
            [void]$stream.Read($probe, 0, $probeLength)
            $prefix = [Text.Encoding]::ASCII.GetString($probe)
        }
        finally {
            $stream.Dispose()
        }
        if ($prefix.StartsWith("version https://git-lfs.github.com/spec/v1")) {
            throw "Unexpanded Git LFS pointer: $relativePath. Run git lfs pull first."
        }

        $destination = Join-Path $stageRoot $relativePath
        $destinationDirectory = Split-Path -Parent $destination
        New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination
    }

    if (Test-Path -LiteralPath $outputFullPath) {
        [System.IO.File]::Delete($outputFullPath)
    }
    Add-Type -AssemblyName System.IO.Compression
    $zipStream = [System.IO.File]::Create($outputFullPath)
    try {
        $zipArchive = [System.IO.Compression.ZipArchive]::new(
            $zipStream,
            [System.IO.Compression.ZipArchiveMode]::Create,
            $false
        )
        try {
            foreach ($file in Get-ChildItem -LiteralPath $stageRoot -Recurse -File) {
                $relativePath = $file.FullName.Substring($stageRoot.Length)
                $relativePath = $relativePath.TrimStart([char[]]@("\", "/")).Replace("\", "/")
                $entry = $zipArchive.CreateEntry(
                    $relativePath,
                    [System.IO.Compression.CompressionLevel]::Optimal
                )
                $inputStream = $file.OpenRead()
                $entryStream = $entry.Open()
                try {
                    $inputStream.CopyTo($entryStream)
                }
                finally {
                    $entryStream.Dispose()
                    $inputStream.Dispose()
                }
            }
        }
        finally {
            $zipArchive.Dispose()
        }
    }
    finally {
        $zipStream.Dispose()
    }

    $archive = Get-Item -LiteralPath $outputFullPath
    $archiveHash = (Get-FileHash -LiteralPath $outputFullPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $checksumPath = $outputFullPath + ".sha256"
    [System.IO.File]::WriteAllText(
        $checksumPath,
        "$archiveHash  $($archive.Name)`n",
        [System.Text.UTF8Encoding]::new($false)
    )
    Write-Output "Created $outputFullPath"
    Write-Output "Size: $($archive.Length)"
    Write-Output "SHA-256: $archiveHash"
}
finally {
    if (Test-Path -LiteralPath $stageRoot) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
}
