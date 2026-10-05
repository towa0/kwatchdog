import json

import httpx
import pytest

from kwatchdog.channels import builtin
from kwatchdog.core.plugin import Notification

N = Notification(kind="alert", project="web", watcher="api", status="ALERT", message="down", ts=0)


@pytest.fixture
def captured(monkeypatch):
    reqs = []
    real = httpx.AsyncClient

    def handler(req):
        reqs.append(req)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(builtin.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return reqs


async def test_telegram(captured):
    ch = builtin.TelegramChannel("tg", builtin.TelegramConfig(token="T0K", chat_id="42"))
    await ch.send(N)
    req = captured[0]
    assert req.url.path == "/botT0K/sendMessage"
    body = json.loads(req.content)
    assert body["chat_id"] == "42" and "[ALERT] web/api: down" in body["text"]


async def test_ntfy(captured):
    ch = builtin.NtfyChannel("n", builtin.NtfyConfig(topic="kw", token="abc"))
    await ch.send(N)
    req = captured[0]
    assert str(req.url) == "https://ntfy.sh/kw"
    assert req.headers["Priority"] == "urgent" and req.headers["Authorization"] == "Bearer abc"


async def test_discord(captured):
    ch = builtin.DiscordChannel("d", builtin.DiscordConfig(webhook_url="https://discord.test/hook"))
    await ch.send(N.model_copy(update={"kind": "recovery", "status": "OK"}))
    body = json.loads(captured[0].content)
    assert body["embeds"][0]["title"] == "[RECOVERED] web/api"


async def test_http_error_raises(monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(builtin.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(500)), **kw))
    with pytest.raises(httpx.HTTPStatusError):
        await builtin.NtfyChannel("n", builtin.NtfyConfig(topic="kw")).send(N)


async def test_email(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def starttls(self, context=None):
            sent["tls"] = True

        def login(self, u, p):
            sent["login"] = (u, p)

        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr(builtin.smtplib, "SMTP", FakeSMTP)
    ch = builtin.EmailChannel("e", builtin.EmailConfig(host="smtp.x", sender="a@x", to=["b@x"],
                                                       username="u", password="p"))
    await ch.send(N)
    assert sent["tls"] and sent["login"] == ("u", "p")
    assert sent["msg"]["Subject"] == "[ALERT] web/api" and sent["msg"]["To"] == "b@x"


async def test_bell_hook():
    got = []
    builtin.BellChannel.hook = got.append
    try:
        await builtin.BellChannel("b", builtin.BellConfig()).send(N)
    finally:
        builtin.BellChannel.hook = None
    assert got == [N]
