#!/bin/sh
# Tests install-unix.sh without the network.
#
# The digest, entry-name and layout cases mirror Test-DigestVerification.ps1
# and Test-ArchiveEntryValidation.ps1, which in turn mirror updater.py's own
# tests: all three consume the same archives, so a shape one accepts and
# another refuses is a bug in whichever accepts it. The install cases feed a
# synthetic sdist and release JSON through the script's testing hooks.
#
# Run from anywhere:  sh installer/tests/test-install-unix.sh
# (also under dash and bash; tests/unit/test_installer_scripts.py runs each.)
#
# pass/fail always succeed, so `test && pass || fail` is a safe if/else here.
# shellcheck disable=SC2015
# shellcheck source-path=SCRIPTDIR

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT="$HERE/../install-unix.sh"
# shellcheck disable=SC2034  # read by the sourced script
CASESORTER_INSTALL_LIB=1
# shellcheck source=../install-unix.sh
. "$SCRIPT"
set +e

PASSES=0
FAILURES=0
pass() { PASSES=$((PASSES + 1)); }
fail() {
    FAILURES=$((FAILURES + 1))
    printf 'FAIL: %s\n' "$1"
}

TMP=$(mktemp -d "${TMPDIR:-/tmp}/casesorter-installer-test.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
# check_archive and friends use the installer's own scratch directory.
WORK_DIR="$TMP/work"
mkdir "$WORK_DIR"

HEX=abababababababababababababababababababababababababababababababab
HEX_UPPER=ABABABABABABABABABABABABABABABABABABABABABABABABABABABABABABABAB
SHA512=cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd

echo '== digest parsing =='
assert_kind() {
    ak_got=$(classify_digest "$1")
    if [ "$ak_got" = "$2" ]; then pass; else fail "digest '$1' -> '$ak_got', expected '$2'"; fi
}
assert_kind "sha256:$HEX" "sha256 $HEX"
assert_kind "sha256:$HEX_UPPER" "sha256 $HEX"
assert_kind "SHA256:$HEX" "sha256 $HEX"
assert_kind '' 'absent '
assert_kind '   ' 'absent '
assert_kind "sha512:$SHA512" 'unsupported sha512'
assert_kind 'sha256:abc' 'malformed '
assert_kind "sha256:gggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggg" 'malformed '
assert_kind "sha256:${HEX}00" 'malformed '
assert_kind "$HEX" 'malformed '
assert_kind 'sha256' 'malformed '
assert_kind 'sha256:' 'malformed '
assert_kind ":$HEX" 'malformed '
assert_kind "sha 256:$HEX" 'malformed '

echo '== verifying a file =='
# SHA-256 of the three bytes "abc" (FIPS 180-2 test vector).
ABC=ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad
ABC_UPPER=$(printf '%s' "$ABC" | tr '[:lower:]' '[:upper:]')
printf 'abc' >"$TMP/abc"
assert_verify() {
    verify_download "$TMP/abc" "$1" >/dev/null
    case $? in
        0) av_got=verified ;;
        2) av_got=unverified ;;
        *) av_got=refused ;;
    esac
    if [ "$av_got" = "$2" ]; then pass; else fail "digest '$1' was $av_got, expected $2 ($3)"; fi
}
assert_verify "sha256:$ABC" verified 'matching digest'
assert_verify "sha256:$ABC_UPPER" verified 'hex case does not matter'
assert_verify "sha256:$HEX" refused 'mismatch'
assert_verify '' unverified 'no digest published: TLS alone'
assert_verify "sha512:$SHA512" unverified 'algorithm this installer cannot check'
assert_verify 'sha256:abc' refused 'present but unreadable'

echo '== tag validation (updater._TAG_RE) =='
assert_tag() {
    if valid_tag "$1"; then at_got=valid; else at_got=invalid; fi
    if [ "$at_got" = "$2" ]; then pass; else fail "tag '$1' was $at_got, expected $2"; fi
}
T64=1234567890123456789012345678901234567890123456789012345678901234
assert_tag 2.3.0 valid
assert_tag 0.5.0rc1 valid
assert_tag v1.0.0 valid
assert_tag v valid
assert_tag vv1 valid
assert_tag main valid
assert_tag "$T64" valid
assert_tag "v$T64" valid
assert_tag "1$T64" invalid
assert_tag "vv$T64" invalid
assert_tag '' invalid
assert_tag -1.0 invalid
assert_tag .1 invalid
assert_tag '1.0.0/../../evil/repo' invalid
assert_tag '1.0 0' invalid
assert_tag '1.0;rm' invalid
assert_tag '1.0
x' invalid

