import asyncio

import httpx
import pytest

from kwatchdog.core.models import Status
from kwatchdog.watchers.network import HttpWatcher, parse_ping

from .conftest import Ctx


def responder(routes):
    def handler(req: httpx.Request):
        r = routes.get(str(req.url))
        if r is None:
            return httpx.Response(404, text="nope")
        if isinstance(r, Exception):
            raise r
        return r
    return handler


async def test_http_ok(make):
    w = make("http", Ctx(responder({"https://x.test/": httpx.Response(200, text="hello world")})),
             url="https://x.test/", check_ssl=False, body_regex="hello")
    r = await w.check()
    assert r.status == Status.OK, r.message
    assert r.metrics["status_code"] == 200 and r.latency_ms is not None


async def test_http_bad_status_and_body(make):
    w = make("http", Ctx(responder({"https://x.test/": httpx.Response(503, text="Maintenance")})),
             url="https://x.test/", check_ssl=False, body_regex="hello", body_not_regex="Maint\\w+")
    r = await w.check()
    assert r.status == Status.ALERT
    assert "HTTP 503" in r.message and "does not match" in r.message and "Maintenance" in r.message


async def test_http_expect_status_list(make):
    w = make("http", Ctx(responder({"http://x.test/": httpx.Response(401)})),
             url="http://x.test/", expect_status=[200, 401])
    assert (await w.check()).status == Status.OK


async def test_http_redirect_chain(make):
    routes = {
        "http://x.test/a": httpx.Response(301, headers={"Location": "http://x.test/b"}),
        "http://x.test/b": httpx.Response(302, headers={"Location": "http://x.test/c"}),
        "http://x.test/c": httpx.Response(200, text="done"),
    }
    w = make("http", Ctx(responder(routes)), url="http://x.test/a", expect_final_url="http://x.test/c")
    r = await w.check()
    assert r.status == Status.OK and r.metrics["redirects"] == 2
    assert "http://x.test/a -> http://x.test/b -> http://x.test/c" in r.raw

    w = make("http", Ctx(responder(routes)), url="http://x.test/a", max_redirects=1)
    r = await w.check()
    assert r.status == Status.ALERT and "too many redirects" in r.message

    w = make("http", Ctx(responder(routes)), url="http://x.test/a", expect_final_url="http://x.test/z")
    assert (await w.check()).status == Status.WARN


async def test_http_timeout_and_connect_error(make):
    w = make("http", Ctx(responder({"http://x.test/": httpx.ReadTimeout("slow")})), url="http://x.test/")
    r = await w.check()
    assert r.status == Status.ALERT and "timeout" in r.message
    w = make("http", Ctx(responder({"http://x.test/": httpx.ConnectError("refused")})), url="http://x.test/")
    r = await w.check()
    assert r.status == Status.ALERT and "ConnectError" in r.message


async def test_http_latency_threshold(make):
    w = make("http", Ctx(responder({"http://x.test/": httpx.Response(200)})), url="http://x.test/",
             latency_warn_ms=0.000001)
    r = await w.check()
    assert r.status == Status.WARN and "slow" in r.message


@pytest.mark.parametrize("days,expected", [(60, Status.OK), (10, Status.WARN), (1, Status.ALERT)])
async def test_http_ssl_expiry(make, monkeypatch, days, expected):
    async def fake(self):
        return days
    monkeypatch.setattr(HttpWatcher, "_cert_days_left", fake)
    w = make("http", Ctx(responder({"https://x.test/": httpx.Response(200)})), url="https://x.test/")
    r = await w.check()
    assert r.status == expected and r.metrics["ssl_days_left"] == days


async def test_port_open_and_closed(make):
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        r = await make("port", host="127.0.0.1", port=port).check()
        assert r.status == Status.OK and "open" in r.message
    finally:
        server.close()
        await server.wait_closed()
    r = await make("port", host="127.0.0.1", port=port, timeout=2).check()
    assert r.status == Status.ALERT


WIN_EN = """
Pinging 1.1.1.1 with 32 bytes of data:
Reply from 1.1.1.1: bytes=32 time=12ms TTL=57
Reply from 1.1.1.1: bytes=32 time<1ms TTL=57
Request timed out.

Ping statistics for 1.1.1.1:
    Packets: Sent = 3, Received = 2, Lost = 1 (33% loss),
Approximate round trip times in milli-seconds:
    Minimum = 0ms, Maximum = 12ms, Average = 6ms
"""
WIN_NL = """
Antwoord van 1.1.1.1: bytes=32 tijd=15ms TTL=57
Antwoord van 1.1.1.1: bytes=32 tijd=16ms TTL=57
"""
LINUX = """
PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.
64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=11.3 ms
64 bytes from 1.1.1.1: icmp_seq=2 ttl=57 time=10.9 ms

--- 1.1.1.1 ping statistics ---
2 packets transmitted, 2 received, 0% packet loss, time 1001ms
rtt min/avg/max/mdev = 10.9/11.1/11.3/0.2 ms
"""
WIN_UNREACH = "Reply from 192.168.1.1: Destination host unreachable.\n"


def test_parse_ping_locales():
    assert parse_ping(WIN_EN) == [12.0, 1.0]
    assert parse_ping(WIN_NL) == [15.0, 16.0]
    assert parse_ping(LINUX) == [11.3, 10.9]
    assert parse_ping(WIN_UNREACH) == []


class FakeProc:
    def __init__(self, out):
        self.out = out
        self.returncode = 0

    async def communicate(self):
        return self.out.encode(), b""


@pytest.mark.parametrize("out,status", [(LINUX, Status.OK), (WIN_EN, Status.OK), (WIN_UNREACH, Status.ALERT)])
async def test_ping_check(make, monkeypatch, out, status):
    async def fake_exec(*a, **k):
        return FakeProc(out)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    w = make("ping", host="1.1.1.1", count=3, loss_warn_percent=50)
    r = await w.check()
    assert r.status == status, r.message


async def test_ping_loss_warn(make, monkeypatch):
    async def fake_exec(*a, **k):
        return FakeProc(WIN_EN)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    r = await make("ping", host="1.1.1.1", count=3).check()  # 1/3 lost >= 34%? no: 33.3%
    assert r.status == Status.OK
    r = await make("ping", host="1.1.1.1", count=3, loss_warn_percent=30).check()
    assert r.status == Status.WARN and r.metrics["loss_percent"] == pytest.approx(33.3, 0.1)
