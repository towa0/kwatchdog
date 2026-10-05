import asyncio
import textwrap

import httpx
import pytest

from kwatchdog.core import web
from kwatchdog.core.config import load_config
from kwatchdog.core.daemon import Daemon
from kwatchdog.core.storage import Store

from .test_daemon import SCRIPT, regs  # noqa: F401

TOKEN = "s3cret-status-token"


@pytest.fixture
def make(tmp_path, regs):  # noqa: F811
    async def _make(status_page: str):
        p = tmp_path / "c.yaml"
        p.write_text(textwrap.dedent(f"""
            settings: {{heartbeat_port: null}}
            status_page: {status_page}
            alerts: {{default: {{min_failures: 1, cooldown: 0}}}}
            projects:
              web:
                watchers:
                  - {{name: api, type: fake, interval: 3600}}
                  - {{name: site, type: fake, interval: 3600}}
        """))
        d = Daemon(p, store=Store(tmp_path / "w.db"), watcher_registry=regs[0], channel_registry=regs[1])
        d.first_run_delay = 3600  # tests drive checks manually
        await d.start()
        return d
    return _make


async def test_page_and_api_without_token(make):
    d = await make("{port: 0}")
    try:
        assert d.status_server.bound[0] == "127.0.0.1"  # loopback by default
        SCRIPT["api"] = ["ALERT"]
        d.watchers["web/api"].config.note = "<script>alert(1)</script>"
        await d.check_once("web/api")
        await d.check_once("web/site")
        base = d.status_server.url
        async with httpx.AsyncClient() as c:
            r = await c.get(base)
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
            assert "default-src 'none'" in r.headers["content-security-policy"]
            assert "<script>" not in r.text and "&lt;script&gt;" in r.text  # escaped
            assert "web" in r.text and "OPEN INCIDENTS" in r.text and 'class="alerting"' in r.text
            data = (await c.get(base + "api/status")).json()
            assert data["overall"] == "ALERT" and data["counts"]["ALERT"] == 1
            proj = data["projects"][0]
            assert proj["name"] == "web" and proj["uptime_24h"] == 50.0
            assert {w["key"] for w in proj["watchers"]} == {"web/api", "web/site"}
            assert "raw" not in proj["watchers"][0]  # raw output never exposed
            assert len((await c.get(base + "api/incidents")).json()["open"]) == 1
            w = (await c.get(base + "api/watchers/web/api")).json()
            assert w["status"] == "ALERT" and len(w["results"]) == 1
            assert (await c.get(base + "api/watchers/web/nope")).status_code == 404
            assert (await c.get(base + "../../etc/passwd")).status_code == 404
            assert (await c.get(base + "healthz")).json() == {"ok": True}
    finally:
        await d.stop()


async def test_read_only(make):
    d = await make("{port: 0}")
    try:
        async with httpx.AsyncClient() as c:
            for method in ("POST", "PUT", "DELETE", "PATCH"):
                r = await c.request(method, d.status_server.url + "api/status")
                assert r.status_code == 405 and r.headers["allow"] == "GET, HEAD"
            r = await c.head(d.status_server.url)
            assert r.status_code == 200 and r.content == b""
    finally:
        await d.stop()


async def test_token_header_cookie_and_query(make):
    d = await make(f"{{port: 0, token: {TOKEN}}}")
    try:
        base = d.status_server.url
        async with httpx.AsyncClient() as c:
            assert (await c.get(base + "api/status")).status_code == 401
            assert (await c.get(base, headers={"Authorization": "Bearer wrong"})).status_code == 401
            ok = await c.get(base + "api/status", headers={"Authorization": f"Bearer {TOKEN}"})
            assert ok.status_code == 200
            assert (await c.get(base + "healthz")).status_code == 200  # liveness needs no token
            r = await c.get(base + f"?token={TOKEN}")
            assert r.status_code == 303 and r.headers["location"] == "/"
            cookie = r.headers["set-cookie"]
            assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
            r2 = await c.get(base, headers={"Cookie": f"kw_token={TOKEN}"})
            assert r2.status_code == 200
            assert TOKEN not in r2.text
    finally:
        await d.stop()


async def test_oversized_request_rejected(make):
    d = await make("{port: 0}")
    try:
        host, port = d.status_server.bound
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(b"GET /" + b"a" * 20000 + b" HTTP/1.1\r\n\r\n")
        await writer.drain()
        resp = await asyncio.wait_for(reader.read(200), 5)
        writer.close()
        assert resp.startswith(b"HTTP/1.1 414") or resp == b""
    finally:
        await d.stop()


def test_lan_without_token_warns(regs):  # noqa: F811
    cfg = load_config(None, *regs, text="status_page: {host: lan}\n")
    assert any("no token" in w for w in cfg.warnings)
    cfg = load_config(None, *regs, text="status_page: {host: 127.0.0.1}\n")
    assert not any("no token" in w for w in cfg.warnings)


def test_token_from_env_and_missing_env_disables(regs, monkeypatch):  # noqa: F811
    monkeypatch.setenv("KW_STATUS_TOKEN", "from-env-token")
    cfg = load_config(None, *regs, text="status_page: {token: '${KW_STATUS_TOKEN}'}\n")
    assert cfg.status_page.token == "from-env-token" and cfg.status_page.enabled
    monkeypatch.delenv("KW_STATUS_TOKEN")
    cfg = load_config(None, *regs, text="status_page: {token: '${KW_STATUS_TOKEN}'}\n")
    assert not cfg.status_page.enabled and any("status page disabled" in e for e in cfg.errors)


def test_tailscale_resolution(monkeypatch):
    monkeypatch.setattr(web.shutil, "which", lambda n: None)
    with pytest.raises(OSError, match="isn't installed"):
        web.resolve_host("tailscale")

    class Out:
        returncode, stdout, stderr = 0, "100.101.102.103\n", ""
    monkeypatch.setattr(web.shutil, "which", lambda n: "/usr/bin/tailscale")
    monkeypatch.setattr(web.subprocess, "run", lambda *a, **k: Out())
    assert web.resolve_host("tailscale") == "100.101.102.103"
    Out.returncode, Out.stdout, Out.stderr = 1, "", "not logged in"
    with pytest.raises(OSError, match="not logged in"):
        web.resolve_host("tailscale")
    assert web.resolve_host("lan") == "0.0.0.0"


async def test_tailscale_failure_never_falls_back(make, monkeypatch):
    monkeypatch.setattr(web.shutil, "which", lambda n: None)
    d = await make("{host: tailscale, port: 0, token: x}")
    try:
        assert d.status_server is None
        assert any("status page not started" in e for e in d.store.kv_get("config_errors"))
    finally:
        await d.stop()