echo '== asset name =='
[ "$(expected_asset_name 2.3.0)" = ai_case_sorter-2.3.0.tar.gz ] && pass || fail 'asset name for 2.3.0'
[ "$(expected_asset_name v1.0)" = ai_case_sorter-1.0.tar.gz ] && pass || fail 'one lowercase v is stripped'
[ "$(expected_asset_name vv1)" = ai_case_sorter-v1.tar.gz ] && pass || fail 'only one v is stripped'
[ "$(expected_asset_name V1)" = ai_case_sorter-V1.tar.gz ] && pass || fail 'a capital V is kept'

echo '== entry names =='
assert_entry() {
    if check_entry_name "$1" >/dev/null; then ae_got=accepted; else ae_got=rejected; fi
    if [ "$ae_got" = "$2" ]; then pass; else fail "entry '$1' was $ae_got, expected $2"; fi
}
assert_entry 'C:/evil.py' rejected
assert_entry 'C:evil.py' rejected
assert_entry 'pkg/D:/evil.py' rejected
assert_entry 'pkg/D:evil.py' rejected
assert_entry 'pkg/sub/E:/x.dll' rejected
assert_entry 'pkg\F:\x.dll' rejected
assert_entry 'pkg/main.py:stream' rejected
assert_entry '/etc/passwd' rejected
assert_entry '\windows\system32\x' rejected
assert_entry '\\server\share\x' rejected
assert_entry 'pkg/../../evil.py' rejected
assert_entry '..' rejected
assert_entry '../' rejected
assert_entry 'pkg/..' rejected
assert_entry 'pkg/../' rejected
assert_entry 'pkg\..\..\evil.py' rejected
assert_entry 'ai_case_sorter-1.2.3/main.py' accepted
assert_entry 'ai_case_sorter-1.2.3/' accepted
assert_entry 'ai_case_sorter-1.2.3/src/sorter/__init__.py' accepted
assert_entry 'ai_case_sorter-1.2.3/installer/install-unix.sh' accepted
assert_entry 'ai_case_sorter-1.2.3/.gitignore' accepted
assert_entry 'pkg/a..b.py' accepted
assert_entry 'pkg/..hidden' accepted
assert_entry 'pkg/file with spaces.py' accepted

echo '== archive entry types =='
assert_archive() {
    if check_archive "$1" >/dev/null; then aa_got=accepted; else aa_got=rejected; fi
    if [ "$aa_got" = "$2" ]; then pass; else fail "archive $3 was $aa_got, expected $2"; fi
}
mkdir -p "$TMP/arc/pkg"
echo x >"$TMP/arc/pkg/a.py"
tar -czf "$TMP/clean.tar.gz" -C "$TMP/arc" pkg
assert_archive "$TMP/clean.tar.gz" accepted 'with files and directories'
ln -s /etc/passwd "$TMP/arc/pkg/link"
tar -czf "$TMP/symlink.tar.gz" -C "$TMP/arc" pkg
assert_archive "$TMP/symlink.tar.gz" rejected 'with a symlink'
rm "$TMP/arc/pkg/link"
ln "$TMP/arc/pkg/a.py" "$TMP/arc/pkg/b.py"
tar -czf "$TMP/hardlink.tar.gz" -C "$TMP/arc" pkg
assert_archive "$TMP/hardlink.tar.gz" rejected 'with a hardlink'
rm "$TMP/arc/pkg/b.py"
printf '' | gzip >"$TMP/empty.tar.gz"
assert_archive "$TMP/empty.tar.gz" rejected 'that is empty'
echo 'not a tarball' >"$TMP/junk.tar.gz"
assert_archive "$TMP/junk.tar.gz" rejected 'that is not a tarball'

