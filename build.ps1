param(
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot
$Version = "0.4"

python -m pip install -r requirements-dev.txt
if (-not $SkipTests) {
    python -m pytest -q
}

python -m PyInstaller --noconfirm --clean ShroudDesigner.spec

$AppDirectory = Join-Path $ProjectRoot "dist\ShroudDesigner"
$AppExecutable = Join-Path $AppDirectory "ShroudDesigner.exe"
$SelfTestReport = Join-Path $ProjectRoot "packaged-self-test-windows.json"
if (Test-Path -LiteralPath $SelfTestReport) {
    Remove-Item -LiteralPath $SelfTestReport
}
$SelfTestProcess = Start-Process `
    -FilePath $AppExecutable `
    -ArgumentList @("--self-test", ('"{0}"' -f $SelfTestReport)) `
    -Wait `
    -PassThru
$SelfTestExitCode = $SelfTestProcess.ExitCode
if (-not (Test-Path -LiteralPath $SelfTestReport)) {
    throw "Packaged self-test did not create a report (exit code $SelfTestExitCode)."
}
$SelfTest = Get-Content -LiteralPath $SelfTestReport -Raw | ConvertFrom-Json
if ($SelfTestExitCode -ne 0 -or -not $SelfTest.ok) {
    throw "Packaged self-test failed: $($SelfTest | ConvertTo-Json -Compress)"
}
Write-Host "Packaged self-test OK"

Copy-Item -LiteralPath "LICENSE" -Destination $AppDirectory
Copy-Item -LiteralPath "THIRD_PARTY_NOTICES.md" -Destination $AppDirectory
Copy-Item -LiteralPath "licenses" -Destination $AppDirectory -Recurse

$PortableArchive = Join-Path $ProjectRoot "dist\ShroudDesigner-$Version-windows-x86_64.zip"
if (Test-Path -LiteralPath $PortableArchive) {
    Remove-Item -LiteralPath $PortableArchive
}
Compress-Archive -Path $AppDirectory -DestinationPath $PortableArchive -CompressionLevel Optimal

if (-not $SkipInstaller) {
    $CompilerCandidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )
    $InnoCompiler = $CompilerCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $InnoCompiler) {
        throw "Inno Setup 6 was not found. Install it with: winget install JRSoftware.InnoSetup"
    }
    & $InnoCompiler "installer\ShroudDesigner.iss"
}

$ChecksumTargets = @($PortableArchive)
if (-not $SkipInstaller) {
    $ChecksumTargets += Join-Path $ProjectRoot "dist\ShroudDesigner-$Version-Setup.exe"
}
$ChecksumLines = foreach ($Target in $ChecksumTargets) {
    $Hash = Get-FileHash -LiteralPath $Target -Algorithm SHA256
    "$($Hash.Hash.ToLowerInvariant())  $(Split-Path -Leaf $Target)"
}
$ChecksumPath = Join-Path $ProjectRoot "dist\SHA256SUMS-windows.txt"
$ChecksumLines | Set-Content -LiteralPath $ChecksumPath -Encoding ascii

Write-Host "Build complete."
Write-Host "Application: $AppExecutable"
Write-Host "Portable: $PortableArchive"
if (-not $SkipInstaller) {
    Write-Host "Installer: dist\ShroudDesigner-$Version-Setup.exe"
}
Write-Host "Checksums: $ChecksumPath"
