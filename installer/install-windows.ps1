<#
.SYNOPSIS
    Installs (or updates) the AI Case Sorter on Windows. No git required.

.DESCRIPTION
    Provisions the two things a non-developer machine is missing - a Python
    runtime and a copy of the app - then hands off to start.bat, which just
    calls bootstrap.py: that's what owns the virtualenv and dependency
    install now (via uv), not this script or start.bat itself.

    Deliberately git-free. `git pull` over HTTPS and a release tarball (tar.gz) over HTTPS
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
    Install a specific release tag of the app (e.g. "1.0.0") instead of the
    latest. Omit it unless you are pinning, downgrading, or testing a tag -
    the default is whatever /releases/latest resolves to.

    This is the *app's* release, not a version of this script: the installer
    ships inside the release it installs and has no version of its own.

    Tags carry no "v" prefix - .github/actions/check-version rejects that form
    at tag time, so "v1.0.0" would 404 against the releases API. The example
    names a real published tag on purpose: an invented one is a 404 for
    anyone who copies it.

.PARAMETER NoLaunch
    Install without starting the app afterwards.

.PARAMETER Repo
    Override the "owner/repo" to install from. Mirrors sorter/update/updater.py's
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

# This Python only runs bootstrap.py; uv provisions the app's own. So
# $PythonMin tracks requires-python, while what we install tracks
# .python-version, letting uv reuse it instead of fetching a second.
$PythonMin      = [Version]'3.12'   # keep in step with pyproject.toml
$PythonWinget   = 'Python.Python.3.13'
# Explicit patch: python.org publishes no "latest 3.13" URL. Check the file
# exists when bumping - an unreachable one 404s at download.
$PythonFallback = '3.13.14'         # keep in step with .python-version

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
      Returns the path to a python.exe meeting $PythonMin, or $null. This
      interpreter only ever runs bootstrap.py; uv provisions the one the app
      itself runs on, so nothing here needs the app's own dependencies.
    #>
    # Probes are expected to fail, and PowerShell 5.1 turns native stderr into
    # a terminating ErrorRecord under 'Stop'. Function-scoped.
    $ErrorActionPreference = 'SilentlyContinue'

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

    # Newest first, and nothing below $PythonMin: a 3.11 or 3.10 entry can
    # only ever be probed and rejected, which costs an interpreter launch
    # apiece to learn what the version number already said.
    $candidates += @(
        "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    )

    foreach ($candidate in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path $candidate)) { continue }
        # Skip the Microsoft Store "app execution alias" stub. It exists on a
        # stock Windows install with no Python behind it: run with arguments it
        # just exits 9009, and bare it opens the Store. Either way there is no
        # interpreter here to find.
        if ($candidate -like '*\WindowsApps\*') { continue }
        try {
            $out = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
            if ([Version]$out.Trim() -ge $PythonMin) { return $candidate }
        } catch { continue }
    }
    return $null
}

