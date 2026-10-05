"""Command line: kwatchdog daemon | tui | run | add | check | list | plugins | validate | ping | init."""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import __version__
from .core.config import (STARTER_CONFIG, ConfigError, add_project, default_config_path, load_config,
                          upsert_watcher)
from .core.models import Status, fmt_age


def _cfg_path(args: argparse.Namespace) -> Path:
    return Path(args.config).expanduser() if args.config else default_config_path()


def ensure_config(path: Path, quiet: bool = False) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STARTER_CONFIG, encoding="utf-8")
        if not quiet:
            print(f"created starter config: {path}")


def _registries(path: Path):
    from .core.plugin import registries

    plugins = None
    try:
        from .core.config import Settings, _yaml_load

        raw = _yaml_load(path.read_text(encoding="utf-8")) or {}
        plugins = Settings.model_validate((raw.get("settings") or {})).path("plugins_dir")
    except Exception:
        from .core.config import watchdog_home

        plugins = watchdog_home() / "plugins"
    return registries(plugins)


def _store(path: Path):
    from .core.storage import Store

    try:
        cfg = load_config(path, *_registries(path))
        return Store(cfg.settings.path("db"))
    except ConfigError:
        from .core.config import Settings

        return Store(Settings().path("db"))


def cmd_daemon(args: argparse.Namespace) -> int:
    from .core.config import Settings
    from .core.daemon import Daemon, setup_logging

    path = _cfg_path(args)
    ensure_config(path)
    try:
        cfg = load_config(path, *_registries(path))
        log_file = cfg.settings.path("log_file") if cfg.settings.log_file else None
    except ConfigError:
        log_file = Settings().path("log_file")
    setup_logging(log_file, logging.DEBUG if args.verbose else logging.INFO, console=not args.quiet)
    d = Daemon(path)

    async def main() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, d.request_stop)
            except (NotImplementedError, RuntimeError):  # Windows
                pass
        await d.run_forever()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_tui(args: argparse.Namespace, embedded: bool = False) -> int:
    from .tui.app import WatchdogApp

    path = _cfg_path(args)
    ensure_config(path, quiet=True)
    app = WatchdogApp(path, embedded=embedded, splash=not args.no_splash)
    app.run()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    return cmd_tui(args, embedded=True)


def parse_value(s: str) -> Any:
    """CLI values are YAML scalars/flow: 8080 -> int, [200,301] -> list, true -> bool."""
    from .core.config import _yaml_load

    try:
        v = _yaml_load(s)
    except Exception:
        return s
    return s if v is None else v


