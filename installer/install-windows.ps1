<#
.SYNOPSIS
    Installs (or updates) the AI Case Sorter on Windows. No git required.

.DESCRIPTION
    Provisions the two things a non-developer machine is missing - a Python
    runtime and a copy of the app - then hands off to start.bat, which just
    calls bootstrap.py: that's what owns the virtualenv and dependency
    install now (via uv), not this script or start.bat itself.

    Deliberately git-free. `git pull` over HTTPS and a release ZIP over HTTPS
    have the same trust anchor (TLS to github.com), and this repo is ~1 MB, so
    git's delta transfer buys nothing. Not installing a 60 MB dependency to
    deliver a 1 MB update is the whole point.

    Installs per-user to %LOCALAPPDATA%\Programs\CaseSorter - no admin rights,
    and the folder stays writable so the venv and the in-app updater work.
    User data lives in %LOCALAPPDATA%\CaseSorter, outside the app folder, so
    reinstalling never touches trained models or settings.

    Re-running this script updates an existing install in place.

.PARAMETER InstallDir
    Override the install location.

.PARAMETER Version
    Install a specific release tag (e.g. "v0.2.0") instead of the latest.

.PARAMETER NoLaunch
    Install without starting the app afterwards.

.PARAMETER Repo
    Override the "owner/repo" to install from. Mirrors sorter/updater.py's
    CASESORTER_UPDATE_REPO -- same reason: verifying against a fork's own
    releases without editing the script.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install-windows.ps1
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\CaseSorter",
    [string]$Version = "",
    [switch]$NoLaunch,
    [string]$Repo = 'sjseth/AI-Case-Sorter-Py'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# StrictMode makes reading an unset variable a terminating error, and
# $LASTEXITCODE does not exist until some external program has run.
$LASTEXITCODE = 0

# NOTE: keep this file pure ASCII. Windows PowerShell 5.1 decodes a
# BOM-less file as the system ANSI codepage, so a UTF-8 em-dash arrives as
# 'a', 'EUR', and U+201D - and PowerShell treats U+201D as a closing double
# quote, which silently truncates the enclosing string and misparses
# everything after it. tests/unit/test_installer_scripts.py enforces this.

$DefaultBranch = 'main'
$PythonWinget = 'Python.Python.3.12'
$PythonMinor  = 12
# Keep in step with requires-python in pyproject.toml.
$PythonMin    = [Version]'3.12'

function Write-Step  { param([string]$m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Note  { param([string]$m) Write-Host "    $m" -ForegroundColor DarkGray }
function Write-Ok    { param([string]$m) Write-Host "    $m" -ForegroundColor Green }
function Write-Warn2 { param([string]$m) Write-Host "    $m" -ForegroundColor Yellow }

# TLS 1.2 for Invoke-WebRequest on older Windows PowerShell defaults.
try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

function Get-PythonCommand {
    <#
      Returns the path to a python.exe meeting $PythonMin that also has
      tkinter, or $null. tkinter matters: the whole UI is Tkinter, and a
      Python without Tcl/Tk fails at launch with a confusing ImportError
      rather than here where we can do something about it.
    #>
    $candidates = @()

    # -CommandType Application so a function or alias named `python` can't
    # shadow a real interpreter.
    $cmd = Get-Command python -CommandType Application -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += @($cmd | ForEach-Object { $_.Source }) }

    # The py launcher knows about installs that aren't on PATH.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            $p = & py "-3" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $p) { $candidates += $p.Trim() }
        } catch { }
    }

    $candidates += @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
    )

    foreach ($candidate in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path $candidate)) { continue }
        # Skip the Microsoft Store "app execution alias" stub. It exists on a
        # stock Windows install with no Python behind it, and running it opens
        # the Store instead of an interpreter.
        if ($candidate -like '*\WindowsApps\*') { continue }
        try {
            $out = & $candidate -c "import sys, tkinter; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
            if ([Version]$out.Trim() -ge $PythonMin) { return $candidate }
        } catch { continue }
    }
    return $null
}

