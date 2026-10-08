#!/bin/sh
# Installs (or updates) the AI Case Sorter on Linux and macOS. No git needed.
#
# Downloads a release's sdist, checks it against the SHA-256 GitHub publishes
# for that asset, unpacks it into a per-user folder and puts a launcher on
# ~/.local/bin. The behaviour mirrors install-windows.ps1; the rules it shares
# with the in-app updater (tag pattern, asset name, digest policy, archive
# entry checks) mirror sorter/update/updater.py and must stay in step with it.
#
# POSIX sh on purpose: it has to run on macOS's stock /bin/sh (bash 3.2) and
# on Linux dash, with no GNU-only flags and nothing beyond curl and tar.
#
# Usage: sh install-unix.sh [--prefix DIR] [--version TAG] [--force]
#                           [--no-bootstrap] [--repo OWNER/REPO] [--help]
#
# Testing hooks (not for users): CASESORTER_INSTALL_LIB=1 when sourcing
# defines the functions without installing anything;
# CASESORTER_INSTALL_RELEASE_JSON and CASESORTER_INSTALL_ARCHIVE substitute
# local files for the release lookup and the download.

set -eu
# Bytewise ranges in the character classes below, whatever the user's locale.
LC_ALL=C
export LC_ALL

DEFAULT_REPO='sjseth/AI-Case-Sorter-Py'
LAUNCHER_NAME='ai-case-sorter'
LAUNCHER_MARKER='# AI Case Sorter launcher (written by install-unix.sh)'
# Kept in step with PROTECTED_TOP_LEVEL and PRUNE_ROOTS in
# sorter/update/apply_update.py.
PROTECTED_TOP_LEVEL='.git .venv venv .uv data .env portable.txt'
PRUNE_ROOTS='src/sorter sorter'
# bootstrap.py's SETUP_ONLY_FLAG; an older release would pass it to the app.
SETUP_ONLY_FLAG='--setup-only'

LOG_FILE=''
WORK_DIR=''

# ---------------------------------------------------------------------------
# Output. Everything the user sees also goes to the install log, best-effort.
# ---------------------------------------------------------------------------

_emit() {
    printf '%s\n' "$1"
    if [ -n "$LOG_FILE" ]; then
        printf '%s\n' "$1" >>"$LOG_FILE" 2>/dev/null || true
    fi
}
step() { _emit "==> $1"; }
note() { _emit "    $1"; }
warn() { _emit "    WARNING: $1"; }

die() {
    _emit ''
    _emit '  Install failed.'
    _emit ''
    _emit "  $1"
    _emit ''
    if [ -n "$LOG_FILE" ]; then
        _emit '  A full log of this attempt is at:'
        _emit "    $LOG_FILE"
        _emit ''
    fi
    exit 1
}

# ---------------------------------------------------------------------------
# Rules shared with sorter/update/updater.py
# ---------------------------------------------------------------------------

# Succeeds when $1 matches updater._TAG_RE: v?[0-9A-Za-z][0-9A-Za-z._-]{0,63}
valid_tag() {
    vt_tag=$1
    case $vt_tag in
        '' | *[!0-9A-Za-z._-]*) return 1 ;;
        [!0-9A-Za-z]*) return 1 ;;
    esac
    # 64 characters fit with or without the optional v; 65 only with it.
    [ "${#vt_tag}" -le 64 ] && return 0
    [ "${#vt_tag}" -eq 65 ] || return 1
    case $vt_tag in
        v[0-9A-Za-z]*) return 0 ;;
    esac
    return 1
}

