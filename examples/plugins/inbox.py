"""Example plugin: alert when files pile up in a folder (a stuck work queue).

Copy to ~/.watchdog/plugins/ and use `type: inbox` in config.yaml.
"""
from pathlib import Path

from kwatchdog.core.models import Result
from kwatchdog.core.plugin import Watcher, WatcherConfig


class InboxConfig(WatcherConfig):
    path: str
    warn_above: int = 10
    alert_above: int = 100


class InboxWatcher(Watcher):
    type = "inbox"
    description = "alert when too many files wait in a folder"
    Config = InboxConfig

    async def check(self) -> Result:
        n = sum(1 for p in Path(self.config.path).expanduser().iterdir() if p.is_file())
        msg, metrics = f"{n} file(s) waiting", {"files": n}
        if n > self.config.alert_above:
            return Result.alert(msg, metrics=metrics)
        if n > self.config.warn_above:
            return Result.warn(msg, metrics=metrics)
        return Result.ok(msg, metrics=metrics)