echo '== layout gate =='
assert_layout() {
    al_root="$TMP/layout.$$"
    rm -rf "$al_root"
    for al_f in $1; do
        mkdir -p "$(dirname "$al_root/$al_f")"
        : >"$al_root/$al_f"
    done
    if looks_like_the_app "$al_root"; then al_got=app; else al_got=not-app; fi
    if [ "$al_got" = "$2" ]; then pass; else fail "layout '$1' was $al_got, expected $2"; fi
    rm -rf "$al_root"
}
assert_layout 'main.py sorter/__init__.py' app
assert_layout 'main.py src/sorter/__init__.py' app
assert_layout 'main.py' not-app
assert_layout 'README.md setup.py' not-app

echo '== release JSON =='
cat >"$TMP/release.json" <<'EOF'
{
  "tag_name": "9.9.9",
  "name": "9.9.9",
  "body": "notes with \"name\": \"ai_case_sorter-9.9.9.tar.gz\", and [brackets] {braces}",
  "draft": false,
  "assets": [
    {"name": "ai-case-sorter-docs-9.9.9.pdf", "uploader": {"login": "bot", "id": 1},
     "digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
     "browser_download_url": "https://example.invalid/docs.pdf"},
    {"name": "decoy.tar.gz", "digest": null, "browser_download_url": "https://example.invalid/decoy.tar.gz"},
    {"name": "ai_case_sorter-9.9.9.tar.gz", "size": 123, "uploader": {"name": "x"},
     "digest": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
     "browser_download_url": "https://example.invalid/ai_case_sorter-9.9.9.tar.gz"},
    {"name": "ai_case_sorter-9.9.9-py3-none-any.whl", "digest": null, "browser_download_url": "https://example.invalid/w.whl"}
  ]
}
EOF
json_flatten <"$TMP/release.json" >"$TMP/release.tsv"
[ "$(json_get "$TMP/release.tsv" .tag_name)" = 9.9.9 ] && pass || fail 'tag_name'
if select_asset "$TMP/release.tsv" 9.9.9 &&
    [ "$ASSET_URL" = https://example.invalid/ai_case_sorter-9.9.9.tar.gz ] &&
    [ "$ASSET_DIGEST" = sha256:2222222222222222222222222222222222222222222222222222222222222222 ]; then
    pass
else
    fail "sdist selected by exact name (got '${ASSET_URL-}' '${ASSET_DIGEST-}')"
fi
tr -d '\n' <"$TMP/release.json" | json_flatten >"$TMP/minified.tsv"
cmp -s "$TMP/release.tsv" "$TMP/minified.tsv" && pass || fail 'minified JSON flattens the same'
if select_asset "$TMP/release.tsv" 9.9.8; then fail 'a release without the sdist selects nothing'; else pass; fi
printf '{"assets": [{"name": "ai_case_sorter-1.tar.gz", "digest": null, "browser_download_url": "https://e.invalid/x"}]}' |
    json_flatten >"$TMP/null.tsv"
select_asset "$TMP/null.tsv" 1 && [ -z "$ASSET_DIGEST" ] && pass || fail 'null digest reads as absent'

echo '== download URLs =='
valid_download_url 'https://github.com/o/r/releases/download/1.0/a.tar.gz' && pass || fail 'release URL'
valid_download_url 'http://github.com/a.tar.gz' && fail 'plain http accepted' || pass
valid_download_url "https://e.invalid/a'b" && fail 'quote accepted' || pass
valid_download_url 'https://e.invalid/a b' && fail 'space accepted' || pass
valid_download_url 'https://e.invalid/a\u0022' && fail 'JSON escape accepted' || pass

echo '== installing =='
# A minimal sdist: the app gate's files plus a start.sh that records its
# arguments. "hostile" plants protected entries; "setup" gives bootstrap.py
# the --setup-only flag a current release carries.
make_sdist() {
    ms_dir="$TMP/sdist-src/ai_case_sorter-9.9.9"
    rm -rf "$TMP/sdist-src"
    mkdir -p "$ms_dir/src/sorter"
    : >"$ms_dir/src/sorter/__init__.py"
    echo "__version__ = version = '9.9.9'" >"$ms_dir/src/sorter/_version.py"
    : >"$ms_dir/bootstrap.py"
    case " $* " in
        *' setup '*) echo 'SETUP_ONLY_FLAG = "--setup-only"' >"$ms_dir/bootstrap.py" ;;
    esac
    # shellcheck disable=SC2016  # expanded by the fake start.sh, not here
    printf '#!/bin/sh\necho "$*" >>"$(dirname "$0")/start-calls"\necho started\n' >"$ms_dir/start.sh"
    case " $* " in *' hostile '*)
        mkdir -p "$ms_dir/.venv"
        echo hostile >"$ms_dir/.venv/marker"
        echo hostile >"$ms_dir/portable.txt"
        ;;
    esac
    tar -czf "$TMP/sdist.tar.gz" -C "$TMP/sdist-src" ai_case_sorter-9.9.9
}
release_with_digest() {
    printf '{"tag_name": "9.9.9", "assets": [{"name": "ai_case_sorter-9.9.9.tar.gz", "digest": %s, "browser_download_url": "https://example.invalid/s.tar.gz"}]}\n' \
        "$1" >"$TMP/rel.json"
}
# Runs the installer in a child shell with a throwaway HOME and data root.
run_install() {
    env HOME="$TMP/home" CASESORTER_DATA_DIR="$TMP/data" CASESORTER_INSTALL_LIB= \
        CASESORTER_INSTALL_RELEASE_JSON="$TMP/rel.json" CASESORTER_INSTALL_ARCHIVE="$TMP/sdist.tar.gz" \
        TMPDIR="$TMP" "${TEST_SHELL:-sh}" "$SCRIPT" "$@" >"$TMP/out.txt" 2>&1
}
dump() { sed 's/^/    | /' "$TMP/out.txt"; }
# installs DESCRIPTION [ARGS...] / refuses DESCRIPTION [ARGS...]
installs() {
    in_desc=$1
    shift
    if run_install "$@"; then pass; else
        fail "$in_desc: exited non-zero"
        dump
    fi
}
refuses() {
    re_desc=$1
    shift
    if run_install "$@"; then
        fail "$re_desc: exited zero"
        dump
    else pass; fi
}
expect() {
    if grep -qF "$1" "$TMP/out.txt"; then pass; else
        fail "$2: output lacks '$1'"
        dump
    fi
}
PREFIX_DIR="$TMP/home/.local/opt/ai-case-sorter"
LAUNCHER="$TMP/home/.local/bin/ai-case-sorter"
mkdir -p "$TMP/home"