def cmd_add(args: argparse.Namespace) -> int:
    path = _cfg_path(args)
    ensure_config(path)
    if not args.name:
        try:
            add_project(path, args.project, args.description or "")
        except ConfigError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"added project '{args.project}'")
        return 0
    if not args.type:
        print("error: watcher type required: kwatchdog add PROJECT NAME TYPE key=value ...", file=sys.stderr)
        return 2
    body: dict[str, Any] = {"name": args.name, "type": args.type}
    for kv in args.options:
        if "=" not in kv:
            print(f"error: option {kv!r} must be key=value", file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        body[k.strip()] = parse_value(v)
    # validate against a scratch copy before touching the real file
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td) / "config.yaml"
        shutil.copy(path, scratch)
        try:
            upsert_watcher(scratch, args.project, body)
            cfg = load_config(scratch, *_registries(path))
        except ConfigError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    key = f"{args.project}/{args.name}"
    spec = cfg.watcher(key)
    if spec is None or spec.error:
        print(f"error: {spec.error if spec else 'not added'}", file=sys.stderr)
        return 1
    upsert_watcher(path, args.project, body)
    print(f"added {key} ({args.type})" + (f" - note: {spec.unavailable}" if spec.unavailable else ""))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Run checks once in-process and print results (no daemon, no alerts)."""
    from .core.daemon import StoreContext
    from .core.storage import Store

    path = _cfg_path(args)
    wreg, creg = _registries(path)
    try:
        cfg = load_config(path, wreg, creg)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    store = Store(cfg.settings.path("db"))
    specs = [w for w in cfg.watchers() if not args.target or w.key == args.target or w.project == args.target]
    if not specs:
        print("no matching watchers")
        return 1

    async def run_all() -> list:
        from .core.daemon import Daemon

        d = Daemon(path, store=store, watcher_registry=wreg, channel_registry=creg, serve_heartbeat=False)
        out = []
        for spec in specs:
            if not spec.runnable:
                out.append((spec, None, spec.error or spec.unavailable or "disabled"))
                continue
            cls = wreg.get(spec.type)
            w = cls(spec.name, spec.config, StoreContext(store, spec.project, spec.name, d.shared_http),
                    timeout=spec.timeout)
            out.append((spec, await d.run_check(w, spec), ""))
        if d._http:
            await d._http.aclose()
        return out

    worst = Status.OK
    for spec, r, why in asyncio.run(run_all()):
        if r is None:
            print(f"{'SLEEPING':8} {spec.key:30} {why}")
            continue
        worst = Status.worst([worst, r.status])
        lat = f"{r.latency_ms:.0f}ms" if r.latency_ms is not None else ""
        print(f"{r.status.value:8} {spec.key:30} {lat:>7}  {r.message}")
        if args.verbose and r.raw:
            print("    " + r.raw.replace("\n", "\n    ")[:3000])
    return {Status.OK: 0, Status.WARN: 1, Status.ALERT: 2}.get(worst, 0)


def cmd_list(args: argparse.Namespace) -> int:
    from .core.daemon import daemon_alive

    path = _cfg_path(args)
    store = _store(path)
    meta = daemon_alive(store)
    print(f"daemon: {'running pid ' + str(meta['pid']) if meta else 'SLEEPING (not running)'}")
    now = time.time()
    for key, row in sorted(store.watcher_rows().items()):
        age = fmt_age(now - row.last_check) if row.last_check else "never"
        flags = ("muted " if row.muted(now) else "") + ("disabled " if row.disabled else "") + \
                ("flapping" if row.flapping else "")
        print(f"{row.status.value:8} {key:30} {age:>7}  {row.message} {flags}".rstrip())
    for e in store.kv_get("config_errors", []) or []:
        print(f"CONFIG   {e}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    path = _cfg_path(args)
    wreg, creg = _registries(path)
    try:
        cfg = load_config(path, wreg, creg)
    except ConfigError as e:
        print(f"error: {e}")
        return 1
    for k, v in {**wreg.errors, **creg.errors}.items():
        print(f"plugin error: {k}: {v}")
    for e in cfg.errors:
        print(f"error: {e}")
    for w in cfg.warnings:
        print(f"warning: {w}")
    print(f"{len(cfg.projects)} project(s), {len(cfg.watchers())} watcher(s), {len(cfg.channels)} channel(s), "
          f"{len(cfg.errors)} error(s)")
    return 1 if cfg.errors else 0


def cmd_plugins(args: argparse.Namespace) -> int:
    path = _cfg_path(args)
    wreg, creg = _registries(path)
    for title, reg in (("watchers", wreg), ("channels", creg)):
        print(f"{title}:")
        for name, cls in sorted(reg.items.items()):
            why = cls.unavailable_reason()
            state = "ok" if why is None else f"DISABLED ({why})"
            src = "" if cls.source == "builtin" else f" [{cls.source}]"
            print(f"  {name:10} {cls.description or ''}{src} - {state}")
            if args.verbose:
                for fname, f in cls.Config.model_fields.items():
                    req = "required" if f.is_required() else f"default={f.default!r}"
                    print(f"      {fname}: {req}")
        for k, v in reg.errors.items():
            print(f"  ERROR {k}: {v}")
    return 0


def cmd_ping(args: argparse.Namespace) -> int:
    """Record a heartbeat directly in the DB (same machine) - or use the HTTP endpoint."""
    store = _store(_cfg_path(args))
    store.beat(args.name)
    print(f"pinged '{args.name}'")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    path = _cfg_path(args)
    if path.exists() and not args.force:
        print(f"{path} already exists (use --force to overwrite)")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STARTER_CONFIG, encoding="utf-8")
    (path.parent / "plugins").mkdir(exist_ok=True)
    print(f"wrote {path}")
    return 0


def cmd_notify_test(args: argparse.Namespace) -> int:
    from .core.plugin import Notification

    path = _cfg_path(args)
    wreg, creg = _registries(path)
    cfg = load_config(path, wreg, creg)
    n = Notification(kind="test", project="kwatchdog", watcher="test", status="WARN",
                     message="test notification", ts=time.time())
    rc = 0
    for name, cs in cfg.channels.items():
        if args.channel and name != args.channel:
            continue
        if cs.error or cs.unavailable or cs.config is None:
            print(f"{name}: skipped ({cs.error or cs.unavailable})")
            continue
        try:
            asyncio.run(creg.get(cs.type)(name, cs.config).send(n))
            print(f"{name}: sent")
        except Exception as e:
            from .core.secrets import redact

            print(f"{name}: FAILED {redact(str(e))}")
            rc = 1
    return rc


def cmd_autofix(args: argparse.Namespace) -> int:
    """Kill switch + run log for auto-remediation."""
    import datetime as dt

    from .core.daemon import daemon_alive
    from .core.remediation import get_mode, set_mode

    store = _store(_cfg_path(args))
    action = args.action
    if action in ("on", "off", "dry-run"):
        set_mode(store, action)
        print(f"autofix: {action}")
        return 0
    if action == "status":
        print(f"autofix: {get_mode(store)}")
        pending = [r for r in store.runs(limit=200) if r["mode"] == "pending"]
        for r in pending:
            print(f"  pending #{r['id']} {r['wkey']}: {r['action']} ({r['command']})")
        return 0
    if action == "list":
        for r in store.runs(limit=args.limit):
            when = dt.datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M:%S")
            code = "" if r["exit_code"] is None else f"exit {r['exit_code']}"
            print(f"#{r['id']:<5} {when}  {r['mode']:<12} {r['wkey']:<28} {r['action']:<16} {code}")
            if args.verbose and r.get("output"):
                print("      " + r["output"].strip().replace("\n", "\n      ")[-1500:])
        return 0
    if action in ("confirm", "reject"):
        if not args.id:
            print("error: run id required", file=sys.stderr)
            return 2
        row = store.run(int(args.id))
        if row is None or row["mode"] != "pending":
            print(f"error: run #{args.id} is not pending", file=sys.stderr)
            return 1
        if not daemon_alive(store):
            print("error: the daemon isn't running; it executes confirmed fixes", file=sys.stderr)
            return 1
        store.push_command("fix_confirm" if action == "confirm" else "fix_reject", row["wkey"], str(args.id))
        print(f"{action} queued for run #{args.id} ({row['wkey']}: {row['action']})")
        return 0
    return 2


def cmd_digest(args: argparse.Namespace) -> int:
    """Print the daily digest now (and optionally send it through the digest channels)."""
    from .core.daemon import Daemon, daemon_alive
    from .core.digest import build_digest

    path = _cfg_path(args)
    wreg, creg = _registries(path)
    try:
        cfg = load_config(path, wreg, creg)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    from .core.storage import Store

    store = Store(cfg.settings.path("db"))
    now = time.time()
    last = store.kv_get("digest_last") or {}
    since = now - args.hours * 3600 if args.hours else float(last.get("ts") or now - 86400)
    meta = daemon_alive(store)
    _, text = build_digest(cfg, store, now, since, cfg.digest, daemon_started=meta.get("started") if meta else None)
    print(text)
    if args.send:
        async def send() -> list[str]:
            d = Daemon(path, store=store, watcher_registry=wreg, channel_registry=creg, serve_heartbeat=False)
            d.config = cfg
            d.build_channels(cfg)
            names = cfg.digest.channels or [n for n, c in cfg.channels.items() if c.type != "bell"]
            return await d.send_to(names, "digest", "kwatchdog", "daily", Status.OK, text)
        print("\nsent: " + (", ".join(asyncio.run(send())) or "no channels"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kwatchdog", description="kwatchdog - modular terminal monitoring")
    p.add_argument("--version", action="version", version=f"kwatchdog {__version__}")
    p.add_argument("-c", "--config", help="config file (default ~/.watchdog/config.yaml or $WATCHDOG_CONFIG)")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("daemon", help="run the checking daemon (headless)")
    s.add_argument("-v", "--verbose", action="store_true")
    s.add_argument("-q", "--quiet", action="store_true", help="log to file only")
    s.set_defaults(fn=cmd_daemon)

    for name, fn, hlp in (("tui", cmd_tui, "TUI client for a running daemon"),
                          ("run", cmd_run, "daemon + TUI in one process")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("--no-splash", action="store_true")
        s.set_defaults(fn=fn)

    s = sub.add_parser("add", help="add a project or a watcher: add PROJECT [NAME TYPE key=value ...]")
    s.add_argument("project")
    s.add_argument("name", nargs="?")
    s.add_argument("type", nargs="?")
    s.add_argument("options", nargs="*", help="key=value (values parsed as YAML)")
    s.add_argument("--description")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("check", help="run checks once and print (exit 0 OK / 1 WARN / 2 ALERT)")
    s.add_argument("target", nargs="?", help="project or project/watcher")
    s.add_argument("-v", "--verbose", action="store_true", help="show raw output")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("list", help="show current status from the database")
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("validate", help="validate the config file")
    s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("plugins", help="list watcher/channel types")
    s.add_argument("-v", "--verbose", action="store_true", help="show options")
    s.set_defaults(fn=cmd_plugins)
    s = sub.add_parser("ping", help="send a heartbeat for a 'heartbeat' watcher")
    s.add_argument("name")
    s.set_defaults(fn=cmd_ping)
    s = sub.add_parser("init", help="write a starter config")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("autofix", help="auto-remediation: on | off | dry-run | status | list | confirm ID | reject ID")
    s.add_argument("action", choices=["on", "off", "dry-run", "status", "list", "confirm", "reject"])
    s.add_argument("id", nargs="?")
    s.add_argument("-n", "--limit", type=int, default=30)
    s.add_argument("-v", "--verbose", action="store_true", help="show command output")
    s.set_defaults(fn=cmd_autofix)

    s = sub.add_parser("digest", help="print the daily digest now (--send to deliver it)")
    s.add_argument("--send", action="store_true", help="send through the digest channels")
    s.add_argument("--hours", type=float, default=0, help="cover the last N hours (default: since last digest)")
    s.set_defaults(fn=cmd_digest)

    s = sub.add_parser("notify-test", help="send a test notification through channels")
    s.add_argument("channel", nargs="?")
    s.set_defaults(fn=cmd_notify_test)
    return p


def main(argv: list[str] | None = None) -> int:
    if sys.platform.startswith("win"):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            except Exception:
                pass
    args = build_parser().parse_args(argv)
    if not getattr(args, "fn", None):  # bare `kwatchdog` = run
        args.fn, args.no_splash = cmd_run, False
    return int(args.fn(args) or 0)
