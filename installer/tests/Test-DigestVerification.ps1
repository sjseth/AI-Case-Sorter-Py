<#
.SYNOPSIS
    Tests install-windows.ps1's check of the download against GitHub's digest.

.DESCRIPTION
    Get-DigestCheck mirrors classify_digest in sorter/update/updater.py and
    these cases mirror its tests in tests/unit/update/test_updater.py: the
    installer and the in-app updater download the same asset, so a digest one
    refuses and the other accepts is a bug in whichever one accepts it.

    Plain PowerShell rather than Pester, for the same reason as
    Test-ArchiveEntryValidation.ps1. Run:
      powershell -File installer/tests/Test-DigestVerification.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path (Split-Path $PSScriptRoot -Parent) 'install-windows.ps1')

$script:Failures = 0
$script:Passes = 0

function Assert-Kind {
    param($Digest, [string]$Kind, [string]$Value = '')
    $check = Get-DigestCheck -Digest $Digest
    if ($check.Kind -eq $Kind -and $check.Value -eq $Value) {
        $script:Passes++
    } else {
        $script:Failures++
        Write-Host "FAIL: '$Digest' -> $($check.Kind)/$($check.Value), expected $Kind/$Value" -ForegroundColor Red
    }
}

$hex = 'ab' * 32

Write-Host "== digest parsing ==" -ForegroundColor Cyan
Assert-Kind "sha256:$hex"               'sha256' $hex
Assert-Kind "sha256:$($hex.ToUpper())"  'sha256' $hex
Assert-Kind "SHA256:$hex"               'sha256' $hex
Assert-Kind $null                        'absent'
Assert-Kind ''                           'absent'
Assert-Kind '   '                        'absent'
Assert-Kind ("sha512:" + ('cd' * 64))    'unsupported' 'sha512'
Assert-Kind 'sha256:abc'                 'malformed'
Assert-Kind ("sha256:" + ('g' * 64))     'malformed'
Assert-Kind "sha256:$($hex)00"           'malformed'
Assert-Kind $hex                         'malformed'
Assert-Kind 'sha256'                     'malformed'
Assert-Kind 'sha256:'                    'malformed'
Assert-Kind ":$hex"                      'malformed'
Assert-Kind "sha 256:$hex"               'malformed'

Write-Host "== verifying a file ==" -ForegroundColor Cyan
# SHA-256 of the three bytes "abc" (FIPS 180-2 test vector).
$abc = 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
$file = Join-Path ([System.IO.Path]::GetTempPath()) ("casesorter-digest-" + [guid]::NewGuid().ToString('N'))
[System.IO.File]::WriteAllBytes($file, [byte[]](0x61, 0x62, 0x63))

function Assert-Verify {
    param($Digest, [string]$Expect, [string]$Because)
    $outcome = 'refused'
    try {
        $outcome = if (Assert-DownloadDigest -Path $file -Digest $Digest) { 'verified' } else { 'unverified' }
    } catch { }
    if ($outcome -eq $Expect) {
        $script:Passes++
    } else {
        $script:Failures++
        Write-Host "FAIL: '$Digest' was $outcome, expected $Expect ($Because)" -ForegroundColor Red
    }
}

try {
    Assert-Verify "sha256:$abc"              'verified'   'matching digest'
    Assert-Verify "sha256:$($abc.ToUpper())" 'verified'   'hex case does not matter'
    Assert-Verify "sha256:$hex"              'refused'    'mismatch'
    Assert-Verify $null                      'unverified' 'no digest published: TLS alone'
    Assert-Verify ''                         'unverified' 'empty digest: TLS alone'
    Assert-Verify ("sha512:" + ('cd' * 64))  'unverified' 'algorithm this installer cannot check'
    Assert-Verify 'sha256:abc'               'refused'    'present but unreadable'
} finally {
    Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
}

Write-Host ""
if ($script:Failures -gt 0) {
    Write-Host "$($script:Failures) failed, $($script:Passes) passed" -ForegroundColor Red
    exit 1
}
Write-Host "all $($script:Passes) passed" -ForegroundColor Green
