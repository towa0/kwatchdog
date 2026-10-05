"""Git repo watcher: dirty tree, unpushed, behind remote, failing CI (GitHub API)."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Literal

from ..core.models import Result, Status
from ..core.plugin import Watcher, WatcherConfig
from ._common import NO_WINDOW

_GH_RE = re.compile(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?/?$")


class GitConfig(WatcherConfig):
    path: str
    fetch: bool = True  # git fetch before comparing with upstream
    uncommitted: Literal["OK", "WARN", "ALERT"] = "WARN"  # status for a dirty tree
    unpushed: Literal["OK", "WARN", "ALERT"] = "WARN"
    behind: Literal["OK", "WARN", "ALERT"] = "WARN"
    check_ci: bool = False
    github_repo: str | None = None  # "owner/name"; derived from origin if omitted
    github_token: str | None = None  # ${GITHUB_TOKEN}; falls back to the env var
    github_api: str = "https://api.github.com"


class GitWatcher(Watcher):
    type = "git"
    description = "git: uncommitted, unpushed, behind remote, failing GitHub CI"
    Config = GitConfig
    default_interval = 300

    @classmethod
    def unavailable_reason(cls):
        return super().unavailable_reason() or (None if shutil.which("git") else "git executable not found")

    async def git(self, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(Path(self.config.path).expanduser()), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, **NO_WINDOW)
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode(errors="replace").strip()

    async def check(self) -> Result:
        c: GitConfig = self.config
        rc, out = await self.git("rev-parse", "--abbrev-ref", "HEAD")
        if rc != 0:
            return Result.alert(f"not a git repo: {out[:120]}")
        branch = out
        problems: list[tuple[Status, str]] = []
        raw = [f"branch: {branch}"]
        metrics: dict[str, float] = {}

        _, status_out = await self.git("status", "--porcelain")
        dirty = [line for line in status_out.splitlines() if line.strip()]
        metrics["uncommitted"] = float(len(dirty))
        if dirty:
            problems.append((Status(c.uncommitted), f"{len(dirty)} uncommitted"))
            raw.append("uncommitted:\n" + "\n".join(dirty[:30]))

        if c.fetch:
            frc, fout = await self.git("fetch", "--quiet")
            if frc != 0:
                problems.append((Status.WARN, f"fetch failed: {fout.splitlines()[-1][:80] if fout else frc}"))
        urc, _ = await self.git("rev-parse", "--abbrev-ref", "@{u}")
        if urc == 0:
            _, counts = await self.git("rev-list", "--left-right", "--count", "HEAD...@{u}")
            try:
                ahead, behind = (int(x) for x in counts.split())
            except ValueError:
                ahead = behind = 0
            metrics.update(unpushed=float(ahead), behind=float(behind))
            if ahead:
                problems.append((Status(c.unpushed), f"{ahead} unpushed"))
            if behind:
                problems.append((Status(c.behind), f"{behind} behind remote"))
        else:
            raw.append("no upstream branch")

        if c.check_ci:
            ci = await self._ci(branch)
            if ci:
                problems.append(ci)
        problems = [p for p in problems if p[0] != Status.OK]
        if problems:
            return Result(Status.worst(p[0] for p in problems), f"{branch}: " + ", ".join(p[1] for p in problems),
                          metrics, "\n".join(raw))
        return Result.ok(f"{branch}: clean, in sync", metrics=metrics, raw="\n".join(raw))

    async def _ci(self, branch: str) -> tuple[Status, str] | None:
        c: GitConfig = self.config
        repo = c.github_repo
        if not repo:
            _, url = await self.git("remote", "get-url", "origin")
            m = _GH_RE.search(url)
            if not m:
                return Status.WARN, "CI: cannot derive GitHub repo from origin"
            repo = f"{m.group(1)}/{m.group(2)}"
        token = c.github_token or os.environ.get("GITHUB_TOKEN")
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            r = await self.ctx.http().get(f"{c.github_api}/repos/{repo}/actions/runs",
                                          params={"branch": branch, "per_page": 5}, headers=headers,
                                          timeout=self.timeout)
            r.raise_for_status()
            runs = r.json().get("workflow_runs", [])
        except Exception as e:
            return Status.WARN, f"CI: GitHub API error: {e}"
        done = [x for x in runs if x.get("status") == "completed"]
        if not done:
            return None
        latest = done[0]
        concl = latest.get("conclusion")
        if concl in ("failure", "timed_out", "startup_failure"):
            return Status.ALERT, f"CI {concl}: {latest.get('name')}"
        if concl == "cancelled":
            return Status.WARN, f"CI cancelled: {latest.get('name')}"
        return None