valid_repo() {
    case $1 in
        */*/* | /* | */ | *[!A-Za-z0-9._/-]*) return 1 ;;
        */*) return 0 ;;
    esac
    return 1
}

# updater._expected_asset_name: strip one lowercase v, exactly as release.yml.
expected_asset_name() {
    printf 'ai_case_sorter-%s.tar.gz\n' "${1#v}"
}

# Prints "<kind> <value>" exactly as updater.classify_digest returns it:
# "sha256 <lowercase hex>", "absent ", "unsupported <algorithm>" or
# "malformed ".
classify_digest() {
    cd_text=$(printf '%s' "${1-}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    if [ -z "$cd_text" ]; then
        echo 'absent '
        return 0
    fi
    case $cd_text in
        *:*) ;;
        *)
            echo 'malformed '
            return 0
            ;;
    esac
    cd_algo=$(printf '%s' "${cd_text%%:*}" | tr '[:upper:]' '[:lower:]')
    cd_value=${cd_text#*:}
    case $cd_algo in
        '' | *[!a-z0-9-]*)
            echo 'malformed '
            return 0
            ;;
    esac
    if [ -z "$cd_value" ]; then
        echo 'malformed '
        return 0
    fi
    if [ "$cd_algo" != sha256 ]; then
        echo "unsupported $cd_algo"
        return 0
    fi
    case $cd_value in
        *[!0-9a-fA-F]*)
            echo 'malformed '
            return 0
            ;;
    esac
    if [ "${#cd_value}" -ne 64 ]; then
        echo 'malformed '
        return 0
    fi
    echo "sha256 $(printf '%s' "$cd_value" | tr '[:upper:]' '[:lower:]')"
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d ' ' -f 1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d ' ' -f 1
    else
        return 1
    fi
}

# Checks file $1 against digest $2. Returns 0 when verified, 2 when there was
# nothing it could check (TLS alone, warned), and 1 when it must be refused.
verify_download() {
    vd_check=$(classify_digest "${2-}")
    vd_kind=${vd_check%% *}
    vd_value=${vd_check#* }
    if [ "$vd_kind" = malformed ]; then
        _emit "    The release publishes a checksum that can't be read ('${2-}'), so the download can't be verified."
        return 1
    fi
    if ! vd_actual=$(sha256_of "$1") || [ -z "$vd_actual" ]; then
        if [ "$vd_kind" = sha256 ]; then
            _emit '    Neither sha256sum nor shasum is available, so the published checksum cannot be checked.'
            return 1
        fi
        vd_actual='(unknown: no sha256sum or shasum)'
    fi
    case $vd_kind in
        sha256)
            if [ "$vd_actual" != "$vd_value" ]; then
                _emit '    The download does not match the SHA-256 checksum GitHub published for it.'
                _emit "      expected: $vd_value"
                _emit "      got:      $vd_actual"
                return 1
            fi
            note 'SHA-256 verified against the published checksum.'
            return 0
            ;;
        unsupported)
            warn "Not verified: the published checksum is $vd_value, which this installer can't check."
            ;;
        *)
            warn 'Not verified: no checksum is published for this download.'
            ;;
    esac
    note "Relying on the HTTPS connection alone. SHA-256 of the download: $vd_actual"
    return 2
}

# The same entry-name shapes updater._safe_members rejects: absolute paths,
# '..' components, and ':' or '\' in any component.
check_entry_name() {
    ce_name=$1
    case $ce_name in
        /*)
            _emit "    Archive contains an absolute path: $ce_name"
            return 1
            ;;
        *\\*)
            _emit "    Archive contains a backslash path: $ce_name"
            return 1
            ;;
        *:*)
            _emit "    Archive contains a drive-qualified path: $ce_name"
            return 1
            ;;
        .. | ../* | */.. | */../*)
            _emit "    Archive contains a traversal path: $ce_name"
            return 1
            ;;
    esac
    return 0
}

# Vets every entry of the tarball $1 before anything is extracted: names as
# above, and nothing but regular files and directories (no symlinks,
# hardlinks or devices). GNU tar and bsdtar both flag the type in the first
# column of a verbose listing, 'h' included for hardlinks.
check_archive() {
    ca_names="$WORK_DIR/names.txt"
    ca_types="$WORK_DIR/types.txt"
    if ! tar -tzf "$1" >"$ca_names" 2>/dev/null || ! tar -tvzf "$1" >"$ca_types" 2>/dev/null; then
        _emit '    Could not read the downloaded archive.'
        return 1
    fi
    if [ ! -s "$ca_names" ]; then
        _emit '    The downloaded archive is empty.'
        return 1
    fi
    while IFS= read -r ca_line; do
        check_entry_name "$ca_line" || return 1
    done <"$ca_names"
    ca_bad=$(cut -c 1 "$ca_types" | grep -v '^[-d]$' | head -n 1) || true
    if [ -n "$ca_bad" ]; then
        _emit "    Archive contains a non-regular-file entry (type '$ca_bad'):"
        _emit "      $(grep -v '^[-d]' "$ca_types" | head -n 1)"
        return 1
    fi
    return 0
}

# updater.REQUIRED_ENTRY_SETS: the src/ layout, or the pre-2.0 flat layout.
looks_like_the_app() {
    [ -f "$1/src/sorter/__init__.py" ] && return 0
    [ -f "$1/main.py" ] && [ -f "$1/sorter/__init__.py" ]
}

# ---------------------------------------------------------------------------
# Release JSON
# ---------------------------------------------------------------------------

# Flattens JSON on stdin to "<path><TAB><value>" lines for every scalar,
# e.g. ".assets[0].name<TAB>ai_case_sorter-2.3.0.tar.gz". String values keep
# their escapes verbatim, so anything carrying one fails the strict checks
# applied to tags, names and URLs; null prints as empty.
json_flatten() {
    awk '
    { doc = doc $0 " " }
    function path(   p, k) {
        p = ""
        for (k = 1; k <= d; k++) p = p (t[k] == "o" ? "." key[k] : "[" ix[k] "]")
        return p
    }
    END {
        n = length(doc); i = 1; d = 0; want = 0
        while (i <= n) {
            c = substr(doc, i, 1)
            if (c == "{") { d++; t[d] = "o"; want = 1; i++ }
            else if (c == "[") { d++; t[d] = "a"; ix[d] = 0; i++ }
            else if (c == "}" || c == "]") { d--; i++ }
            else if (c == ",") { if (t[d] == "o") want = 1; else ix[d]++; i++ }
            else if (c == "\"") {
                j = i + 1
                while (j <= n) {
                    e = substr(doc, j, 1)
                    if (e == "\\") j += 2
                    else if (e == "\"") break
                    else j++
                }
                s = substr(doc, i + 1, j - i - 1); i = j + 1
                if (t[d] == "o" && want) { key[d] = s; want = 0 }
                else printf "%s\t%s\n", path(), s
            }
            else if (c ~ /[-0-9a-z]/) {
                j = i
                while (j <= n && substr(doc, j, 1) ~ /[-+.0-9a-zA-Z]/) j++
                s = substr(doc, i, j - i); i = j
                printf "%s\t%s\n", path(), (s == "null" ? "" : s)
            }
            else i++
        }
    }'
}

# Value at path $2 in the flattened file $1 (empty when absent).
json_get() {
    awk -F '\t' -v p="$2" '$1 == p { print substr($0, length(p) + 2); exit }' "$1"
}

# Sets ASSET_URL and ASSET_DIGEST from the flattened release $1 for the
# sdist of tag $2, matched by exact name as updater._pick_asset does. Fails
# when the release carries no such asset.
select_asset() {
    sa_expected=$(expected_asset_name "$2")
    sa_index=$(awk -F '\t' -v want="$sa_expected" '
        $1 ~ /^\.assets\[[0-9]+\]\.name$/ && substr($0, length($1) + 2) == want {
            sub(/^\.assets\[/, "", $1); sub(/\]\.name$/, "", $1); print $1; exit
        }' "$1")
    [ -n "$sa_index" ] || return 1
    ASSET_URL=$(json_get "$1" ".assets[$sa_index].browser_download_url")
    ASSET_DIGEST=$(json_get "$1" ".assets[$sa_index].digest")
    [ -n "$ASSET_URL" ]
}

valid_download_url() {
    case $1 in
        https://*) ;;
        *) return 1 ;;
    esac
    case $1 in
        *[!A-Za-z0-9._~:/?#@!\$\&\(\)*+,\;=%-]*) return 1 ;;
    esac
    return 0
}

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# sorter/paths.py's app_data_dir(), for an install at $1.
data_root() {
    if [ -n "${CASESORTER_DATA_DIR:-}" ]; then
        # Literal tildes: expanded here as Path.expanduser() does.
        # shellcheck disable=SC2088
        case $CASESORTER_DATA_DIR in
            '~') printf '%s\n' "$HOME" ;;
            '~/'*) printf '%s/%s\n' "$HOME" "${CASESORTER_DATA_DIR#\~/}" ;;
            *) printf '%s\n' "$CASESORTER_DATA_DIR" ;;
        esac
    elif [ -f "$1/portable.txt" ]; then
        printf '%s/data\n' "$1"
    elif [ "$(uname -s)" = Darwin ]; then
        printf '%s/Library/Application Support/CaseSorter\n' "$HOME"
    else
        printf '%s/CaseSorter\n' "${XDG_DATA_HOME:-$HOME/.local/share}"
    fi
}

is_previous_install() {
    [ -f "$1/src/sorter/__init__.py" ] || [ -f "$1/main.py" ] || [ -f "$1/bootstrap.py" ]
}

dir_is_empty() {
    [ -z "$(ls -A "$1" 2>/dev/null)" ]
}

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

# Downloads $1 to $2; prints the HTTP status ("000" when nothing answered).
http_get() {
    curl -sSL --proto '=https' --proto-redir '=https' \
        -H 'User-Agent: CaseSorter-Installer' \
        -o "$2" -w '%{http_code}' "$1" 2>>"$WORK_DIR/curl.err" || true
}

# Sets REL_TAG, REL_URL and REL_DIGEST for $VERSION (or the latest release).
resolve_release() {
    rr_json="$WORK_DIR/release.json"
    if [ -n "${CASESORTER_INSTALL_RELEASE_JSON:-}" ]; then
        cp "$CASESORTER_INSTALL_RELEASE_JSON" "$rr_json"
    else
        if [ -n "$VERSION" ]; then
            rr_api="https://api.github.com/repos/$REPO/releases/tags/$VERSION"
        else
            rr_api="https://api.github.com/repos/$REPO/releases/latest"
        fi
        rr_status=$(http_get "$rr_api" "$rr_json")
        case $rr_status in
            200) ;;
            404)
                if [ -n "$VERSION" ]; then
                    die "Could not find release '$VERSION'. Check the tag exists: https://github.com/$REPO/releases"
                fi
                die "No published release found at https://github.com/$REPO/releases (or the repository is not public)."
                ;;
            000)
                die "Could not ask GitHub which release to install (no response - network or DNS failure). $(cat "$WORK_DIR/curl.err" 2>/dev/null)"
                ;;
            *)
                die "Could not ask GitHub which release to install (HTTP $rr_status). A 403 usually means GitHub's rate limit for anonymous requests (60/hour per IP); waiting and re-running is normally enough."
                ;;
        esac
    fi

    rr_flat="$WORK_DIR/release.tsv"
    json_flatten <"$rr_json" >"$rr_flat"
    REL_TAG=$(json_get "$rr_flat" .tag_name)
    # The tag is interpolated into URLs and file names; same gate as the updater.
    valid_tag "$REL_TAG" || die "GitHub returned an implausible release tag: '$REL_TAG'"
    if [ -n "$VERSION" ] && [ "$REL_TAG" != "$VERSION" ]; then
        die "Asked for release '$VERSION' but GitHub returned '$REL_TAG'."
    fi

    if select_asset "$rr_flat" "$REL_TAG"; then
        valid_download_url "$ASSET_URL" || die "The release lists an implausible download URL: '$ASSET_URL'"
        REL_URL=$ASSET_URL
        REL_DIGEST=$ASSET_DIGEST
    else
        warn "Release $REL_TAG has no $(expected_asset_name "$REL_TAG"); falling back to the source archive."
        note 'The app will report its version as 0.0.0 until the first in-app update.'
        REL_URL="https://github.com/$REPO/archive/refs/tags/$REL_TAG.tar.gz"
        REL_DIGEST=''
    fi
}

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

install_app() {
    ia_archive="$WORK_DIR/app.tar.gz"
    if [ -n "${CASESORTER_INSTALL_ARCHIVE:-}" ]; then
        cp "$CASESORTER_INSTALL_ARCHIVE" "$ia_archive"
    else
        note "Downloading $REL_TAG..."
        ia_status=$(http_get "$REL_URL" "$ia_archive")
        if [ "$ia_status" != 200 ]; then
            die "Could not download the app (HTTP $ia_status) from $REL_URL $(cat "$WORK_DIR/curl.err" 2>/dev/null)"
        fi
    fi

    # Before tar reads a byte of it.
    ia_rc=0
    verify_download "$ia_archive" "$REL_DIGEST" || ia_rc=$?
    if [ "$ia_rc" -eq 1 ]; then
        die 'The download was discarded and nothing was installed. Re-run the installer; if this keeps happening, report it.'
    fi

    note 'Extracting...'
    check_archive "$ia_archive" || die 'The archive was refused and nothing was installed.'
    ia_unpack="$WORK_DIR/unpacked"
    mkdir "$ia_unpack"
    # -o: owner from the user running this, not the archive (GNU and bsdtar).
    tar -x -o -z -f "$ia_archive" -C "$ia_unpack" || die 'Could not extract the downloaded archive.'

    # Both the sdist and GitHub's source archives nest everything under one
    # top-level directory.
    ia_src=$ia_unpack
    ia_top=$(ls -A "$ia_unpack")
    case $ia_top in
        *'
'*) ;;
        *) [ -n "$ia_top" ] && [ -d "$ia_unpack/$ia_top" ] && ia_src="$ia_unpack/$ia_top" ;;
    esac
    looks_like_the_app "$ia_src" ||
        die 'The downloaded archive does not look like the app (no src/sorter/__init__.py, and no main.py + sorter/__init__.py).'

    for ia_name in $PROTECTED_TOP_LEVEL; do
        rm -rf "${ia_src:?}/$ia_name"
    done

    note "Installing to $PREFIX"
    mkdir -p "$PREFIX" || die "Could not create $PREFIX"
    # The package directories are replaced, not merged, so a module a release
    # dropped can't linger and be imported. Nothing of the user's lives there.
    for ia_root in $PRUNE_ROOTS; do
        rm -rf "${PREFIX:?}/$ia_root"
    done
    cp -R "$ia_src/." "$PREFIX/" || die "Could not copy the app into $PREFIX"
    chmod +x "$PREFIX/start.sh" 2>/dev/null || true

    # An sdist normalises every mtime, so a same-size replacement passes
    # Python's .pyc staleness check; see the same step in install-windows.ps1.
    find "$PREFIX" \( -path "$PREFIX/.venv" -o -path "$PREFIX/.uv" \) -prune -o \
        -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
}

launcher_path() {
    printf '%s/.local/bin/%s\n' "$HOME" "$LAUNCHER_NAME"
}

# Ours when it carries the marker line; anything else is someone else's file.
launcher_is_ours() {
    [ ! -e "$1" ] || grep -qxF "$LAUNCHER_MARKER" "$1" 2>/dev/null
}

write_launcher() {
    wl_path=$(launcher_path)
    wl_dir=$(dirname "$wl_path")
    mkdir -p "$wl_dir" || die "Could not create $wl_dir"
    # Single-quoted, with any ' in the prefix escaped, so no path can break it.
    wl_quoted=$(printf '%s/start.sh' "$PREFIX" | sed "s/'/'\\\\''/g")
    wl_tmp="$wl_path.tmp.$$"
    {
        echo '#!/bin/sh'
        echo "$LAUNCHER_MARKER"
        echo "exec '$wl_quoted' \"\$@\""
    } >"$wl_tmp" || die "Could not write $wl_path"
    chmod +x "$wl_tmp"
    mv -f "$wl_tmp" "$wl_path" || die "Could not write $wl_path"
    note "Launcher: $wl_path"
    case ":${PATH:-}:" in
        *":$wl_dir:"*) ;;
        *) warn "$wl_dir is not on your PATH. Add it, or start the app with: $wl_path" ;;
    esac
}

# The first dependency sync (uv, Python, packages, any sudo prompt for system
# libraries), here in the user's terminal rather than on first launch.
first_run_setup() {
    if ! grep -qF "SETUP_ONLY_FLAG = \"$SETUP_ONLY_FLAG\"" "$PREFIX/bootstrap.py" 2>/dev/null; then
        note "Release $REL_TAG sets up its dependencies on first launch instead."
        return 0
    fi
    if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
        warn 'No python3 found, so the dependencies could not be set up yet.'
        return 0
    fi
    step 'Setting up dependencies (first run; this takes a few minutes)'
    if "$PREFIX/start.sh" "$SETUP_ONLY_FLAG"; then
        note 'Dependencies are ready.'
    else
        die "The app is installed, but setting up its dependencies failed (see the output above). Starting the app retries it; its log is in $(data_root "$PREFIX")/logs/launch.log"
    fi
}

usage() {
    cat <<'EOF'
Install or update the AI Case Sorter (Linux and macOS).

Usage: sh install-unix.sh [options]

  --prefix DIR        Install the app into DIR
                      (default: ~/.local/opt/ai-case-sorter)
  --version TAG       Install a specific release, e.g. 2.3.0 (default: latest)
  --force             Install into a non-empty DIR that is not a previous
                      install, and replace a launcher this script did not write
  --no-bootstrap      Skip the first-run dependency setup; the first launch
                      does it instead
  --repo OWNER/REPO   Install from another repository's releases (testing)
  -h, --help          Show this help

The launcher goes to ~/.local/bin/ai-case-sorter. Re-running upgrades in
place; your data (models, settings, logs) lives outside the install folder
and is never touched.
EOF
}

main() {
    PREFIX="$HOME/.local/opt/ai-case-sorter"
    VERSION=''
    FORCE=0
    BOOTSTRAP=1
    REPO=$DEFAULT_REPO
    while [ $# -gt 0 ]; do
        case $1 in
            --prefix)
                [ $# -ge 2 ] || { usage >&2; exit 2; }
                PREFIX=$2
                shift 2
                ;;
            --prefix=*) PREFIX=${1#*=}; shift ;;
            --version)
                [ $# -ge 2 ] || { usage >&2; exit 2; }
                VERSION=$2
                shift 2
                ;;
            --version=*) VERSION=${1#*=}; shift ;;
            --repo)
                [ $# -ge 2 ] || { usage >&2; exit 2; }
                REPO=$2
                shift 2
                ;;
            --repo=*) REPO=${1#*=}; shift ;;
            --force) FORCE=1; shift ;;
            --no-bootstrap) BOOTSTRAP=0; shift ;;
            -h | --help) usage; exit 0 ;;
            *)
                echo "Unknown option: $1" >&2
                usage >&2
                exit 2
                ;;
        esac
    done

    [ -n "$PREFIX" ] || die '--prefix needs a directory.'
    case $PREFIX in
        /*) ;;
        *) PREFIX="$(pwd)/$PREFIX" ;;
    esac
    PREFIX=${PREFIX%/}
    [ -n "$PREFIX" ] || die 'Refusing to install into /.'

    # Data root, never the install folder, which this script and the updater
    # both overwrite. Best-effort: an unwritable data root costs the log only.
    m_logs="$(data_root "$PREFIX")/logs"
    if mkdir -p "$m_logs" 2>/dev/null; then
        LOG_FILE="$m_logs/install-$(date +%Y%m%d-%H%M%S).log"
        : >>"$LOG_FILE" 2>/dev/null || LOG_FILE=''
    fi

    _emit ''
    _emit '  AI Case Sorter - Linux/macOS installer'
    _emit '  --------------------------------------'
    _emit ''
    note "System       : $(uname -srm 2>/dev/null || echo unknown)"
    note "Repo         : $REPO${VERSION:+ (pinned to $VERSION)}"
    note "Prefix       : $PREFIX"
    [ -z "$LOG_FILE" ] || note "Log          : $LOG_FILE"
    _emit ''

    valid_repo "$REPO" || die "Not an OWNER/REPO name: '$REPO'"
    if [ -n "$VERSION" ] && ! valid_tag "$VERSION"; then
        die "Not a release tag: '$VERSION'. Tags look like 2.3.0 (no 'v' prefix)."
    fi
    for m_tool in curl tar; do
        command -v "$m_tool" >/dev/null 2>&1 || die "This installer needs '$m_tool', which was not found."
    done

    if [ -e "$PREFIX" ] && [ ! -d "$PREFIX" ]; then
        die "$PREFIX exists and is not a directory."
    fi
    if [ -d "$PREFIX" ] && is_previous_install "$PREFIX"; then
        step "Updating the existing install at $PREFIX"
    elif [ -d "$PREFIX" ] && ! dir_is_empty "$PREFIX" && [ "$FORCE" -ne 1 ]; then
        die "$PREFIX is not empty and does not hold a previous install. Choose another --prefix, or pass --force to install into it anyway."
    else
        step "Installing to $PREFIX"
    fi
    if [ "$FORCE" -ne 1 ] && ! launcher_is_ours "$(launcher_path)"; then
        die "$(launcher_path) exists and was not written by this installer. Move it aside, or pass --force to replace it."
    fi

    WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/casesorter-install.XXXXXX") || die 'Could not create a temporary directory.'
    trap 'rm -rf "$WORK_DIR"' EXIT
    trap 'exit 130' INT TERM

    step 'Fetching the app'
    resolve_release
    note "Source: $REL_URL"
    install_app
    note "$REL_TAG installed."

    step 'Creating the launcher'
    write_launcher

    [ "$BOOTSTRAP" -eq 0 ] || first_run_setup

    _emit ''
    note 'Done. The app is installed at:'
    note "  $PREFIX"
    note 'Your models and settings are kept separately at:'
    note "  $(data_root "$PREFIX")"
    _emit ''
    note "Start it with: $LAUNCHER_NAME"
    note 'Updates are offered inside the app - no need to re-run this.'
    if ! command -v python3 >/dev/null 2>&1; then
        warn 'No python3 found. The app needs Python 3 to start; install it first.'
    fi
}

if [ "${CASESORTER_INSTALL_LIB:-}" != 1 ]; then
    main "$@"
fi
