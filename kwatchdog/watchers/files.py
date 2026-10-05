"""File watchers: file/dir freshness, log tail matcher."""
from __future__ import annotations

import asyncio
import glob
import os
import re
import time
from pathlib import Path

from pydantic import Field, field_validator

from ..core.models import Duration, Result, Status, fmt_age
from ..core.plugin import Watcher, WatcherConfig


class FileConfig(WatcherConfig):
    path: str  # file, directory, or glob ("logs/*.log")
    max_age: Duration | None = None  # ALERT if newest write older than this
    warn_age: Duration | None = None
    min_size: int | None = None  # bytes
    recursive: bool = False  # for directories


class FileWatcher(Watcher):
    type = "file"
    description = "file/dir freshness: newest write age, size, existence"
    Config = FileConfig
    default_interval = 60

    async def check(self) -> Result:
        return await asyncio.to_thread(self._check)

    def _check(self) -> Result:
        c: FileConfig = self.config
        p = os.path.expanduser(c.path)
        if any(ch in p for ch in "*?["):
            files = [Path(f) for f in glob.glob(p, recursive=True) if os.path.isfile(f)]
        elif os.path.isdir(p):
            it = Path(p).rglob("*") if c.recursive else Path(p).iterdir()
            files = [f for f in it if f.is_file()]
        elif os.path.isfile(p):
            files = [Path(p)]
        else:
            return Result.alert(f"{c.path} does not exist")
        if not files:
            return Result.alert(f"{c.path}: no files")
        newest = max(files, key=lambda f: f.stat().st_mtime)
        st = newest.stat()
        age = time.time() - st.st_mtime
        metrics = {"age_s": round(age, 1), "size": float(st.st_size), "files": float(len(files))}
        name = newest.name if len(files) > 1 else c.path
        status, msg = Status.OK, f"{name} written {fmt_age(age)} ago"
        if c.max_age is not None and age > c.max_age:
            status, msg = Status.ALERT, f"{name} stale: last write {fmt_age(age)} ago (> {fmt_age(c.max_age)})"
        elif c.warn_age is not None and age > c.warn_age:
            status, msg = Status.WARN, f"{name} getting stale: {fmt_age(age)} ago (> {fmt_age(c.warn_age)})"
        if c.min_size is not None and st.st_size < c.min_size:
            status = Status.worst([status, Status.ALERT])
            msg += f"; size {st.st_size}B < {c.min_size}B"
        return Result(status, msg, metrics)


class LogTailConfig(WatcherConfig):
    path: str
    patterns: list[str] = Field(default_factory=lambda: [r"\bERROR\b", r"Traceback \(most recent call last\)"])
    warn_patterns: list[str] = Field(default_factory=list)  # e.g. ["\\b429\\b", "WARNING"]
    ignore: list[str] = Field(default_factory=list)  # lines matching these are skipped
    ignore_case: bool = False
    min_matches: int = Field(1, ge=1)  # ALERT only if at least this many lines match
    from_start: bool = False  # first run: scan whole file instead of starting at the end
    max_bytes: int = 2_000_000  # per check
    encoding: str = "utf-8"

    @field_validator("patterns", "warn_patterns", "ignore")
    @classmethod
    def _re(cls, v):
        for p in v:
            re.compile(p)
        return v


class LogTailWatcher(Watcher):
    type = "logtail"
    description = "regex on new log lines (ERROR, Traceback, 429, ...); rotation-safe"
    Config = LogTailConfig
    default_interval = 30

    async def check(self) -> Result:
        return await asyncio.to_thread(self._check)

    def _check(self) -> Result:
        c: LogTailConfig = self.config
        p = Path(c.path).expanduser()
        try:
            st = p.stat()
        except OSError:
            return Result.alert(f"{c.path} does not exist")
        flags = re.I if c.ignore_case else 0
        pos = self.ctx.state_get("pos")
        ino = self.ctx.state_get("ino")
        cur_ino = f"{st.st_ino}:{st.st_dev}"
        if pos is None:
            pos = 0 if c.from_start else st.st_size
        with p.open("rb") as f:
            head = f.read(64).hex()
            # rotated (new inode), truncated (smaller), or rewritten in place (head changed)
            old_head = self.ctx.state_get("head")
            if (ino not in (None, cur_ino) or st.st_size < pos
                    or (old_head is not None and pos > 0 and not head.startswith(old_head[: 2 * min(64, pos)]))):
                pos = 0
            f.seek(pos)
            chunk = f.read(c.max_bytes)
        # only consume complete lines; a partial last line is re-read next time
        nl = chunk.rfind(b"\n")
        consumed = chunk[: nl + 1] if nl >= 0 else b""
        if nl < 0 and len(chunk) >= c.max_bytes:
            consumed = chunk
        self.ctx.state_set("pos", pos + len(consumed))
        self.ctx.state_set("ino", cur_ino)
        self.ctx.state_set("head", head)
        lines = consumed.decode(c.encoding, errors="replace").splitlines()
        alert_re = [re.compile(x, flags) for x in c.patterns]
        warn_re = [re.compile(x, flags) for x in c.warn_patterns]
        ign_re = [re.compile(x, flags) for x in c.ignore]
        alerts, warns = [], []
        for line in lines:
            if any(r.search(line) for r in ign_re):
                continue
            if any(r.search(line) for r in alert_re):
                alerts.append(line)
            elif any(r.search(line) for r in warn_re):
                warns.append(line)
        metrics = {"new_lines": float(len(lines)), "matches": float(len(alerts)), "warn_matches": float(len(warns))}
        raw = "\n".join((alerts + warns)[-50:])
        if len(alerts) >= c.min_matches:
            return Result(Status.ALERT, f"{len(alerts)} matching line(s): {alerts[-1].strip()[:120]}", metrics, raw)
        if warns or alerts:
            last = (warns or alerts)[-1].strip()[:120]
            return Result(Status.WARN, f"{len(warns) + len(alerts)} warning line(s): {last}", metrics, raw)
        return Result.ok(f"{len(lines)} new line(s), no matches", metrics=metrics)
