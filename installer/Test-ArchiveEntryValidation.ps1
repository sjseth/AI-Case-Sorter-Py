<#
.SYNOPSIS
    Tests install-windows.ps1's archive-entry validation.

.DESCRIPTION
    tar.exe extracts unconditionally and cannot be relied on to sanitize
    entry names, so install-windows.ps1 checks them itself before letting a
    single byte be written. That check is the installer's only defence
    against a hostile archive, and it is the half of this repo that CANNOT
    be exercised by the Python suite -- so it gets its own tests.

    These are the same shapes sorter/update/updater.py's _safe_members rejects, in
    tests/unit/update/test_updater.py. The two extraction paths consume the *same*
    archives; a shape rejected by one and accepted by the other is a bug in
    whichever one accepts it.

    Plain PowerShell rather than Pester: the Windows runners ship an ancient
    Pester 3.x whose syntax differs sharply from 5.x, and this needs no
    fixtures or mocking. Run:  pwsh -File installer/Test-ArchiveEntryValidation.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Dot-source the installer for its functions only; the guard on its main
# block keeps it from trying to install anything.
. (Join-Path $PSScriptRoot 'install-windows.ps1')

$script:Failures = 0
$script:Passes = 0

function Assert-Rejected {
    param([string]$EntryName, [string]$Because)
    $threw = $false
    try {
        Assert-SafeArchiveEntries -EntryNames @($EntryName)
    } catch {
        $threw = $true
    }
    if ($threw) {
        $script:Passes++
    } else {
        $script:Failures++
        Write-Host "FAIL: '$EntryName' was ACCEPTED but must be rejected ($Because)" -ForegroundColor Red
    }
}

function Assert-Accepted {
    param([string]$EntryName)
    try {
        Assert-SafeArchiveEntries -EntryNames @($EntryName)
        $script:Passes++
    } catch {
        $script:Failures++
        Write-Host "FAIL: '$EntryName' was REJECTED but is a legitimate entry: $($_.Exception.Message)" -ForegroundColor Red
    }
}

Write-Host "== must reject: drive letter in any component ==" -ForegroundColor Cyan
# The regression this file exists for. A start-anchored '^[A-Za-z]:' catches
# only the first two cases; PurePosixPath/Path() on the Python side and
# Win32 path-join on this side both re-anchor on a drive component wherever
# it appears, so every one of these can escape the destination.
Assert-Rejected 'C:/evil.py'            'drive letter at the start'
Assert-Rejected 'C:evil.py'             'drive-relative at the start'
Assert-Rejected 'pkg/D:/evil.py'        'drive letter in a later component'
Assert-Rejected 'pkg/D:evil.py'         'drive-relative in a later component'
Assert-Rejected 'pkg/sub/E:/x.dll'      'drive letter deeper still'
Assert-Rejected 'pkg\F:\x.dll'          'drive letter after a backslash separator'
Assert-Rejected 'pkg/main.py:stream'    'NTFS alternate data stream'

Write-Host "== must reject: absolute and UNC paths ==" -ForegroundColor Cyan
Assert-Rejected '/etc/passwd'           'POSIX absolute path'
Assert-Rejected '\windows\system32\x'   'backslash-rooted path'
Assert-Rejected '\\server\share\x'      'UNC path'

Write-Host "== must reject: traversal ==" -ForegroundColor Cyan
Assert-Rejected 'pkg/../../evil.py'     'parent traversal'
Assert-Rejected '..'                    'bare parent'
Assert-Rejected 'pkg\..\..\evil.py'     'parent traversal via backslashes'

Write-Host "== must accept: real sdist entries ==" -ForegroundColor Cyan
# Guarding against the opposite failure: a check so broad it rejects the
# archive every legitimate install depends on. The `src/` layout (#58) is
# what a real sdist ships today; the bare `main.py` case below is kept
# because path-safety has to accept that shape too -- it's what an
# *old*, pre-#58 release archive looked like, and install-windows.ps1 still
# has to be able to unpack one of those from a pinned -Version.
Assert-Accepted 'ai_case_sorter-1.2.3/main.py'
Assert-Accepted 'ai_case_sorter-1.2.3/bootstrap.py'
Assert-Accepted 'ai_case_sorter-1.2.3/src/sorter/__init__.py'
Assert-Accepted 'ai_case_sorter-1.2.3/src/sorter/_version.py'
Assert-Accepted 'ai_case_sorter-1.2.3/PKG-INFO'
Assert-Accepted 'ai_case_sorter-1.2.3/installer/install-windows.ps1'
Assert-Accepted 'ai_case_sorter-1.2.3/.gitignore'
Assert-Accepted 'ai_case_sorter-1.2.3/src/sorter/ui/tab_ai.py'
Assert-Accepted 'pkg/a..b.py'           # dots, but no '..' component
Assert-Accepted 'pkg/sub.dir/x.py'
Assert-Accepted 'pkg/file with spaces.py'

Write-Host ""
if ($script:Failures -gt 0) {
    Write-Host "$($script:Failures) failed, $($script:Passes) passed" -ForegroundColor Red
    exit 1
}
Write-Host "all $($script:Passes) passed" -ForegroundColor Green
