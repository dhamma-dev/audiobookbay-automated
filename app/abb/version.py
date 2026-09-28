"""Build identity: which commit the running app was built from, so the footer,
/healthz and the boot log say exactly what's deployed — no guessing whether
Portainer pulled the new image yet.

Docker images carry it as APP_* env vars, set from CI build args (see the
Dockerfile and the workflow). A local run from a git checkout asks git
instead. Nothing here runs at import; Services reads the env in __init__ and
only falls back to git in start(), so tests never spawn a process.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime

log = logging.getLogger("abb.version")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _short_date(iso):
    """'2026-09-28T15:05:12Z' -> '2026-09-28 15:05 UTC' (raw text if unparseable)."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso


def _info(full_sha, ref="", built="", repo_url="", source="image", dirty=False):
    full_sha = (full_sha or "").strip()
    if not full_sha:
        return None
    return {
        "sha": full_sha[:7] + ("-dirty" if dirty else ""),
        "full_sha": full_sha,
        "ref": (ref or "").strip(),
        "built": _short_date(built.strip()) if built and built.strip() else "",
        # Commit link for the footer; only for real builds (a dirty local tree
        # isn't that commit).
        "url": f"{repo_url.strip().rstrip('/')}/commit/{full_sha}"
               if repo_url and repo_url.strip() and not dirty else "",
        "source": source,
    }


def from_env(env=None):
    """Build info baked into the image, or None (e.g. a local run)."""
    env = os.environ if env is None else env
    return _info(env.get("APP_GIT_SHA"), env.get("APP_GIT_REF"),
                 env.get("APP_BUILD_DATE"), env.get("APP_GIT_REPO_URL"))


def from_git_checkout(root=_REPO_ROOT):
    """Best effort for local runs: HEAD of the checkout this code sits in,
    marked dirty when tracked files have uncommitted changes. None when
    there's no git or no checkout (e.g. inside the image)."""
    def git(*args):
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                              timeout=5, check=True).stdout.strip()
    try:
        sha = git("rev-parse", "HEAD")
        ref = git("rev-parse", "--abbrev-ref", "HEAD")
        dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    except (OSError, subprocess.SubprocessError):
        return None
    return _info(sha, "" if ref == "HEAD" else ref, source="checkout", dirty=dirty)


def describe(info):
    """One line for the boot log."""
    if not info:
        return "unknown (no APP_GIT_SHA, and not a git checkout)"
    where = f"{info['ref']}@{info['sha']}" if info["ref"] else info["sha"]
    built = f", built {info['built']}" if info["built"] else ""
    return f"{where}{built} ({'local checkout' if info['source'] == 'checkout' else 'image'})"