make_sdist
SDIST_SHA=$(sha256_of "$TMP/sdist.tar.gz")

release_with_digest "\"sha256:$HEX\""
refuses 'digest mismatch'
expect 'does not match the SHA-256' 'mismatch'
[ ! -e "$PREFIX_DIR/src" ] && pass || fail 'a refused download left files behind'

release_with_digest '"sha256:abc"'
refuses 'malformed digest'
expect "can't be read" 'malformed'

release_with_digest "\"sha256:$SDIST_SHA\""
installs 'verified install'
expect 'SHA-256 verified against the published checksum.' 'verified'
[ -f "$PREFIX_DIR/src/sorter/_version.py" ] && pass || fail 'the tree was not installed'
[ -x "$PREFIX_DIR/start.sh" ] && pass || fail 'start.sh is not executable'
[ -x "$LAUNCHER" ] && pass || fail 'the launcher is missing or not executable'
[ ! -e "$PREFIX_DIR/start-calls" ] && pass || fail 'ran start.sh for a release without --setup-only'
expect 'sets up its dependencies on first launch' 'older release'
[ "$(sh "$LAUNCHER")" = started ] && pass || fail 'the launcher does not run start.sh'
ls "$TMP/data/logs"/install-*.log >/dev/null 2>&1 && pass || fail 'no install log under the data root'
ls -d "$TMP"/casesorter-install.* >/dev/null 2>&1 && fail 'the temporary directory was left behind' || pass

