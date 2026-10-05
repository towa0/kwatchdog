from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path
from typing import Iterable

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import DataTable, Footer, Input, Static, Tree

from ..core.models import Status, fmt_age
from ..core.plugin import Notification
from .screens import AboutScreen, DetailScreen, HelpScreen, MuteScreen, SplashScreen, WatcherForm
from .state import STATUS_GLYPH, STATUS_STYLE, Snapshot, StateReader, fuzzy_score, sparkline, status_text

RED, DARK, GRAY = "#ff1a1a", "#8b0000", "#5f5f5f"

THEME = Theme(
    name="kwatchdog",
    primary=RED, secondary=DARK, accent=RED, warning=RED, error=RED, success=DARK,
    foreground=RED, background="#000000", surface="#000000", panel="#0d0000", boost="#1a0000",
    dark=True,
    variables={
        "block-cursor-background": "#5a0000", "block-cursor-foreground": RED,
        "block-cursor-blurred-background": "#3a0000", "block-hover-background": "#1a0000",
        "footer-background": "#000000", "footer-foreground": DARK, "footer-key-foreground": RED,
        "footer-description-foreground": DARK, "footer-item-background": "#000000",
        "input-selection-background": "#5a0000", "input-cursor-background": RED, "input-cursor-foreground": "#000000",
        "scrollbar": "#3a0000", "scrollbar-hover": DARK, "scrollbar-active": RED,
        "scrollbar-background": "#000000", "scrollbar-background-hover": "#000000",
        "scrollbar-background-active": "#000000", "scrollbar-corner-color": "#000000",
        "border": RED, "border-blurred": DARK, "foreground-muted": DARK, "foreground-disabled": GRAY,
        "link-color": RED, "link-background-hover": "#3a0000", "link-color-hover": RED,
        "screen-selection-background": "#5a0000", "screen-selection-foreground": RED,
        "button-foreground": RED, "button-color-foreground": RED,
        "text-muted": DARK, "text-disabled": GRAY,
    },
)

EVENT_STYLE = {"alert": RED, "escalation": f"bold {RED}", "update": RED, "recovery": DARK, "flapping": RED,
               "flap_end": DARK, "incident": DARK, "resolved": DARK, "reload": GRAY, "mute": GRAY,
               "disable": GRAY, "enable": GRAY, "stable": DARK, "test": GRAY, "autofix": RED,
               "digest": DARK, "budget": f"bold {RED}"}
EVENT_LABEL = {"alert": "NOTIFIED", "update": "WORSE", "escalation": "ESCALATED", "recovery": "RECOVERED",
               "flapping": "FLAPPING", "flap_end": "STABLE", "incident": "OPENED", "resolved": "CLOSED",
               "reload": "RELOAD", "mute": "MUTE", "disable": "DISABLED", "enable": "ENABLED", "test": "TEST",
               "stable": "STABLE", "autofix": "AUTOFIX", "digest": "DIGEST", "budget": "BUDGET"}
NOTIFY_KINDS = {"alert", "update", "escalation", "recovery", "flapping", "autofix", "budget"}