function Update-PathFromRegistry {
    <# PrependPath edits the persistent PATH, which Windows broadcasts to new
       processes only -- and Start-Process inherits this one's environment.
       Without this the start.bat launched at the end cannot see the Python
       just installed. Needed after every install path, not just
       python.org's. #>
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

# Where both provisioning paths land a per-user install, e.g. Python313.
function Get-JustInstalledPython {
    $tag = 'Python' + (($PythonFallback -split '\.')[0, 1] -join '')
    foreach ($root in @($env:LOCALAPPDATA, $env:ProgramFiles)) {
        if (-not $root) { continue }
        $exe = Join-Path $root "Programs\Python\$tag\python.exe"
        if (Test-Path $exe) { return $exe }
    }
    return $null
}

function Install-Python {
    Write-Step "Installing Python (none suitable was found)"

    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Note "Using winget: $PythonWinget"
        try {
            # Out-Host: a function returns everything written to the success
            # stream, so winget's output would otherwise be prepended to the
            # path this returns.
            & winget install --id $PythonWinget --exact --source winget `
                --accept-package-agreements --accept-source-agreements `
                --scope user --silent | Out-Host
            # winget reports failure by exit code, not by throwing.
            if ($LASTEXITCODE -ne 0) {
                Write-Warn2 "winget exited $LASTEXITCODE."
            }
        } catch {
            Write-Warn2 "winget failed: $($_.Exception.Message)"
        }
        # Before the probe: winget edits the persistent PATH only.
        Update-PathFromRegistry
        # The one we just installed wins over whatever else the restored PATH
        # turned up -- otherwise a Python the process PATH could not see (another
        # user's, or one added since login) silently takes its place, and the
        # install we just paid for goes unused.
        $found = Get-JustInstalledPython
        if (-not $found) { $found = Get-PythonCommand }
        if ($found) { return $found }
        Write-Warn2 "winget did not produce a usable Python; falling back to python.org."
    } else {
        Write-Note "winget is not available; using python.org."
    }

    # python.org fallback. Per-user and silent.
    $arch = if ([Environment]::Is64BitOperatingSystem) { 'amd64' } else { 'win32' }
    $url = "https://www.python.org/ftp/python/$PythonFallback/python-$PythonFallback-$arch.exe"
    $exe = Join-Path $env:TEMP "python-$PythonFallback-$arch.exe"

    Write-Note "Downloading $url"
    try {
        Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
    } catch {
        throw "Could not download Python from $url : $($_.Exception.Message)"
    }

    Write-Note "Running the installer (per-user, silent). This takes a minute..."
    $proc = Start-Process -FilePath $exe -Wait -PassThru -ArgumentList @(
        '/quiet', 'InstallAllUsers=0', 'PrependPath=1',
        'Include_pip=1', 'Include_launcher=1'
    )
    Remove-Item $exe -Force -ErrorAction SilentlyContinue
    if ($proc.ExitCode -ne 0) {
        # 1602 is the user cancelling a UAC/consent prompt, and 1603 is the
        # catch-all MSI failure that a same-version install already present
        # can produce. Naming them beats a bare number the user has to search.
        $hint = switch ($proc.ExitCode) {
            1602 { " (the install was cancelled)" }
            1603 { " (a fatal installer error - a repair or reboot may be needed)" }
            default { "" }
        }
        throw "The Python installer exited with code $($proc.ExitCode)$hint."
    }

    # PrependPath only affects *new* processes, so this shell still can't see
    # it - re-read the user PATH rather than trusting `where python`.
    Update-PathFromRegistry

    $found = Get-JustInstalledPython
    if (-not $found) { $found = Get-PythonCommand }
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
       the same mistake sorter/update/updater.py's Python-side extraction had (it
       tested name[1] of the whole name) and was fixed for; the two paths
       consume the same archives and must reject the same shapes. See
       tests/Test-ArchiveEntryValidation.ps1, and _safe_members in updater.py. #>
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