# Upgrade in place: what the user and bootstrap.py own survives, stale
# package files and bytecode do not, and an archive cannot plant over them.
mkdir -p "$PREFIX_DIR/.venv" "$PREFIX_DIR/.uv" "$PREFIX_DIR/src/sorter/__pycache__" "$PREFIX_DIR/__pycache__"
echo mine >"$PREFIX_DIR/.venv/marker"
echo mine >"$PREFIX_DIR/.env"
: >"$PREFIX_DIR/src/sorter/dropped_module.py"
: >"$PREFIX_DIR/__pycache__/bootstrap.cpython-312.pyc"
make_sdist hostile
release_with_digest null
installs 'upgrade with no digest'
expect 'Updating the existing install' 'upgrade'
expect 'Not verified: no checksum is published' 'absent digest'
[ "$(cat "$PREFIX_DIR/.venv/marker")" = mine ] && pass || fail '.venv was overwritten'
[ "$(cat "$PREFIX_DIR/.env")" = mine ] && pass || fail '.env was overwritten'
[ -d "$PREFIX_DIR/.uv" ] && pass || fail '.uv was removed'
[ ! -e "$PREFIX_DIR/portable.txt" ] && pass || fail 'the archive planted portable.txt'
[ ! -e "$PREFIX_DIR/src/sorter/dropped_module.py" ] && pass || fail 'a stale module survived'
[ ! -e "$PREFIX_DIR/__pycache__" ] && pass || fail 'stale bytecode survived'

make_sdist
SDIST_SHA=$(sha256_of "$TMP/sdist.tar.gz")
release_with_digest "\"sha256:$SDIST_SHA\""

# First-run setup: start.sh --setup-only, never a plain launch; skippable.
make_sdist setup
SDIST_SHA=$(sha256_of "$TMP/sdist.tar.gz")
release_with_digest "\"sha256:$SDIST_SHA\""
rm -f "$PREFIX_DIR/start-calls"
installs 'no-bootstrap' --no-bootstrap
[ ! -e "$PREFIX_DIR/start-calls" ] && pass || fail '--no-bootstrap still ran start.sh'
installs 'first-run setup'
[ "$(cat "$PREFIX_DIR/start-calls" 2>/dev/null)" = --setup-only ] && pass || fail 'start.sh was not run with --setup-only alone'
expect 'Dependencies are ready.' 'first-run setup'
rm -f "$PREFIX_DIR/start-calls"
make_sdist
SDIST_SHA=$(sha256_of "$TMP/sdist.tar.gz")
release_with_digest "\"sha256:$SDIST_SHA\""

# Someone else's directory and someone else's launcher are refused.
mkdir -p "$TMP/other"
echo theirs >"$TMP/other/notes.txt"
refuses 'foreign directory' --prefix "$TMP/other"
expect 'pass --force' 'foreign prefix'
[ ! -e "$TMP/other/src" ] && pass || fail 'a refused prefix was written to'
installs '--force into a foreign directory' --prefix "$TMP/other" --force
[ -f "$TMP/other/notes.txt" ] && [ -f "$TMP/other/src/sorter/__init__.py" ] && pass || fail '--force install'
grep -qF "$TMP/other/start.sh" "$LAUNCHER" && pass || fail 'the launcher does not follow --prefix'

printf '#!/bin/sh\necho mine\n' >"$LAUNCHER"
refuses 'foreign launcher'
expect 'was not written by this installer' 'foreign launcher'
[ "$(sh "$LAUNCHER")" = mine ] && pass || fail 'the foreign launcher was modified'

# A prefix with a quote in it still produces a working launcher.
rm -f "$LAUNCHER"
QUOTED="$TMP/it's here"
installs 'path with a quote' --prefix "$QUOTED"
[ "$(sh "$LAUNCHER")" = started ] && pass || fail 'the launcher breaks on a quoted path'

refuses 'invalid --version' --version '1.0/../../x'
expect 'Not a release tag' 'invalid --version'
refuses 'tag other than requested' --version 9.9.8
expect "Asked for release '9.9.8'" 'tag mismatch'
refuses 'invalid --repo' --repo 'a/b/../c'
refuses 'unknown option' --bogus
installs '--help' --help
expect 'Usage: sh install-unix.sh' 'help'

echo ''
if [ "$FAILURES" -gt 0 ]; then
    echo "$FAILURES failed, $PASSES passed"
    exit 1
fi
echo "all $PASSES passed"
