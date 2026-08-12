#!/usr/bin/env python3
"""Attach images to a GitHub issue or PR from the command line.

Produces a real GitHub attachment — the same `github.com/user-attachments/assets/<uuid>`
you get by dragging a file into the comment box — so nothing is committed to the
repository and nothing renders differently from a hand-attached image.

    tools/gh_attach_images.py shots/before.png shots/after.png

Prints one markdown image line per file, ready to paste into a body written with
`gh pr edit --body-file` / `gh issue comment --body-file`.

## The endpoint

`POST https://uploads.github.com/user-attachments/assets`
    ?name=<file>&content_type=<mime>&repository_id=<id>

with `Authorization: Bearer <gh oauth token>` and the raw bytes as the body. It
is undocumented but takes an ordinary `gh` token — no browser session, no
cookie — for **images and video only**, on repositories the token can push to.
It answers `201 {"url": "https://github.com/user-attachments/assets/<uuid>"}`.

Two things about that URL that look like bugs and are not:

- Fetching it directly returns 404 to an anonymous client. GitHub rewrites it at
  render time into a short-lived signed `private-user-images.githubusercontent.com`
  URL, and it mints one for logged-out visitors too — verified against a
  logged-out fetch of a public PR. Attachments made through the web UI behave
  identically; the bare URL is a handle, not a CDN path.
- The asset is orphaned until some comment or body references it.

Credit: the endpoint is the one used by
https://github.com/drogers0/gh-image (MIT), whose `internal/upload/bearer.go`
documents its constraints. This is a stdlib reimplementation of just the token
path, so there is nothing to install and no fallback that reads browser
cookies — that project also has a `user_session` cookie path for the cases the
token cannot cover (non-image files, repos you cannot push to), and that cookie
is equivalent to your account password. We do not want that for screenshots.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UPLOAD_URL = "https://uploads.github.com/user-attachments/assets"

# The endpoint accepts images and video only; anything else 422s.
CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}


def gh(*args: str) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"gh {' '.join(args)} failed:\n{proc.stderr.strip()}")
    return proc.stdout.strip()


def upload(path: Path, repo_id: int, token: str) -> str:
    """POST one file and return its attachment URL."""
    query = urllib.parse.urlencode(
        {
            "name": path.name,
            "content_type": CONTENT_TYPES[path.suffix.lower()],
            "repository_id": repo_id,
        }
    )
    request = urllib.request.Request(
        f"{UPLOAD_URL}?{query}",
        data=path.read_bytes(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            # Omitting this makes the endpoint 400 before it looks at anything else.
            "Content-Type": CONTENT_TYPES[path.suffix.lower()],
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)["url"]
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        sys.exit(f"upload of {path.name} failed: HTTP {exc.code} {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Attach images to a GitHub issue or PR.")
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--repo", default=None, help="defaults to the current repo")
    args = parser.parse_args()

    for path in args.files:
        if not path.is_file():
            sys.exit(f"no such file: {path}")
        if path.suffix.lower() not in CONTENT_TYPES:
            sys.exit(f"the attachment endpoint takes images and video only: {path}")

    repo_args = ["repo", "view", "--json", "id,nameWithOwner"]
    if args.repo:
        repo_args.insert(2, args.repo)
    # `gh repo view --json id` is the GraphQL node id; the upload endpoint wants
    # the numeric REST one.
    repo = json.loads(gh(*repo_args))["nameWithOwner"]
    repo_id = int(gh("api", f"repos/{repo}", "-q", ".id"))
    token = gh("auth", "token")

    for path in args.files:
        url = upload(path, repo_id, token)
        print(f"![{path.stem.replace('-', ' ')}]({url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
