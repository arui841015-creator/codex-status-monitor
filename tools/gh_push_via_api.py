"""Push one local commit to GitHub through the REST Git Data API.

The git protocol needs https://github.com, which some corporate egress
policies refuse (CONNECT tunnel 502) even though api.github.com stays open.
This script replays the local commit object-for-object so the remote commit
keeps the same SHA and the local branch does not diverge.

Usage: python tools/gh_push_via_api.py [--dry-run]
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.request

API = "https://api.github.com"
OWNER = "arui841015-creator"
REPO = "codex-status-monitor"
BRANCH = "main"


def git(*args: str, cwd: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True
    ).stdout


def token() -> str:
    payload = "protocol=https\nhost=github.com\n\n".encode()
    out = subprocess.run(
        ["git", "credential", "fill"], input=payload, capture_output=True, check=True
    ).stdout.decode()
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise SystemExit("no stored GitHub credential")


def call(method: str, path: str, body: dict | None, tok: str) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Accept", "application/vnd.github+json")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dry = "--dry-run" in sys.argv
    tok = token()

    raw = git("cat-file", "commit", "HEAD", cwd=cwd).decode("utf-8", "replace")
    header, _, message = raw.partition("\n\n")
    fields: dict[str, str] = {}
    for line in header.splitlines():
        if line.startswith(" "):
            continue
        key, _, value = line.partition(" ")
        fields.setdefault(key, value)
    parent = fields["parent"]
    author_name, author_email, author_ts, author_tz = parse_person(fields["author"])
    committer_name, committer_email, committer_ts, committer_tz = parse_person(fields["committer"])

    changed = git("diff", "--name-only", f"{parent}", "HEAD", cwd=cwd).decode().split()
    listing = {}
    for line in git("ls-files", "-s", "--", *changed, cwd=cwd).decode().splitlines():
        meta, path = line.split("\t", 1)
        mode, sha, _stage = meta.split()
        listing[path] = (mode, sha)

    print(f"commit={git('rev-parse', 'HEAD', cwd=cwd).decode().strip()}")
    print(f"parent={parent} files={len(changed)}")
    if dry:
        for path in changed:
            print(f"  {listing.get(path, ('?', '?'))[0]} {path}")
        return 0

    parent_commit = call("GET", f"/repos/{OWNER}/{REPO}/git/commits/{parent}", None, tok)
    base_tree = parent_commit["tree"]["sha"]

    entries = []
    for path in changed:
        mode, local_blob = listing[path]
        content = (git("show", f"HEAD:{path}", cwd=cwd))
        blob = call(
            "POST",
            f"/repos/{OWNER}/{REPO}/git/blobs",
            {"content": base64.b64encode(content).decode(), "encoding": "base64"},
            tok,
        )
        if blob["sha"] != local_blob:
            raise SystemExit(f"blob sha mismatch for {path}: {blob['sha']} != {local_blob}")
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]})
    print(f"blobs recreated: {len(entries)} (all sha match)")

    tree = call(
        "POST",
        f"/repos/{OWNER}/{REPO}/git/trees",
        {"base_tree": base_tree, "tree": entries},
        tok,
    )
    local_tree = fields["tree"]
    print(f"tree={tree['sha']} local={local_tree} match={tree['sha'] == local_tree}")
    if tree["sha"] != local_tree:
        raise SystemExit("tree sha mismatch: local commit cannot be replayed byte-identically")

    commit = call(
        "POST",
        f"/repos/{OWNER}/{REPO}/git/commits",
        {
            "message": message,
            "tree": tree["sha"],
            "parents": [parent],
            "author": {
                "name": author_name,
                "email": author_email,
                "date": iso(author_ts, author_tz),
            },
            "committer": {
                "name": committer_name,
                "email": committer_email,
                "date": iso(committer_ts, committer_tz),
            },
        },
        tok,
    )
    local_head = git("rev-parse", "HEAD", cwd=cwd).decode().strip()
    print(f"remote commit={commit['sha']} local={local_head} match={commit['sha'] == local_head}")

    call(
        "PATCH",
        f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}",
        {"sha": commit["sha"], "force": False},
        tok,
    )
    print(f"ref {BRANCH} -> {commit['sha']}")
    return 0


def parse_person(value: str) -> tuple[str, str, int, str]:
    """Split 'Name <email> 1795789018 +0800' into its four parts."""
    head, _, tail = value.rpartition(">")
    name, _, email = head.partition(" <")
    ts, tz = tail.split()
    return name, email, int(ts), tz


def iso(ts: int, tz: str) -> str:
    """RFC 3339 timestamp GitHub accepts (it keeps the original offset)."""
    import datetime as dt

    sign = 1 if tz.startswith("+") else -1
    offset = dt.timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])) * sign
    zone = dt.timezone(offset)
    return dt.datetime.fromtimestamp(ts, zone).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
