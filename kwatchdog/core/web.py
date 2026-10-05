"""Read-only status page + JSON API.

* Binds 127.0.0.1 by default. ``host: lan`` (0.0.0.0) and ``host: tailscale``
  (the machine's Tailscale IPv4, from ``tailscale ip -4``) are opt-in. If
  Tailscale can't be resolved it refuses to bind; it never falls back to all
  interfaces.
* Optional bearer token: ``Authorization: Bearer <token>``. A phone browser can
  open ``/?token=<token>`` once. That sets an HttpOnly, SameSite=Strict cookie
  and redirects to a clean URL.
* GET/HEAD only, and no endpoint changes any state. Every dynamic value is
  HTML-escaped and a strict CSP blocks scripts, so a scraped page can't inject
  into the status page. Raw check output and autofix output are not exposed.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import html
import json
import logging
import shutil
import subprocess
import sys
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from .models import Status, fmt_age

log = logging.getLogger("kwatchdog.web")

MAX_LINE = 8192
MAX_HEADERS = 64
COOKIE = "kw_token"
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class StatusPageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    host: str = "127.0.0.1"  # IP/hostname, "lan" (= 0.0.0.0) or "tailscale"
    port: int = Field(8788, ge=0, le=65535)
    token: str | None = None  # ${STATUS_TOKEN}; strongly recommended for lan/tailscale
    refresh: int = Field(15, ge=5, le=3600)  # page auto-refresh, seconds
    title: str = "kwatchdog"


def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")


def resolve_host(host: str) -> str:
    if host == "lan":
        return "0.0.0.0"
    if host == "tailscale":
        exe = shutil.which("tailscale")
        if not exe:
            raise OSError("host: tailscale, but the tailscale CLI isn't installed")
        kw = {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}
        out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=10, **kw)
        ip = (out.stdout.strip().splitlines() or [""])[0].strip()
        if out.returncode != 0 or not ip.startswith("100."):
            raise OSError(f"could not get a Tailscale IPv4 address: {(out.stderr or out.stdout).strip()[:120]}")
        return ip
    return host


# --------------------------------------------------------------------- data
def build_status(daemon) -> dict[str, Any]:
    """Everything the page and /api/status show. Read-only."""
    from .daemon import daemon_alive
    from .remediation import get_mode

    store, cfg = daemon.store, daemon.config
    now = time.time()
    rows = store.watcher_rows()
    projects = []
    for p in cfg.projects.values():
        ws = []
        for w in p.watchers:
            row = rows.get(w.key)
            status = Status.SLEEPING if row is None or row.disabled else row.status
            s24, s7 = store.stats(w.key, now - 86400), store.stats(w.key, now - 7 * 86400)
            ws.append({
                "key": w.key, "name": w.name, "type": w.type, "status": status.value,
                "message": (row.message if row else (w.error or w.unavailable or ""))[:300],
                "last_check": row.last_check if row else None,
                "latency_ms": round(row.latency_ms, 1) if row and row.latency_ms is not None else None,
                "uptime_24h": _r(s24["uptime"]), "uptime_7d": _r(s7["uptime"]),
                "p95_ms_24h": _r(s24["p95"]), "muted": bool(row and row.muted(now)),
                "disabled": bool(row and row.disabled), "flapping": bool(row and row.flapping),
                "depends_on": list(w.depends_on), "slo": w.slo,
                "budget": (store.kv_get(f"budget:{w.key}") or {}).get("text") if w.slo else None,
            })
        up24 = [x["uptime_24h"] for x in ws if x["uptime_24h"] is not None]
        up7 = [x["uptime_7d"] for x in ws if x["uptime_7d"] is not None]
        projects.append({
            "name": p.name, "description": p.description,
            "status": Status.worst(Status(x["status"]) for x in ws).value,
            "uptime_24h": _r(sum(up24) / len(up24)) if up24 else None,
            "uptime_7d": _r(sum(up7) / len(up7)) if up7 else None,
            "watchers": ws,
        })
    incidents = store.incidents(limit=30)

    def inc(i) -> dict:
        return {"id": i.id, "key": i.key, "status": i.status.value, "message": i.message[:300],
                "opened": i.opened, "closed": i.closed, "escalated": i.escalated,
                "duration_s": round((i.closed or now) - i.opened, 1)}

    statuses = [Status(w["status"]) for p in projects for w in p["watchers"]]
    counts = {s.value: sum(1 for x in statuses if x == s) for s in Status}
    meta = daemon_alive(store)
    return {
        "generated": now,
        "overall": Status.worst(statuses).value,
        "counts": counts,
        "daemon": {"running": bool(meta), "started": meta.get("started") if meta else None},
        "autofix": get_mode(store),
        "projects": projects,
        "incidents": {"open": [inc(i) for i in incidents if i.closed is None],
                      "recent": [inc(i) for i in incidents if i.closed is not None][:15]},
    }


def _r(v: float | None) -> float | None:
    return round(v, 3) if v is not None else None


# --------------------------------------------------------------------- html
CSS = """
:root{--red:#ff1a1a;--dark:#8b0000;--grey:#5f5f5f;--bg:#000}
*{box-sizing:border-box}
html,body{max-width:100%;overflow-x:hidden}
body{margin:0;background:var(--bg);color:var(--red);font:14px/1.4 ui-monospace,Menlo,Consolas,monospace}
header{padding:12px 14px;border-bottom:2px solid var(--dark);display:flex;flex-wrap:wrap;gap:8px;align-items:center}
header h1{font-size:16px;margin:0 12px 0 0;letter-spacing:.3em}
body.alerting header{border-bottom-color:var(--red);animation:pulse 1s steps(2) infinite}
@keyframes pulse{50%{border-bottom-color:var(--dark)}}
main{padding:8px 10px 40px;max-width:980px;margin:auto}
.b{display:inline-block;min-width:74px;text-align:center;padding:1px 6px;font-weight:bold;border-radius:2px}
.ALERT{background:var(--red);color:#000}.WARN{color:var(--red);border:1px solid var(--red)}
.OK{color:var(--dark);border:1px solid var(--dark)}.SLEEPING{color:var(--grey);border:1px solid #333}
.BLOCKED{color:var(--red);background:#2e2e2e;font-weight:normal}
.muted{color:var(--grey)}.dim{color:var(--dark)}
details{border:1px solid var(--dark);margin:10px 0;border-radius:3px}
details[open]{border-color:var(--red)}
summary{cursor:pointer;padding:8px 10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;list-style:none}
summary::-webkit-details-marker{display:none}
summary .name{font-weight:bold;flex:1}
.w{padding:7px 10px;border-top:1px solid #1a0000;display:grid;grid-template-columns:auto 1fr auto;gap:2px 10px;align-items:center}
.w .b{justify-self:start}
.w .msg{grid-column:1/-1;color:var(--dark);overflow-wrap:anywhere}
summary .dim{white-space:nowrap}
.w .k{font-weight:bold}.w .st{color:var(--dark);text-align:right;white-space:nowrap}
h2{font-size:13px;letter-spacing:.2em;margin:22px 0 6px;color:var(--red)}
table{width:100%;border-collapse:collapse;table-layout:fixed}
td{padding:5px 6px;border-top:1px solid #1a0000;vertical-align:top;overflow-wrap:anywhere}
td:first-child{width:92px}td:last-child{width:70px}
footer{color:var(--grey);font-size:12px;text-align:center;padding:16px}
@media (max-width:560px){.w{grid-template-columns:auto 1fr}.w .st{grid-column:1/-1;text-align:left}
.hide-s{display:none}td:first-child{width:80px}}
"""


def render_html(data: dict[str, Any], cfg: StatusPageConfig) -> str:
    e = html.escape
    now = data["generated"]

    def badge(status: str) -> str:
        return f'<span class="b {e(status)}">{e(status)}</span>'

    def pct(v) -> str:
        return f"{v:.2f}%" if v is not None else "-"

    out = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        '<meta name=viewport content="width=device-width,initial-scale=1">',
        f'<meta http-equiv=refresh content="{int(cfg.refresh)}">',
        '<meta name=robots content="noindex,nofollow">',
        f"<title>{e(data['overall'])} · {e(cfg.title)}</title><style>{CSS}</style></head>",
        f'<body class="{"alerting" if data["overall"] == "ALERT" else ""}"><header>',
        f"<h1>{e(cfg.title.upper())}</h1>{badge(data['overall'])}",
    ]
    for s in ("ALERT", "WARN", "BLOCKED", "OK", "SLEEPING"):
        n = data["counts"].get(s, 0)
        if n:
            out.append(f'<span class="dim">{n} {e(s)}</span>')
    if not data["daemon"]["running"]:
        out.append('<span class="b ALERT">DAEMON DOWN</span>')
    if data["autofix"] != "on":
        out.append(f'<span class="muted">autofix {e(data["autofix"])}</span>')
    out.append("</header><main>")

    if data["incidents"]["open"]:
        out.append("<h2>OPEN INCIDENTS</h2><table>")
        for i in data["incidents"]["open"]:
            out.append(f"<tr><td>{badge(i['status'])}</td><td><b>{e(i['key'])}</b><br>"
                       f"<span class=dim>{e(i['message'])}</span></td>"
                       f"<td class=muted>{e(fmt_age(i['duration_s']))}{' · escalated' if i['escalated'] else ''}</td></tr>")
        out.append("</table>")

    out.append("<h2>PROJECTS</h2>")
    for p in data["projects"]:
        opened = " open" if p["status"] in ("ALERT", "WARN", "BLOCKED") else ""
        out.append(f"<details{opened}><summary>{badge(p['status'])}<span class=name>{e(p['name'])}</span>"
                   f"<span class=dim>24h {pct(p['uptime_24h'])}</span>"
                   f"<span class='dim hide-s'>7d {pct(p['uptime_7d'])}</span></summary>")
        for w in p["watchers"]:
            age = fmt_age(now - w["last_check"]) + " ago" if w["last_check"] else "never"
            flags = " ".join(f for f, on in (("muted", w["muted"]), ("disabled", w["disabled"]),
                                             ("FLAPPING", w["flapping"])) if on)
            lat = f" · {w['latency_ms']:.0f}ms" if w["latency_ms"] is not None else ""
            out.append(
                f"<div class=w>{badge(w['status'])}<span class=k>{e(w['name'])} "
                f"<span class=muted>{e(w['type'])} {e(flags)}</span></span>"
                f"<span class=st>{e(age)}{e(lat)} · 24h {pct(w['uptime_24h'])} · 7d {pct(w['uptime_7d'])}</span>"
                f"<span class=msg>{e(w['message'])}"
                + (f"<br><span class=muted>{e(w['budget'])}</span>" if w.get("budget") else "")
                + "</span></div>")
        out.append("</details>")

    if data["incidents"]["recent"]:
        out.append("<h2>RECENT INCIDENTS</h2><table>")
        for i in data["incidents"]["recent"]:
            when = dt.datetime.fromtimestamp(i["opened"]).strftime("%m-%d %H:%M")
            out.append(f"<tr><td class=muted>{e(when)}</td><td><b>{e(i['key'])}</b> "
                       f"<span class=dim>{e(i['message'])}</span></td>"
                       f"<td class=muted>{e(fmt_age(i['duration_s']))}</td></tr>")
        out.append("</table>")
    out.append(f"<footer>read-only · refreshes every {int(cfg.refresh)}s · "
               f"{e(dt.datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S'))} · "
               f"<a class=dim href=/api/status>json</a></footer></main></body></html>")
    return "".join(out)


# ------------------------------------------------------------------- server
class StatusServer:
    def __init__(self, daemon, cfg: StatusPageConfig):
        self.daemon = daemon
        self.cfg = cfg
        self.server: asyncio.AbstractServer | None = None
        self.bound: tuple[str, int] | None = None
        self._cache: tuple[float, dict] | None = None

    async def start(self) -> None:
        host = resolve_host(self.cfg.host)
        self.server = await asyncio.start_server(self._handle, host, self.cfg.port, limit=MAX_LINE * 2)
        sock = self.server.sockets[0].getsockname()
        self.bound = (sock[0], sock[1])
        log.info("status page on http://%s:%s/ (token %s)", sock[0], sock[1],
                 "required" if self.cfg.token else "not set")

    def close(self) -> None:
        if self.server:
            self.server.close()

    @property
    def url(self) -> str | None:
        if not self.bound:
            return None
        host = "127.0.0.1" if self.bound[0] in ("0.0.0.0", "::") else self.bound[0]
        return f"http://{host}:{self.bound[1]}/"

    def _data(self) -> dict:
        now = time.monotonic()
        if self._cache and now - self._cache[0] < 2:
            return self._cache[1]
        data = build_status(self.daemon)
        self._cache = (now, data)
        return data

    def _authorized(self, headers: dict[str, str], query: dict[str, list[str]]) -> tuple[bool, bool]:
        """(authorized, via_query)"""
        token = self.cfg.token
        if not token:
            return True, False
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer ") and hmac.compare_digest(auth[7:].strip(), token):
            return True, False
        for part in headers.get("cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and hmac.compare_digest(v, token):
                return True, False
        q = (query.get("token") or [""])[0]
        if q and hmac.compare_digest(q, token):
            return True, True
        return False, False

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(self._serve_one(reader, writer), timeout=10)
        except Exception as e:  # never let a client take anything down
            log.debug("status page request failed: %s", e)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _serve_one(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        if len(line) > MAX_LINE or not line.endswith(b"\n"):
            return await self._send(writer, 414, "text/plain", "request line too long\n")
        headers: dict[str, str] = {}
        for _ in range(MAX_HEADERS + 1):
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""):
                break
            if len(headers) >= MAX_HEADERS or len(h) > MAX_LINE:
                return await self._send(writer, 431, "text/plain", "headers too large\n")
            k, _, v = h.decode("latin-1").partition(":")
            headers[k.strip().lower()] = v.strip()
        parts = line.decode("latin-1").split()
        if len(parts) != 3:
            return await self._send(writer, 400, "text/plain", "bad request\n")
        method, target, _ = parts
        head = method == "HEAD"
        if method not in ("GET", "HEAD"):
            return await self._send(writer, 405, "text/plain", "read-only: GET only\n", {"Allow": "GET, HEAD"})
        url = urlsplit(target)
        path, query = unquote(url.path), parse_qs(url.query)
        if path == "/healthz":
            return await self._send(writer, 200, "application/json", '{"ok": true}\n', head=head)
        ok, via_query = self._authorized(headers, query)
        if not ok:
            return await self._send(writer, 401, "text/plain", "unauthorized\n",
                                    {"WWW-Authenticate": 'Bearer realm="kwatchdog"'}, head=head)
        if via_query:  # swap the URL token for a cookie, then drop it from the address bar
            return await self._send(writer, 303, "text/plain", "", {
                "Location": path or "/",
                "Set-Cookie": f"{COOKIE}={self.cfg.token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"},
                head=head)
        if path in ("/", "/index.html"):
            return await self._send(writer, 200, "text/html; charset=utf-8", render_html(self._data(), self.cfg),
                                    head=head)
        if path == "/api/status":
            return await self._send_json(writer, self._data(), head)
        if path == "/api/incidents":
            return await self._send_json(writer, self._data()["incidents"], head)
        if path.startswith("/api/watchers/"):
            key = path[len("/api/watchers/"):].strip("/")
            for p in self._data()["projects"]:
                for w in p["watchers"]:
                    if w["key"] == key:
                        hist = [{"ts": r.ts, "status": r.status.value, "latency_ms": r.latency_ms,
                                 "message": r.message[:300]} for r in self.daemon.store.results(key, 50)]
                        return await self._send_json(writer, {**w, "results": hist}, head)
        return await self._send(writer, 404, "text/plain", "not found\n", head=head)

    async def _send_json(self, writer, obj, head: bool) -> None:
        await self._send(writer, 200, "application/json", json.dumps(obj, indent=1) + "\n", head=head)

    async def _send(self, writer, code: int, ctype: str, body: str, extra: dict | None = None,
                    head: bool = False) -> None:
        reasons = {200: "OK", 303: "See Other", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
                   405: "Method Not Allowed", 414: "URI Too Long", 431: "Request Header Fields Too Large"}
        data = body.encode("utf-8")
        hdrs = {"Content-Type": ctype, "Content-Length": str(len(data)), "Connection": "close",
                **SECURITY_HEADERS, **(extra or {})}
        writer.write((f"HTTP/1.1 {code} {reasons.get(code, 'OK')}\r\n"
                      + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n").encode("latin-1"))
        if not head:
            writer.write(data)
        await writer.drain()
