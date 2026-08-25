#!/usr/bin/env python3
"""Push the local history to GitHub through the Data API.

The sandbox git credential has no push grant on a freshly created repo, so the
commits are replayed as blobs -> tree -> commit -> ref. The Data API cannot
create a repository's FIRST object ("Git Repository is empty"), so the first
object is bootstrapped through the Contents API and everything else hangs off
it. Nothing is ever force-updated.

    GH_TOKEN=... python3 tools/push_via_api.py Metta-AI/cogame-collab-cooking main
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
# local commit sha -> the sha the API minted for it. Without this the whole
# history is replayed on every run and the branch grows a duplicate of itself.
STATE = pathlib.Path(".git/api_push_state.json")


def run(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def call(method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "accept": "application/vnd.github+json",
            "content-type": "application/json",
            "user-agent": "coworld-builder",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        raise SystemExit(f"{method} {path} -> {error.code}: {detail}") from error


def main() -> None:
    repo, branch = sys.argv[1], sys.argv[2]
    pushed: dict[str, str] = json.loads(STATE.read_text()) if STATE.exists() else {}
    every = run("git", "rev-list", "--reverse", "HEAD").split()
    commits = [sha for sha in every if sha not in pushed]
    print(f"{len(commits)} of {len(every)} commits to replay")
    if not commits:
        print("nothing to push")
        return

    try:
        head = call("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        parent = head["object"]["sha"]
        print(f"branch exists at {parent[:8]}")
    except SystemExit:
        seed = base64.b64encode(
            b"# cogame-collab-cooking\n\nBootstrapping the first git object.\n"
        ).decode()
        created = call(
            "PUT",
            f"/repos/{repo}/contents/README.md",
            {
                "message": "Bootstrap the first git object (the Data API cannot)",
                "content": seed,
                "branch": branch,
            },
        )
        parent = created["commit"]["sha"]
        print(f"bootstrapped at {parent[:8]}")

    uploaded: dict[str, str] = {}
    for index, commit in enumerate(commits, start=1):
        entries = []
        for line in run("git", "ls-tree", "-r", commit).splitlines():
            meta, path = line.split("\t", 1)
            mode, _kind, sha = meta.split()
            if sha not in uploaded:
                content = subprocess.run(
                    ["git", "cat-file", "blob", sha], capture_output=True, check=True
                ).stdout
                blob = call(
                    "POST",
                    f"/repos/{repo}/git/blobs",
                    {"content": base64.b64encode(content).decode(), "encoding": "base64"},
                )
                uploaded[sha] = blob["sha"]
            entries.append(
                {"path": path, "mode": mode, "type": "blob", "sha": uploaded[sha]}
            )
        tree = call("POST", f"/repos/{repo}/git/trees", {"tree": entries})
        message = run("git", "log", "-1", "--format=%B", commit).rstrip("\n")
        made = call(
            "POST",
            f"/repos/{repo}/git/commits",
            {"message": message, "tree": tree["sha"], "parents": [parent]},
        )
        parent = made["sha"]
        pushed[commit] = parent
        STATE.write_text(json.dumps(pushed, indent=1))
        headline = message.splitlines()[0]
        print(f"  [{index}/{len(commits)}] {parent[:8]}  {headline}")

    call("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": parent, "force": False})
    print(f"{branch} -> {parent}")


if __name__ == "__main__":
    main()