class MainScreen(Screen):
    BINDINGS = [
        Binding("a", "app.add", "add"),
        Binding("e", "app.edit_selected", "edit"),
        Binding("d", "app.disable_selected", "disable"),
        Binding("m", "app.mute_selected", "mute"),
        Binding("r", "app.run_selected", "run now"),
        Binding("slash", "search", "search"),
        Binding("enter", "drill", "detail", show=False),
        Binding("tab", "app.focus_next", "pane", show=False),
        Binding("shift+tab", "app.focus_previous", "pane", show=False),
        Binding("escape", "clear_search", "clear", show=False),
        Binding("question_mark", "app.help", "help"),
        Binding("ctrl+r", "app.reload", "reload"),
        Binding("t", "app.test_notify", "test notify", show=False),
        Binding("f", "app.confirm_fix", "confirm fix", show=False),
        Binding("i", "app.about", "about", show=False),
        Binding("q", "app.quit", "quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        yield Static(id="banner")
        with Horizontal(id="main"):
            with Vertical(id="left") as left:
                left.border_title = "PROJECTS"
                yield Input(placeholder="fuzzy search… (esc clears)", id="search")
                tree: Tree[str] = Tree("all", data="", id="tree")
                tree.show_root = False
                tree.guide_depth = 2
                yield tree
            with Vertical(id="right"):
                table = DataTable(id="table", cursor_type="row", zebra_stripes=False,
                                  cursor_foreground_priority="renderable")
                table.border_title = "WATCHERS"
                yield table
                feed = DataTable(id="feed", cursor_type="row", show_header=False,
                                 cursor_foreground_priority="renderable")
                feed.border_title = "ALERT FEED"
                yield feed
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#table", DataTable)
        for label, key, width in (("STATUS", "status", 9), ("WATCHER", "watcher", 22), ("LAST", "last", 6),
                                  ("LATENCY", "latency", 7), ("TREND", "trend", 16), ("MESSAGE", "message", None)):
            t.add_column(label, key=key, width=width)
        f = self.query_one("#feed", DataTable)
        f.add_column("time", key="time", width=8)
        f.add_column("kind", key="kind", width=9)
        f.add_column("status", key="status", width=8)
        f.add_column("watcher", key="watcher", width=26)
        f.add_column("message", key="message")
        t.focus()

    def on_resize(self) -> None:
        self.call_after_refresh(self.fit_columns)

    def fit_columns(self) -> None:
        """Give MESSAGE columns the remaining width so nothing scrolls sideways."""
        for table_id in ("#table", "#feed"):
            t = self.query_one(table_id, DataTable)
            cols = list(t.columns.values())
            if not cols or not t.size.width:
                continue
            fixed = sum(c.width + 2 * t.cell_padding for c in cols[:-1])
            last = cols[-1]
            last.auto_width = False
            last.width = max(10, t.size.width - fixed - 2 * t.cell_padding - 2)
            t.refresh()

    def action_search(self) -> None:
        s = self.query_one("#search", Input)
        s.add_class("show")
        s.focus()

    def action_clear_search(self) -> None:
        s = self.query_one("#search", Input)
        if s.value or s.has_class("show"):
            s.value = ""
            s.remove_class("show")
            self.app.query_filter = ""  # type: ignore[attr-defined]
            self.app.refresh_view(force=True)  # type: ignore[attr-defined]
            self.query_one("#table", DataTable).focus()

    @on(Input.Changed, "#search")
    def _search(self, event: Input.Changed) -> None:
        self.app.query_filter = event.value  # type: ignore[attr-defined]
        self.app.refresh_view(force=True)  # type: ignore[attr-defined]

    @on(Input.Submitted, "#search")
    def _search_done(self) -> None:
        self.query_one("#table", DataTable).focus()

    def action_drill(self) -> None:
        self.app.drill()  # type: ignore[attr-defined]


class WatchdogApp(App):
    CSS_PATH = "theme.tcss"
    TITLE = "kwatchdog"
    ENABLE_COMMAND_PALETTE = True
    BINDINGS = [Binding("h", "toggle_footer", "hide keys")]
    FOOTER_KEY = "tui:footer_hidden"

    def __init__(self, config_path: Path, *, embedded: bool = False, splash: bool = True,
                 refresh_interval: float = 1.0):
        super().__init__()
        self.config_path = Path(config_path)
        self.embedded = embedded
        self.show_splash = splash
        self.refresh_interval = refresh_interval
        self.reader = StateReader(self.config_path)
        self.store = self.reader.store
        self.snap: Snapshot | None = None
        self.daemon = None
        self.query_filter = ""
        self.selected_key = ""  # "project" or "project/watcher"
        self._table_keys: list[str] = []
        self._tree_sig: tuple = ()
        self._tree_nodes: dict[str, object] = {}
        self._last_event_id = max((e.id for e in self.store.events(1)), default=0)
        self._pulse = False

    async def on_mount(self) -> None:
        self.register_theme(THEME)
        self.theme = "kwatchdog"
        self.set_class(bool(self.store.kv_get(self.FOOTER_KEY, False)), "-hide-footer")
        await self.push_screen(MainScreen())
        if self.embedded:
            await self._start_daemon()
        self.call_after_refresh(self.main.fit_columns)  # type: ignore[union-attr]
        self.refresh_view(force=True)
        self.set_interval(self.refresh_interval, self.refresh_view)
        self.set_interval(0.5, self._pulse_border)
        if self.show_splash:
            await self.push_screen(SplashScreen())

    async def _start_daemon(self) -> None:
        from ..channels.builtin import BellChannel
        from ..core.daemon import Daemon, setup_logging

        st = self.reader.config.settings
        setup_logging(st.path("log_file") if st.log_file else None, logging.INFO, console=False)
        self.daemon = Daemon(self.config_path, store=self.store)
        BellChannel.hook = self._bell_hook
        await self.daemon.start()

    async def on_unmount(self) -> None:
        if self.daemon is not None:
            await self.daemon.stop()

    def _bell_hook(self, n: Notification) -> None:
        self.bell()

    @property
    def main(self) -> MainScreen | None:
        for s in self.screen_stack:
            if isinstance(s, MainScreen):
                return s
        return None

    def refresh_view(self, force: bool = False) -> None:
        try:
            self.snap = self.reader.snapshot()
        except Exception as e:  # DB locked etc: keep the last view
            self.log.error(f"snapshot failed: {e}")
            return
        main = self.main
        if main is None:
            return
        snap = self.snap
        self._render_topbar(main, snap)
        self._render_banner(main, snap)
        self._render_tree(main, snap, force)
        self._render_table(main, snap)
        self._render_feed(main, snap)
        self._toast_new_events(snap)

    def _visible(self, snap: Snapshot) -> list:
        ws = snap.config.watchers()
        if self.query_filter:
            scored = [(fuzzy_score(self.query_filter, f"{w.key} {w.type}"), w) for w in ws]
            ws = [w for s, w in sorted(((s, w) for s, w in scored if s is not None), key=lambda x: -x[0])]
        return ws

    def _render_topbar(self, main: MainScreen, snap: Snapshot) -> None:
        c = snap.counts()
        t = Text()
        t.append(" KWATCHDOG ", style=f"bold #000000 on {RED}")
        if snap.daemon:
            mode = "embedded" if self.embedded else f"pid {snap.daemon.get('pid')}"
            t.append(f"  daemon {mode} ", style=DARK)
            port = snap.daemon.get("heartbeat_port")
            if port:
                t.append(f":{port} ", style=GRAY)
            if snap.daemon.get("status_url"):
                t.append(f"{snap.daemon['status_url']} ", style=GRAY)
        else:
            t.append("  daemon SLEEPING - run `kwatchdog daemon` ", style=f"bold {RED}")
        t.append("  ")
        for s in (Status.ALERT, Status.WARN, Status.BLOCKED, Status.OK, Status.SLEEPING):
            t.append(f" {c[s]} {s.value} ", style=STATUS_STYLE[s] if c[s] else GRAY)
            t.append(" ")
        if snap.autofix_mode != "on":
            t.append(f"  AUTOFIX {snap.autofix_mode.upper()} ", style=f"bold {DARK}")
        if snap.pending_fixes:
            t.append(f"  {len(snap.pending_fixes)} fix(es) awaiting confirm [f] ", style=f"bold {RED}")
        if snap.errors:
            t.append(f"  {len(snap.errors)} config error(s)", style=f"bold {RED}")
        t.append(f"   {dt.datetime.now():%H:%M:%S}", style=DARK)
        main.query_one("#topbar", Static).update(t)
        alerting = c[Status.ALERT] > 0
        main.set_class(alerting, "alerting")

    def _pulse_border(self) -> None:
        main = self.main
        if main is None:
            return
        self._pulse = not self._pulse
        main.set_class(self._pulse, "pulse-on")
        main.set_class(not self._pulse, "pulse-off")

    def _render_banner(self, main: MainScreen, snap: Snapshot) -> None:
        b = main.query_one("#banner", Static)
        lines = [Text(f"✖ {e}", style=f"bold {RED}") for e in snap.errors[:4]]
        if len(snap.errors) > 4:
            lines.append(Text(f"  … {len(snap.errors) - 4} more (kwatchdog validate)", style=DARK))
        lines += [Text(f"· {w}", style=GRAY) for w in snap.warnings[: max(0, 5 - len(lines))]]
        b.set_class(bool(lines), "show")
        b.update(Text("\n").join(lines) if lines else "")

    def _render_tree(self, main: MainScreen, snap: Snapshot, force: bool) -> None:
        tree: Tree[str] = main.query_one("#tree", Tree)
        visible = {w.key for w in self._visible(snap)}
        sig = tuple((p.name, tuple(w.key for w in p.watchers if w.key in visible))
                    for p in snap.config.projects.values())
        if sig != self._tree_sig or force:
            if sig != self._tree_sig:
                self._tree_sig = sig
                tree.clear()
                self._tree_nodes = {}
                for pname, keys in sig:
                    if self.query_filter and not keys:
                        continue
                    pn = tree.root.add("", data=pname, expand=True)
                    self._tree_nodes[pname] = pn
                    for k in keys:
                        self._tree_nodes[k] = pn.add_leaf("", data=k)
        now = time.time()
        for key, node in self._tree_nodes.items():
            if "/" in key:
                st = snap.status(key)
                row = snap.rows.get(key)
                label = Text()
                label.append(STATUS_GLYPH[st], style=STATUS_STYLE[st])
                label.append(f" {key.split('/', 1)[1]}", style=RED if st != Status.SLEEPING else GRAY)
                if row and row.muted(now):
                    label.append(" (muted)", style=GRAY)
                if row and row.last_check:
                    label.append(f" {fmt_age(now - row.last_check)}", style=DARK)
            else:
                st = snap.project_status(key)
                label = Text()
                label.append(STATUS_GLYPH[st], style=STATUS_STYLE[st])
                label.append(f" {key}", style=f"bold {RED}")
                n = len(snap.config.projects[key].watchers) if key in snap.config.projects else 0
                label.append(f" ({n})", style=DARK)
            node.set_label(label)  # type: ignore[attr-defined]

    def _render_table(self, main: MainScreen, snap: Snapshot) -> None:
        table = main.query_one("#table", DataTable)
        sel = self.selected_key
        ws = self._visible(snap)
        if sel and "/" not in sel and not self.query_filter:
            ws = [w for w in ws if w.project == sel]
        keys = [w.key for w in ws]
        now = time.time()
        if keys != self._table_keys:
            cur_key = self._table_keys[table.cursor_row] if self._table_keys and table.cursor_row < len(self._table_keys) else None
            table.clear()
            for w in ws:
                table.add_row(*self._cells(w, snap, now), key=w.key)
            self._table_keys = keys
            if cur_key in keys:
                table.move_cursor(row=keys.index(cur_key))
            table.border_title = f"WATCHERS · {sel if sel and '/' not in sel else 'all'}" + \
                                 (f" · /{self.query_filter}" if self.query_filter else "")
        else:
            for w in ws:
                for col, val in zip(("status", "watcher", "last", "latency", "trend", "message"),
                                    self._cells(w, snap, now)):
                    table.update_cell(w.key, col, val)

    def _cells(self, w, snap: Snapshot, now: float) -> tuple:
        st = snap.status(w.key)
        row = snap.rows.get(w.key)
        lat = row.latency_ms if row else None
        msg = row.message if row else (w.error or w.unavailable or "")
        flags = ""
        if row and row.muted(now):
            flags += "[muted] "
        if row and row.flapping:
            flags += "[FLAPPING] "
        return (
            status_text(st, 9),
            Text(w.key, style=RED if st != Status.SLEEPING else GRAY),
            Text(fmt_age(now - row.last_check) if row and row.last_check else "-", style=DARK),
            Text(f"{lat:.0f}ms" if lat is not None else "", style=RED),
            Text(sparkline(snap.latencies.get(w.key, []), 16), style=DARK if st != Status.ALERT else RED),
            Text(flags + msg, style=RED if st.failing else (GRAY if st == Status.SLEEPING else DARK),
                 no_wrap=True, overflow="ellipsis"),
        )

    def _render_feed(self, main: MainScreen, snap: Snapshot) -> None:
        feed = main.query_one("#feed", DataTable)
        ids = [str(e.id) for e in snap.events]
        if ids == getattr(self, "_feed_ids", None):
            return
        self._feed_ids = ids
        feed.clear()
        for e in snap.events:
            style = EVENT_STYLE.get(e.kind, DARK)
            msg = e.message + (f"  → {e.delivered}" if e.delivered and e.kind in NOTIFY_KINDS else "")
            try:
                st_cell = status_text(Status(e.status), 8) if e.kind in NOTIFY_KINDS | {"incident"} else Text("")
            except ValueError:
                st_cell = Text(e.status, style=DARK)
            feed.add_row(Text(dt.datetime.fromtimestamp(e.ts).strftime("%H:%M:%S"), style=DARK),
                         Text(EVENT_LABEL.get(e.kind, e.kind.upper()), style=style), st_cell,
                         Text(e.key, style=RED), Text(msg, style=style, no_wrap=True, overflow="ellipsis"),
                         key=str(e.id))

    def _toast_new_events(self, snap: Snapshot) -> None:
        new = [e for e in snap.events if e.id > self._last_event_id]
        if not new:
            return
        self._last_event_id = max(e.id for e in new)
        for e in reversed(new[:3]):
            if e.kind in NOTIFY_KINDS:
                sev = "error" if e.status == "ALERT" and e.kind != "recovery" else (
                    "information" if e.kind == "recovery" else "warning")
                self.notify(e.message[:200], title=f"{e.kind.upper()} {e.key}", severity=sev, markup=False)

    @on(Tree.NodeHighlighted)
    def _tree_hl(self, event: Tree.NodeHighlighted) -> None:
        data = event.node.data
        if data is not None:
            self.selected_key = data
            if "/" not in data:
                self._table_keys = []  # project changed: rebuild table
            self.refresh_view()

    @on(Tree.NodeSelected)
    def _tree_sel(self, event: Tree.NodeSelected) -> None:
        if event.node.data and "/" in event.node.data:
            self.drill(event.node.data)

    @on(DataTable.RowHighlighted, "#table")
    def _table_hl(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            self._table_selected = event.row_key.value

    @on(DataTable.RowSelected, "#table")
    def _table_sel(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value:
            self.drill(event.row_key.value)

    @on(DataTable.RowSelected, "#feed")
    def _feed_sel(self, event: DataTable.RowSelected) -> None:
        ev = next((e for e in (self.snap.events if self.snap else []) if str(e.id) == event.row_key.value), None)
        if ev and ev.key and "/" in ev.key:
            self.drill(ev.key)

    def target(self) -> str:
        """Watcher under the cursor in the focused pane, else the tree selection."""
        main = self.main
        focused = self.focused
        if main is not None and focused is main.query_one("#table", DataTable):
            k = getattr(self, "_table_selected", "")
            if k:
                return k
        return self.selected_key or getattr(self, "_table_selected", "")

    def drill(self, key: str | None = None) -> None:
        key = key or self.target()
        if key and "/" in key:
            self.push_screen(DetailScreen(key, self.store))

    def run_check(self, key: str) -> None:
        if not key:
            return
        self.store.push_command("run", key)
        self.notify(f"check queued: {key}", timeout=2)

    def mute(self, key: str) -> None:
        if not key:
            return

        def done(minutes: float | None) -> None:
            if minutes is not None:
                self.store.push_command("mute", key, str(minutes))
                self.notify(f"{key}: " + (f"muted {minutes:g} min" if minutes else "unmuted"), timeout=3)
        self.push_screen(MuteScreen(key), done)

    def toggle_disable(self, key: str) -> None:
        if not key:
            return
        keys = [key] if "/" in key else [w.key for w in (self.snap.config.watchers() if self.snap else [])
                                          if w.project == key]
        rows = self.snap.rows if self.snap else {}
        currently = all(rows.get(k) is not None and rows[k].disabled for k in keys)
        self.store.push_command("disable", key, "0" if currently else "1")
        self.notify(f"{key}: {'enabled' if currently else 'disabled'}", timeout=3)

    def edit(self, key: str) -> None:
        if not key or "/" not in key:
            self.notify("select a watcher to edit", severity="warning", timeout=3)
            return
        project, name = key.split("/", 1)

        def done(saved: str | None) -> None:
            if saved:
                self.notify(f"saved {saved} - daemon reloads automatically", timeout=3)
        self.push_screen(WatcherForm(self.config_path, project, name), done)

    def action_add(self) -> None:
        project = (self.selected_key or "").split("/", 1)[0]

        def done(saved: str | None) -> None:
            if saved:
                self.notify(f"added {saved} - daemon reloads automatically", timeout=3)
                self.refresh_view(force=True)
        self.push_screen(WatcherForm(self.config_path, project), done)

    def action_edit_selected(self) -> None:
        self.edit(self.target())

    def action_disable_selected(self) -> None:
        self.toggle_disable(self.target())

    def action_mute_selected(self) -> None:
        self.mute(self.target())

    def action_run_selected(self) -> None:
        self.run_check(self.target() or "*")

    def action_reload(self) -> None:
        self.store.push_command("reload")
        self.reader.load_config(force=True)
        self.refresh_view(force=True)
        self.notify("config reload requested", timeout=2)

    def action_confirm_fix(self) -> None:
        """Confirm the pending autofix of the selected watcher (or the only pending one)."""
        pending = self.snap.pending_fixes if self.snap else []
        key = self.target()
        mine = [r for r in pending if r["wkey"] == key] or (pending if len(pending) == 1 else [])
        if not mine:
            self.notify("no pending autofix for this watcher", severity="warning", timeout=3)
            return
        run = mine[0]
        self.store.push_command("fix_confirm", run["wkey"], str(run["id"]))
        self.notify(f"confirmed autofix #{run['id']} ({run['action']}) for {run['wkey']}", timeout=4)

    def action_autofix_mode(self, mode: str) -> None:
        from ..core.remediation import set_mode

        set_mode(self.store, mode)
        self.notify(f"autofix: {mode}", timeout=3)
        self.refresh_view(force=True)

    def action_test_notify(self) -> None:
        self.store.push_command("test", self.target())
        self.notify("test notification queued", timeout=2)

    def action_toggle_footer(self) -> None:
        """Hide/show the key bar at the bottom (remembered across restarts)."""
        hidden = not self.has_class("-hide-footer")
        self.set_class(hidden, "-hide-footer")
        self.store.kv_set(self.FOOTER_KEY, hidden)
        if hidden:
            self.notify("key bar hidden - press h to show it again", timeout=3)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_about(self) -> None:
        self.push_screen(AboutScreen())

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield SystemCommand("Run all checks now", "Queue every watcher", lambda: self.run_check("*"))
        yield SystemCommand("Reload config", "Re-read the YAML file", self.action_reload)
        yield SystemCommand("Add watcher", "Open the add form", self.action_add)
        yield SystemCommand("Mute selected", "Mute for N minutes", self.action_mute_selected)
        yield SystemCommand("Unmute all", "Clear every mute", lambda: self.store.push_command("mute", "*", "0"))
        yield SystemCommand("Send test notification", "Through all channels", self.action_test_notify)
        yield SystemCommand("Autofix: OFF (kill switch)", "Stop all auto-remediation",
                            lambda: self.action_autofix_mode("off"))
        yield SystemCommand("Autofix: dry-run", "Log what would run, run nothing",
                            lambda: self.action_autofix_mode("dry-run"))
        yield SystemCommand("Autofix: on", "Re-enable auto-remediation", lambda: self.action_autofix_mode("on"))
        yield SystemCommand("Confirm pending fix", "Run the queued fix of the selected watcher",
                            self.action_confirm_fix)
        yield SystemCommand("Hide / show key bar", "Toggle the controls at the bottom (h)",
                            self.action_toggle_footer)
        yield SystemCommand("Help", "Key bindings", self.action_help)
        yield SystemCommand("About kwatchdog", "The dog", self.action_about)
        if self.snap:
            for w in self.snap.config.watchers():
                yield SystemCommand(f"Open {w.key}", f"{w.type} · detail view",
                                    lambda k=w.key: self.drill(k), discover=False)
        yield SystemCommand("Quit", "Exit the TUI", self.exit)