function Test-LooksLikeTheApp {
    <# Whether an extracted archive is this app, in either supported layout.

       Kept in lock-step with sorter/update/updater.py's REQUIRED_ENTRY_SETS,
       down to the semantics: a set matches only when *every* entry in it is
       present, and any one set matching is enough. Checking `main.py` alone
       is what made these two disagree -- a bare main.py with no package was
       accepted here and rejected by the updater, so an archive could install
       and then never update.

       Both arms stay indefinitely. A release published before the src/ move
       is still a legitimate thing to install from a pinned -Version. #>
    param([Parameter(Mandatory)][string]$Root)

    $requiredEntrySets = @(
        @('main.py', 'sorter\__init__.py'),   # flat, up to and including 1.1.0
        @('src\sorter\__init__.py')           # src/ layout
    )

    foreach ($entrySet in $requiredEntrySets) {
        $complete = $true
        foreach ($entry in $entrySet) {
            if (-not (Test-Path (Join-Path $Root $entry))) {
                $complete = $false
                break
            }
        }
        if ($complete) { return $true }
    }
    return $false
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
       sorter/update/updater.py updates from; falls back to a source archive, and
       (only when no -Version was requested) to the default branch if there
       are no releases at all yet.

       The sdist matters because it is the only archive that carries
       sorter/_version.py (hatch-vcs stamps it at build time). A source archive
       has neither that file nor .git, so an install made from one reports
       0.0.0+unknown -- which parses as a pre-release, so every launch would
       see the current release as "newer" and re-prompt. sorter/update/apply_update.py
       stamps a version after an in-app update, but nothing does so here.

       -Version used to skip all of this and always fetch the raw source
       archive (/archive/refs/tags/<tag>.tar.gz) for the requested tag --
       silently reintroducing the exact bug above for every pinned install.
       It now goes through the same lookup and asset-matching as the
       latest-release path. #>
    $releaseUrl = if ($Version) {
        "https://api.github.com/repos/$Repo/releases/tags/$Version"
    } else {
        "https://api.github.com/repos/$Repo/releases/latest"
    }
    try {
        $resp = Invoke-RestMethod -Uri $releaseUrl `
            -Headers @{ 'User-Agent' = 'CaseSorter-Installer' } -UseBasicParsing
    } catch {
        # Only a confirmed 404 means "no such release". Anything else -- and
        # that includes no response at all, which is what DNS failures, resets
        # and timeouts give you -- means "could not ask", which is a different
        # answer and must not be reported as a missing tag or fall back to a
        # branch archive that has no _version.py.
        $status = $null
        if ($_.Exception.PSObject.Properties['Response'] -and $_.Exception.Response) {
            try { $status = [int]$_.Exception.Response.StatusCode } catch { }
        }
        if ($status -ne 404) {
            $what = if ($status) { "HTTP $status" } else { "no response - network or DNS failure" }
            throw @"
Could not ask GitHub which release to install ($what).

  $releaseUrl

This is not "no releases yet" - the request itself failed. A 403 usually
means GitHub's rate limit for anonymous requests (60/hour per IP); 5xx and
timeouts mean GitHub or the network in between. Waiting and re-running is
normally enough.

Refusing to fall back to the $DefaultBranch branch, because that installs a
tree with no version stamp and would report 0.0.0 forever.
"@
        }

        if ($Version) {
            # The caller asked for a specific tag, so falling back to
            # $DefaultBranch would install something other than what was
            # requested with no warning.
            throw "Could not find release '$Version'. Check the tag exists: https://github.com/$Repo/releases"
        }

        # A 404 here means either "no releases published yet" or "this repo is
        # not publicly readable" - the API gives an anonymous caller the same
        # answer for both. Say so, rather than reporting only the happy-path
        # guess and letting the download fail with a bare "Not Found".
        Write-Warn2 "No published release found (the repo may have none yet)."
        Write-Note  "Reason: $($_.Exception.Message)"
        Write-Note  "Falling back to the current $DefaultBranch branch."
        return [pscustomobject]@{
            Tag = $DefaultBranch
            Url = "https://github.com/$Repo/archive/refs/heads/$DefaultBranch.tar.gz"
        }
    }

    $tag = $resp.PSObject.Properties['tag_name'].Value
    $found = Select-ReleaseAsset -Release $resp
    if ($found) { return $found }
    Write-Warn2 "Release $tag has no matching sdist; falling back to the source archive."
    Write-Note  "The app will report its version as 0.0.0 until the first in-app update."
    return [pscustomobject]@{ Tag = $tag; Url = "https://github.com/$Repo/archive/refs/tags/$tag.tar.gz" }
}

function Install-App {
    param([string]$Url, [string]$Tag, [string]$Dest)

    $work = Join-Path $env:TEMP "casesorter-install-$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $work -Force | Out-Null
    try {
        # Every path into here is a .tar.gz now, so there is no archive-type
        # branch left; Expand-Archive cannot read one anyway.
        $targz = Join-Path $work 'app.tar.gz'
        Write-Note "Downloading $Tag..."
        try {
            Invoke-WebRequest -Uri $Url -OutFile $targz -UseBasicParsing
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
        $listing = @(& tar.exe -tf $targz)
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

        & tar.exe -xzf $targz -C $unpack
        if ($LASTEXITCODE -ne 0) {
            throw "Could not extract the downloaded archive (tar exited $LASTEXITCODE)."
        }

        # Both the sdist (<name>-<version>/) and GitHub's source archives
        # (<repo>-<tag>/) nest everything under one top-level directory.
        $entries = @(Get-ChildItem -Path $unpack)
        $src = if ($entries.Count -eq 1 -and $entries[0].PSIsContainer) {
            $entries[0].FullName
        } else { $unpack }

        if (-not (Test-LooksLikeTheApp -Root $src)) {
            throw "The downloaded archive does not look like the app (no src/sorter/__init__.py, and no main.py + sorter/__init__.py)."
        }

        New-Item -ItemType Directory -Path $Dest -Force | Out-Null

        # Copy over the top. The venv (.venv), the local uv install (.uv),
        # and any local .env are left alone; user data lives outside $Dest
        # entirely, so nothing here can touch it.
        Write-Note "Installing to $Dest"
        Copy-Item -Path (Join-Path $src '*') -Destination $Dest -Recurse -Force

        # Drop cached bytecode from the tree we just overwrote. Python
        # invalidates a .pyc on source mtime+size, and an sdist normalises
        # every mtime to the same constant -- so a same-size replacement (any
        # version string, say '1.0.0' -> '0.3.0') passes both checks and the
        # stale .pyc wins. The app then runs the previous release's code.
        # .venv/.uv are skipped: their caches belong to packages this did not
        # touch. sorter/update/apply_update.py gets this for free, since its stale
        # sweep already removes .pyc the new tree does not ship.
        Get-ChildItem -LiteralPath $Dest -Directory -Filter '__pycache__' -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notlike '*\.venv\*' -and $_.FullName -notlike '*\.uv\*' } |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
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
# tests/Test-ArchiveEntryValidation.ps1 gets at the functions above. Without this,
# loading the script to test one function runs a real install.
if ($MyInvocation.InvocationName -eq '.') { return }

# Data root, not the install folder, which this script and the updater both
# overwrite. Mirrors sorter/paths.py's logs_dir(), not importable here since
# there may be no Python yet. Timestamped, never pruned; a few KB each.
$LogDir = if ($env:CASESORTER_DATA_DIR) {
    Join-Path $env:CASESORTER_DATA_DIR 'logs'
} else {
    Join-Path $env:LOCALAPPDATA 'CaseSorter\logs'
}
$LogFile = $null
try {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    $LogFile = Join-Path $LogDir ("install-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
    Start-Transcript -Path $LogFile -Force | Out-Null
} catch {
    # Transcription can be disabled by policy, and the data root can be
    # unwritable. Neither is a reason to refuse to install.
    $LogFile = $null
}

try {
    Write-Host ""
    Write-Host "  AI Case Sorter - Windows installer" -ForegroundColor White
    Write-Host "  ----------------------------------" -ForegroundColor DarkGray
    Write-Host ""

    # Recorded before anything can fail, because these are the answers to the
    # first questions asked about any install that went wrong, and by then the
    # machine is not available to ask.
    Write-Note "Windows      : $([Environment]::OSVersion.Version) ($(if ([Environment]::Is64BitOperatingSystem) { 'x64' } else { 'x86' }))"
    Write-Note "PowerShell   : $($PSVersionTable.PSVersion)"
    Write-Note "Repo         : $Repo$(if ($Version) { " (pinned to $Version)" })"
    Write-Note "tar.exe      : $(if (Get-Command tar.exe -ErrorAction SilentlyContinue) { 'present' } else { 'MISSING' })"
    Write-Note "winget       : $(if (Get-Command winget -ErrorAction SilentlyContinue) { 'present' } else { 'not installed' })"
    if ($LogFile) { Write-Note "Log          : $LogFile" }
    Write-Host ""

    if ((Test-Path (Join-Path $InstallDir 'main.py')) -or (Test-Path (Join-Path $InstallDir 'src\sorter\__init__.py'))) {
        Write-Step "Updating the existing install at $InstallDir"
    } else {
        Write-Step "Installing to $InstallDir"
    }

    Write-Step "Checking for Python $PythonMin or newer"
    $python = Get-PythonCommand
    if ($python) {
        Write-Ok "Found $python"
    } else {
        $python = Install-Python
        Write-Ok "Installed $python"
    }

    Write-Step "Fetching the app"
    $release = Get-ReleaseInfo
    Write-Note "Source: $($release.Url)"
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
        $starter = Join-Path $InstallDir 'start.bat'
        if (-not (Test-Path $starter)) {
            throw "The install completed but $starter is missing."
        }
        # Not -Wait: the app runs until the user closes it, in a console this
        # script never sees - hence bootstrap.py's own log.
        Start-Process -FilePath $starter -WorkingDirectory $InstallDir

        # Only promise the launch log if the installed tree writes one: a
        # newer installer against an older release is routine when testing.
        $installedBootstrap = Join-Path $InstallDir 'bootstrap.py'
        $logsStartup = (Test-Path $installedBootstrap) -and
                       (Select-String -Path $installedBootstrap -Pattern 'def open_log' -Quiet)
        if ($logsStartup) {
            Write-Note "The app logs its startup to $LogDir\launch.log"
            Write-Note "If no window appears, that file says why."
        } else {
            Write-Note "A window should appear shortly - first launch takes a few minutes."
        }
    }
} catch {
    Write-Host ""
    Write-Host "  Install failed." -ForegroundColor Red
    Write-Host ""
    Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    # Noise to the user, but the point of the log for whoever they send it to;
    # the console is the only stream the transcript records.
    if ($_.ScriptStackTrace) {
        Write-Host "  Where:" -ForegroundColor DarkGray
        foreach ($frame in $_.ScriptStackTrace -split "`n") {
            Write-Host "    $($frame.TrimEnd())" -ForegroundColor DarkGray
        }
        Write-Host ""
    }
    if ($LogFile) {
        Write-Host "  A full log of this attempt is at:" -ForegroundColor DarkGray
        Write-Host "    $LogFile"
        Write-Host ""
    }
    exit 1
} finally {
    # Guarded: Stop-Transcript throws if transcription never started, and that
    # error would replace the real one on the way out of the catch above.
    try { Stop-Transcript | Out-Null } catch { }
}