function Install-Python {
    Write-Step "Installing Python (none suitable was found)"

    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Note "Using winget: $PythonWinget"
        try {
            & winget install --id $PythonWinget --exact --source winget `
                --accept-package-agreements --accept-source-agreements `
                --scope user --silent
        } catch {
            Write-Warn2 "winget failed: $($_.Exception.Message)"
        }
        $found = Get-PythonCommand
        if ($found) { return $found }
        Write-Warn2 "winget did not produce a usable Python; falling back to python.org."
    }

    # python.org fallback. Per-user, silent, and explicitly including Tcl/Tk.
    $arch = if ([Environment]::Is64BitOperatingSystem) { 'amd64' } else { 'win32' }
    $pyVer = "3.$PythonMinor.8"
    $url = "https://www.python.org/ftp/python/$pyVer/python-$pyVer-$arch.exe"
    $exe = Join-Path $env:TEMP "python-$pyVer-$arch.exe"

    Write-Note "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing

    Write-Note "Running the installer (per-user, silent)..."
    $proc = Start-Process -FilePath $exe -Wait -PassThru -ArgumentList @(
        '/quiet', 'InstallAllUsers=0', 'PrependPath=1',
        'Include_tcltk=1', 'Include_pip=1', 'Include_launcher=1'
    )
    Remove-Item $exe -Force -ErrorAction SilentlyContinue
    if ($proc.ExitCode -ne 0) {
        throw "The Python installer exited with code $($proc.ExitCode)."
    }

    # PrependPath only affects *new* processes, so this shell still can't see
    # it - re-read the user PATH rather than trusting `where python`.
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')

    $found = Get-PythonCommand
    if (-not $found) {
        throw "Python was installed but could not be located. Open a new terminal and re-run this script."
    }
    return $found
}

# ---------------------------------------------------------------------------
# App payload
# ---------------------------------------------------------------------------

function Assert-SafeArchiveEntries {
    <# Throws unless every entry name is safe to extract into a directory.

       tar.exe extracts unconditionally and has no sanitization worth relying
       on: verified against a real bsdtar (the engine Windows bundles) that
       its extraction does reject a `..` traversal entry, but does NOT reject
       "pkg/D:/evil.py" -- it just writes a literal folder named "D:" on
       Linux, where a colon is an ordinary filename character. On Windows
       that component can instead be read as a drive reference during
       path-join, escaping the destination entirely.

       Checks are per *component*, not against the whole string. An earlier
       version anchored the drive-letter test with '^[A-Za-z]:', which
       matches only when the drive sits at the very start -- so the exact
       "pkg/D:/evil.py" this exists to stop went straight through it. That is
       the same mistake sorter/updater.py's Python-side extraction had (it
       tested name[1] of the whole name) and was fixed for; the two paths
       consume the same archives and must reject the same shapes. See
       Test-ArchiveEntryValidation.ps1, and _safe_members in updater.py. #>
    param([string[]]$EntryNames)

    foreach ($entryName in $EntryNames) {
        if ([string]::IsNullOrWhiteSpace($entryName)) { continue }

        if ($entryName.StartsWith('/') -or $entryName.StartsWith('\')) {
            # Covers UNC ("\\server\share") along with plain rooted paths.
            throw "Update archive contains an absolute path: $entryName"
        }

        foreach ($part in ($entryName -split '[\\/]')) {
            if ($part -eq '..') {
                throw "Update archive contains a traversal path: $entryName"
            }
            # Any colon anywhere in a component: a drive reference ("D:" or
            # "D:name") and an NTFS alternate data stream ("file.txt:hidden")
            # are the same character doing different damage, and neither is
            # legal in a filename this installer should ever write.
            if ($part.Contains(':')) {
                throw "Update archive contains a drive-qualified or stream path: $entryName"
            }
        }
    }
}

function Select-ReleaseAsset {
    <# Given a release API response, matches the sdist by its exact name --
       mirroring updater._expected_asset_name -- not "the first .tar.gz",
       which would let any unrelated tarball attached ahead of it become the
       installed tree. Returns $null if the release has no matching asset.
       Property access is guarded: Set-StrictMode turns a missing property
       into a terminating error, and a release with no assets is normal. #>
    param($Release)

    $tag = $Release.PSObject.Properties['tag_name'].Value
    $expected = "ai_case_sorter-$($tag -replace '^v', '').tar.gz"
    if (-not $Release.PSObject.Properties['assets']) { return $null }
    $asset = $Release.assets |
        Where-Object { $_.PSObject.Properties['name'] -and $_.name -eq $expected } |
        Select-Object -First 1
    if (-not $asset) { return $null }
    return [pscustomobject]@{ Tag = $tag; Url = $asset.browser_download_url }
}

