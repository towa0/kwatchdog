"""Secondary screens: splash, about, help, mute prompt, add/edit form, detail view."""
from __future__ import annotations

import datetime as dt
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Input, Label, Select, Sparkline, Static

from .. import __version__
from ..core.config import ConfigError, load_config, raw_watcher, upsert_watcher
from ..core.models import Status, fmt_age
from ..core.plugin import registries
from ..core.storage import Store
from .art import TAGLINE, TITLE, doberman_text
from .state import STATUS_STYLE, status_text


def _ts(ts: float | None) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S") if ts else "-"


class SplashScreen(Screen):
    """Boot splash: the dog, ~1.5 s, any key skips."""

    BINDINGS = [Binding("escape,enter,space", "close", "skip", show=False)]

    def __init__(self, duration: float = 1.5):
        super().__init__()
        self.duration = duration

    def compose(self) -> ComposeResult:
        with Vertical(id="splash-box"):
            yield Static(doberman_text(), id="dog")
            yield Static(TITLE, id="title")
            yield Static(TAGLINE, id="tagline")

    def on_mount(self) -> None:
        self.set_timer(self.duration, self.action_close)

    def on_key(self) -> None:
        self.action_close()

    def action_close(self) -> None:
        if self.is_current:
            self.dismiss()


class AboutScreen(ModalScreen):
    BINDINGS = [Binding("escape,q,i,enter", "app.pop_screen", "close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="splash-box"):
            yield Static(doberman_text(), id="dog")
            yield Static(TITLE, id="title")
            yield Static(TAGLINE, id="tagline")
            yield Static(f"kwatchdog {__version__}  ·  asyncio daemon + Textual TUI  ·  esc to close",
                         id="about-info")


HELP = """\
[b]NAVIGATION[/b]
  tab / shift+tab   switch panes (tree / table / alert feed)
  up / down         move          enter   drill down (history, raw output)
  /                 fuzzy search  esc     clear search / back

[b]ACTIONS[/b] (on the selected watcher, or the whole project)
  r   run check now          m   mute N minutes (0 = unmute)
  d   disable / enable       a   add watcher (form)
  e   edit watcher           ctrl+r  reload config
  t   send test notification     f   confirm pending autofix

[b]GENERAL[/b]
  ctrl+p  command palette     i  about     ?  this help     q  quit

[b]STATUS[/b]
  [bold #000000 on #ff1a1a] ALERT [/]  failing hard      [bold #ff1a1a] WARN [/]  degraded
  [#8b0000] OK [/]     healthy            [#5f5f5f] SLEEPING [/] disabled / muted / not yet checked
  [bold #8b0000 on #2e2e2e] BLOCKED [/] failing because a dependency is in ALERT (the dependency alerts, not this)

The border pulses while anything is in ALERT. Config errors show in the
banner under the top bar; the daemon keeps running the last good config.
"""


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,q,question_mark", "app.pop_screen", "close")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("KWATCHDOG // KEYS", classes="dialog-title")
            yield Static(HELP, id="help-text")


class MuteScreen(ModalScreen[float | None]):
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, target: str):
        super().__init__()
        self.target = target

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(f"MUTE {self.target}", classes="dialog-title")
            yield Label("minutes (0 = unmute)")
            yield Input(value="30", id="minutes", restrict=r"[0-9.]*")
            yield Static("", id="mute-error", classes="error")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        try:
            self.dismiss(float(event.value or 0))
        except ValueError:
            self.query_one("#mute-error", Static).update("enter a number")

    def action_cancel(self) -> None:
        self.dismiss(None)


COMMON_FIELDS = [("interval", "e.g. 60, 30s, 5m"), ("timeout", "seconds, e.g. 10"), ("retries", "0..10")]


class WatcherForm(ModalScreen[str | None]):
    """Add/edit a watcher. Fields come from the watcher's pydantic schema; values
    are YAML (8080, [200, 301], {warn_above: 80}). Validated before saving."""

    BINDINGS = [Binding("escape", "cancel", "cancel"), Binding("ctrl+s", "save", "save")]

    def __init__(self, config_path: Path, project: str = "", original: str | None = None):
        super().__init__()
        self.config_path = config_path
        self.project = project
        self.original = original
        self.initial: dict[str, Any] = {}
        if original:
            self.initial = raw_watcher(config_path, project, original) or {}
        self.wreg, _ = registries()
        self.type = str(self.initial.get("type") or "http")

    def compose(self) -> ComposeResult:
        title = f"EDIT {self.project}/{self.original}" if self.original else "ADD WATCHER"
        types = [(k, k) for k in sorted(self.wreg.items)]
        with Vertical(classes="dialog form"):
            yield Static(title, classes="dialog-title")
            with VerticalScroll(id="form-scroll"):
                with Horizontal(classes="form-row"):
                    yield Label("project *")
                    yield Input(value=self.project, id="f-project", compact=True)
                with Horizontal(classes="form-row"):
                    yield Label("name *")
                    yield Input(value=str(self.initial.get("name", "")), id="f-name", compact=True)
                with Horizontal(classes="form-row"):
                    yield Label("type *")
                    yield Select(types, value=self.type, allow_blank=False, id="f-type", compact=True)
                for name, hint in COMMON_FIELDS:
                    with Horizontal(classes="form-row"):
                        yield Label(name)
                        yield Input(value=_fmt(self.initial.get(name)), placeholder=hint, id=f"f-{name}",
                                    compact=True)
                yield Static("", id="type-hint", classes="hint")
                yield Vertical(id="opts")
            yield Static("", id="form-error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("cancel", id="cancel")
                yield Button("save  ctrl+s", id="save", classes="-primary")

    async def on_mount(self) -> None:
        await self._build_options()
        self.query_one("#f-name" if self.project else "#f-project", Input).focus()

    async def _build_options(self) -> None:
        box = self.query_one("#opts", Vertical)
        await box.remove_children()
        cls = self.wreg.get(self.type)
        if cls is None:
            return
        self.query_one("#type-hint", Static).update(
            f"{cls.description}" + (f"  [{cls.unavailable_reason()}]" if cls.unavailable_reason() else "")
            + "\nvalues are YAML: 8080 · [200, 301] · {warn_above: 80} · ${ENV_VAR} for secrets")
        rows = []
        for fname, f in cls.Config.model_fields.items():
            req = f.is_required()
            if req:
                hint = "required"
            elif f.default_factory is not None:
                hint = f"default {_fmt(f.default_factory())}"  # type: ignore[call-arg]
            else:
                hint = f"default {_fmt(f.default)}" if f.default is not None else "optional"
            ann = getattr(f.annotation, "__name__", None) or str(f.annotation).replace("typing.", "")
            value = _fmt(self.initial.get(fname)) if self.initial.get("type") == self.type else ""
            row = Horizontal(Label(f"{fname}{' *' if req else ''}"),
                             Input(value=value, placeholder=f"{hint} · {ann}"[:70], id=f"o-{fname}",
                                   compact=True),
                             classes="form-row")
            rows.append(row)
        await box.mount_all(rows)

    @on(Select.Changed, "#f-type")
    async def type_changed(self, event: Select.Changed) -> None:
        if event.value != self.type and isinstance(event.value, str):
            self.type = event.value
            await self._build_options()

    def _collect(self) -> tuple[str, dict[str, Any]]:
        from ..cli import parse_value

        project = self.query_one("#f-project", Input).value.strip()
        body: dict[str, Any] = {"name": self.query_one("#f-name", Input).value.strip(), "type": self.type}
        for name, _ in COMMON_FIELDS:
            v = self.query_one(f"#f-{name}", Input).value.strip()
            if v:
                body[name] = parse_value(v)
        for inp in self.query("#opts Input").results(Input):
            v = inp.value.strip()
            if v:
                body[inp.id[2:]] = parse_value(v)  # type: ignore[index]
        # keep keys the form doesn't know about (alerts, tags, enabled, description)
        for k, v in self.initial.items():
            if k in ("alerts", "tags", "enabled", "description") and k not in body:
                body[k] = v
        return project, body

    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        err = self.query_one("#form-error", Static)
        project, body = self._collect()
        if not project or not body["name"]:
            err.update("project and name are required")
            return
        if self.original and project != self.project:
            err.update("moving a watcher between projects: delete + add instead")
            return
        wreg, creg = registries()
        with tempfile.TemporaryDirectory() as td:
            scratch = Path(td) / "config.yaml"
            if self.config_path.exists():
                shutil.copy(self.config_path, scratch)
            try:
                upsert_watcher(scratch, project, body, original_name=self.original)
                cfg = load_config(scratch, wreg, creg)
            except ConfigError as e:
                err.update(str(e))
                return
        spec = cfg.watcher(f"{project}/{body['name']}")
        if spec is None or spec.error:
            err.update(spec.error if spec else "not saved")
            return
        upsert_watcher(self.config_path, project, body, original_name=self.original)
        self.dismiss(spec.key)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        import json

        return json.dumps(v)
    if hasattr(v, "model_dump"):
        import json

        return json.dumps({k: x for k, x in v.model_dump().items() if x is not None})
    return str(v)


class DetailScreen(Screen):
    """Drill-down: uptime, p50/p95, latency history, status strip, last 50 results,
    raw output of the selected result, incident timeline."""

    BINDINGS = [
        Binding("escape,backspace", "app.pop_screen", "back"),
        Binding("r", "run", "run now"),
        Binding("m", "mute", "mute"),
        Binding("d", "disable", "disable/enable"),
        Binding("e", "edit", "edit"),
    ]

    def __init__(self, key: str, store: Store):
        super().__init__()
        self.key = key
        self.store = store
        self.results: list = []

    def compose(self) -> ComposeResult:
        from textual.widgets import Footer

        yield Static(id="detail-head")
        yield Static(id="detail-stats")
        with Vertical(id="detail-chart-box") as box:
            box.border_title = "latency (ms) · status history"
            yield Sparkline([], id="detail-spark")
            yield Static(id="detail-strip")
        with Horizontal(id="detail-body"):
            t = DataTable(id="detail-results", cursor_type="row", zebra_stripes=False,
                          cursor_foreground_priority="renderable")
            t.border_title = "last 50 results"
            yield t
            with Vertical(id="detail-side"):
                with VerticalScroll(id="detail-raw-box") as raw:
                    raw.border_title = "raw output"
                    yield Static(id="detail-raw", markup=False)
                inc = DataTable(id="detail-incidents", cursor_type="row", cursor_foreground_priority="renderable")
                inc.border_title = "incidents"
                yield inc
                fx = DataTable(id="detail-fixes", cursor_type="row", cursor_foreground_priority="renderable")
                fx.border_title = "autofix runs"
                yield fx
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#detail-results", DataTable)
        t.add_columns("time", "status", "latency", "message")
        inc = self.query_one("#detail-incidents", DataTable)
        inc.add_columns("opened", "duration", "status", "message")
        self.query_one("#detail-fixes", DataTable).add_columns("#", "time", "mode", "exit", "output")
        self.refresh_data()
        self.set_interval(2.0, self.refresh_data)
        t.focus()

    def refresh_data(self) -> None:
        now = time.time()
        row = self.store.watcher_row(self.key)
        status = row.status if row else Status.SLEEPING
        if row and row.disabled:
            status = Status.SLEEPING
        head = Text()
        head.append_text(status_text(status))
        head.append(f"  {self.key}  ", style="bold #ff1a1a")
        if row:
            head.append(row.message, style="#ff1a1a")
            flags = []
            if row.muted(now):
                flags.append(f"muted {fmt_age((row.muted_until or now) - now)}")
            if row.disabled:
                flags.append("disabled")
            if row.flapping:
                flags.append("FLAPPING")
            if flags:
                head.append("   [" + ", ".join(flags) + "]", style="#5f5f5f")
            head.append(f"\nlast check {fmt_age(now - row.last_check) if row.last_check else 'never'} ago",
                        style="#8b0000")
        self.query_one("#detail-head", Static).update(head)

        s24 = self.store.stats(self.key, now - 86400)
        s7 = self.store.stats(self.key, now - 7 * 86400)

        def pct(v):
            return f"{v:.2f}%" if v is not None else "-"

        def ms(v):
            return f"{v:.0f}ms" if v is not None else "-"
        stats = Text()
        stats.append("uptime 24h ", style="#8b0000")
        stats.append(pct(s24["uptime"]), style="bold #ff1a1a")
        stats.append("   7d ", style="#8b0000")
        stats.append(pct(s7["uptime"]), style="bold #ff1a1a")
        stats.append("   p50 ", style="#8b0000")
        stats.append(ms(s24["p50"]), style="bold #ff1a1a")
        stats.append("   p95 ", style="#8b0000")
        stats.append(ms(s24["p95"]), style="bold #ff1a1a")
        stats.append(f"   checks 24h {s24['checks']}  warn {s24['warn']}  alert {s24['alert']}", style="#8b0000")
        self.query_one("#detail-stats", Static).update(stats)

        lats = self.store.latencies(self.key, 200)
        self.query_one("#detail-spark", Sparkline).data = lats or [0]
        recent = self.store.results(self.key, 200)
        strip = Text()
        width = max(10, self.size.width - 6)
        for r in reversed(recent[:width]):
            strip.append("█" if r.status == Status.ALERT else ("▆" if r.status == Status.WARN else "▁"),
                         style=STATUS_STYLE[r.status].split(" on ")[0].replace("bold #000000", "#ff1a1a")
                         .replace("bold ", ""))
        self.query_one("#detail-strip", Static).update(strip)

        self.results = recent[:50]
        t = self.query_one("#detail-results", DataTable)
        cur = t.cursor_row
        t.clear()
        for r in self.results:
            t.add_row(_ts(r.ts), status_text(r.status),
                      f"{r.latency_ms:.0f}ms" if r.latency_ms is not None else "",
                      Text(r.message[:200], style="#ff1a1a"), key=str(r.id))
        if self.results:
            t.move_cursor(row=min(cur, len(self.results) - 1))
        self._show_raw()

        inc = self.query_one("#detail-incidents", DataTable)
        inc.clear()
        for i in self.store.incidents(self.key, 30):
            dur = fmt_age((i.closed or now) - i.opened) + ("" if i.closed else " (open)")
            inc.add_row(_ts(i.opened), dur, status_text(i.status) if not i.closed else Text(" closed", "#5f5f5f"),
                        ("[esc] " if i.escalated else "") + i.message[:120])

        fx = self.query_one("#detail-fixes", DataTable)
        fx.clear()
        for r in self.store.runs(self.key, 20):
            out = (r.get("output") or "").strip().splitlines()
            fx.add_row(str(r["id"]), _ts(r["ts"]), Text(r["mode"], style="bold #ff1a1a" if r["mode"] == "pending" else "#8b0000"),
                       "" if r["exit_code"] is None else str(r["exit_code"]), (out[-1] if out else "")[:80])

    def _show_raw(self) -> None:
        t = self.query_one("#detail-results", DataTable)
        raw = self.query_one("#detail-raw", Static)
        if not self.results:
            raw.update("no results yet")
            return
        r = self.results[min(t.cursor_row, len(self.results) - 1)]
        metrics = "\n".join(f"{k} = {v}" for k, v in r.metrics.items())
        raw.update(f"{_ts(r.ts)}  {r.status.value}\n{r.message}\n\n{metrics}\n\n{r.raw or '(no raw output)'}")

    @on(DataTable.RowHighlighted, "#detail-results")
    def _row(self) -> None:
        self._show_raw()

    def action_run(self) -> None:
        self.app.run_check(self.key)  # type: ignore[attr-defined]

    def action_mute(self) -> None:
        self.app.mute(self.key)  # type: ignore[attr-defined]

    def action_disable(self) -> None:
        self.app.toggle_disable(self.key)  # type: ignore[attr-defined]

    def action_edit(self) -> None:
        self.app.edit(self.key)  # type: ignore[attr-defined]