function Get-ReleaseInfo {
    <# Release tag + archive URL, latest by default or a specific tag via
       -Version. Prefers the published sdist, which is the same artifact
       sorter/updater.py updates from; falls back to a source archive, and
       (only when no -Version was requested) to the default branch if there
       are no releases at all yet.

       The sdist matters because it is the only archive that carries
       sorter/_version.py (hatch-vcs stamps it at build time). A source archive
       has neither that file nor .git, so an install made from one reports
       0.0.0+unknown -- which parses as a pre-release, so every launch would
       see the current release as "newer" and re-prompt. sorter/apply_update.py
       stamps a version after an in-app update, but nothing does so here.

       -Version used to skip all of this and always fetch the raw source zip
       for the requested tag -- silently reintroducing the exact bug above
       for every pinned install. It now goes through the same lookup and
       asset-matching as the latest-release path. #>
    $releaseUrl = if ($Version) {
        "https://api.github.com/repos/$Repo/releases/tags/$Version"
    } else {
        "https://api.github.com/repos/$Repo/releases/latest"
    }
    try {
        $resp = Invoke-RestMethod -Uri $releaseUrl `
            -Headers @{ 'User-Agent' = 'CaseSorter-Installer' } -UseBasicParsing
    } catch {
        if ($Version) {
            # Distinct from "no releases yet" below: the caller asked for a
            # specific tag, so silently falling back to $DefaultBranch would
            # install something other than what was requested with no warning.
            throw "Could not find release '$Version'. Check the tag exists: https://github.com/$Repo/releases"
        }
        # A 404 here means either "no releases published yet" or "this repo is
        # not publicly readable" - the API gives an anonymous caller the same
        # answer for both. Say so, rather than reporting only the happy-path
        # guess and letting the download fail with a bare "Not Found".
        Write-Warn2 "No published release found (the repo may have none yet)."
        Write-Note  "Falling back to the current $DefaultBranch branch."
        return [pscustomobject]@{
            Tag = $DefaultBranch
            Url = "https://github.com/$Repo/archive/refs/heads/$DefaultBranch.zip"
        }
    }

    $tag = $resp.PSObject.Properties['tag_name'].Value
    $found = Select-ReleaseAsset -Release $resp
    if ($found) { return $found }
    Write-Warn2 "Release $tag has no matching sdist; falling back to the source archive."
    Write-Note  "The app will report its version as 0.0.0 until the first in-app update."
    return [pscustomobject]@{ Tag = $tag; Url = "https://github.com/$Repo/archive/refs/tags/$tag.zip" }
}

function Install-App {
    param([string]$Url, [string]$Tag, [string]$Dest)

    $work = Join-Path $env:TEMP "casesorter-install-$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $work -Force | Out-Null
    try {
        $isTar = $Url -like '*.tar.gz'
        $zip = Join-Path $work $(if ($isTar) { 'app.tar.gz' } else { 'app.zip' })
        Write-Note "Downloading $Tag..."
        try {
            Invoke-WebRequest -Uri $Url -OutFile $zip -UseBasicParsing
        } catch {
            $status = $null
            if ($_.Exception.PSObject.Properties['Response'] -and $_.Exception.Response) {
                $status = [int]$_.Exception.Response.StatusCode
            }
            if ($status -eq 404) {
                # The overwhelmingly common cause, and invisible from the bare
                # "Not Found" that Invoke-WebRequest reports on its own.
                throw @"
Could not download the app (HTTP 404).

  $Url

The most likely reason is that the repository is private, or the release tag
does not exist. An anonymous download - which is all this installer does -
needs the repository to be publicly readable.

If you are the maintainer: make the repository public, or publish a release
whose tag matches what you asked for.
"@
            }
            throw "Could not download the app from $Url : $($_.Exception.Message)"
        }

        Write-Note "Extracting..."
        $unpack = Join-Path $work 'unpacked'
        New-Item -ItemType Directory -Path $unpack -Force | Out-Null
        if ($isTar) {
            # bsdtar, shipped in Windows since 10 1803. Expand-Archive cannot
            # read .tar.gz at all, so there is no PowerShell-native fallback.
            $tarExe = Get-Command tar.exe -ErrorAction SilentlyContinue
            if (-not $tarExe) {
                throw @"
This installer needs tar.exe, which ships with Windows 10 (1803) and later.

Your Windows appears to be older. Install a newer Windows, or download and
extract the release archive by hand:

  $Url
"@
            }
            # List and vet every entry before tar.exe writes a byte -- see
            # Assert-SafeArchiveEntries for why tar's own behaviour is not
            # something to lean on.
            $listing = @(& tar.exe -tf $zip)
            if ($LASTEXITCODE -ne 0) {
                throw "Could not read the downloaded archive (tar exited $LASTEXITCODE)."
            }
            # @() above keeps a single-entry archive an array rather than a
            # bare string; an empty listing means tar found nothing to
            # extract, which is never a real sdist.
            if ($listing.Count -eq 0) {
                throw "The downloaded archive is empty."
            }
            Assert-SafeArchiveEntries -EntryNames $listing

            & tar.exe -xzf $zip -C $unpack
            if ($LASTEXITCODE -ne 0) {
                throw "Could not extract the downloaded archive (tar exited $LASTEXITCODE)."
            }
        } else {
            Expand-Archive -Path $zip -DestinationPath $unpack -Force
        }

        # Both the sdist (<name>-<version>/) and GitHub's source archives
        # (<repo>-<tag>/) nest everything under one top-level directory.
        $entries = @(Get-ChildItem -Path $unpack)
        $src = if ($entries.Count -eq 1 -and $entries[0].PSIsContainer) {
            $entries[0].FullName
        } else { $unpack }

        if (-not (Test-Path (Join-Path $src 'main.py'))) {
            throw "The downloaded archive does not look like the app (no main.py)."
        }

        New-Item -ItemType Directory -Path $Dest -Force | Out-Null

        # Copy over the top. The venv (.venv), the local uv install (.uv),
        # and any local .env are left alone; user data lives outside $Dest
        # entirely, so nothing here can touch it.
        Write-Note "Installing to $Dest"
        Copy-Item -Path (Join-Path $src '*') -Destination $Dest -Recurse -Force
    } finally {
        Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function New-Shortcuts {
    param([string]$Dest)

    $target = Join-Path $Dest 'start.bat'
    if (-not (Test-Path $target)) { return }

    $startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    $lnk = Join-Path $startMenu 'AI Case Sorter.lnk'
    try {
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($lnk)
        $sc.TargetPath = $target
        $sc.WorkingDirectory = $Dest
        $sc.Description = 'AI Case Sorter'
        $sc.WindowStyle = 7   # start minimised; start.bat is a console host
        $icon = Join-Path $Dest 'installer\casesorter.ico'
        if (Test-Path $icon) { $sc.IconLocation = $icon }
        $sc.Save()
        Write-Ok "Start Menu shortcut created."
    } catch {
        Write-Warn2 "Could not create the Start Menu shortcut: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Skipped when the file is dot-sourced (`. install-windows.ps1`), which is how
# Test-ArchiveEntryValidation.ps1 gets at the functions above. Without this,
# loading the script to test one function runs a real install.
if ($MyInvocation.InvocationName -eq '.') { return }

Write-Host ""
Write-Host "  AI Case Sorter - Windows installer" -ForegroundColor White
Write-Host "  ----------------------------------" -ForegroundColor DarkGray
Write-Host ""

if (Test-Path (Join-Path $InstallDir 'main.py')) {
    Write-Step "Updating the existing install at $InstallDir"
} else {
    Write-Step "Installing to $InstallDir"
}

Write-Step "Checking for Python $PythonMin or newer (with Tcl/Tk)"
$python = Get-PythonCommand
if ($python) {
    Write-Ok "Found $python"
} else {
    $python = Install-Python
    Write-Ok "Installed $python"
}

Write-Step "Fetching the app"
$release = Get-ReleaseInfo
Install-App -Url $release.Url -Tag $release.Tag -Dest $InstallDir
Write-Ok "$($release.Tag) installed."

Write-Step "Creating shortcuts"
New-Shortcuts -Dest $InstallDir

Write-Host ""
Write-Ok "Done. The app is installed at:"
Write-Host "      $InstallDir"
Write-Ok "Your models and settings are kept separately at:"
Write-Host "      $env:LOCALAPPDATA\CaseSorter"
Write-Host ""
Write-Note "First launch installs the Python dependencies and takes a few minutes."
Write-Note "After that, updates are offered inside the app - no need to re-run this."
Write-Host ""

if (-not $NoLaunch) {
    Write-Step "Starting the app"
    Start-Process -FilePath (Join-Path $InstallDir 'start.bat') -WorkingDirectory $InstallDir
}
