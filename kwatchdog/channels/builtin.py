"""Built-in channels: terminal bell, desktop toast, Telegram, ntfy.sh, Discord, email."""
from __future__ import annotations

import asyncio
import shutil
import smtplib
import ssl
import subprocess
import sys
from email.message import EmailMessage
from typing import ClassVar

import httpx
from pydantic import Field

from ..core.plugin import Channel, ChannelConfig, Notification

_TIMEOUT = 15.0


class BellConfig(ChannelConfig):
    times: int = Field(1, ge=1, le=5)


class BellChannel(Channel):
    """Terminal bell. In the TUI the app also flashes; in a headless daemon it
    writes BEL to stdout (works over SSH)."""

    type = "bell"
    description = "terminal bell"
    Config = BellConfig
    hook: ClassVar = None  # set by the TUI when embedded so the app can ring/flash

    async def send(self, n: Notification) -> None:
        if BellChannel.hook is not None:
            BellChannel.hook(n)
            return
        if sys.stdout and sys.stdout.isatty():
            sys.stdout.write("\a" * self.config.times)
            sys.stdout.flush()


class DesktopConfig(ChannelConfig):
    app_name: str = "kwatchdog"


class DesktopChannel(Channel):
    """Desktop toast: plyer if installed, else notify-send (Linux) or a
    PowerShell toast (Windows)."""

    type = "desktop"
    description = "desktop notification (toast)"
    Config = DesktopConfig

    @classmethod
    def unavailable_reason(cls) -> str | None:
        try:
            import plyer  # noqa: F401
            return None
        except ImportError:
            pass
        if sys.platform.startswith("win"):
            return None if shutil.which("powershell") else "powershell not found (or pip install plyer)"
        if shutil.which("notify-send"):
            return None
        return "no notifier: install libnotify-bin (notify-send) or pip install plyer"

    async def send(self, n: Notification) -> None:
        await asyncio.to_thread(self._send_sync, n.title, n.message)

    def _send_sync(self, title: str, body: str) -> None:
        try:
            from plyer import notification

            notification.notify(title=title, message=body[:250], app_name=self.config.app_name, timeout=10)
            return
        except ImportError:
            pass
        if sys.platform.startswith("win"):
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _ps_toast(title, body)],
                           capture_output=True, timeout=_TIMEOUT, check=False)
        else:
            subprocess.run(["notify-send", "-a", self.config.app_name, title, body[:250]],
                           capture_output=True, timeout=_TIMEOUT, check=False)


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _ps_toast(title: str, body: str) -> str:
    return (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        f"$x=$t.GetElementsByTagName('text');$x.Item(0).AppendChild($t.CreateTextNode({_ps_quote(title)}))|Out-Null;"
        f"$x.Item(1).AppendChild($t.CreateTextNode({_ps_quote(body[:250])}))|Out-Null;"
        "$n=[Windows.UI.Notifications.ToastNotification]::new($t);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('kwatchdog').Show($n)"
    )


class TelegramConfig(ChannelConfig):
    token: str  # ${TELEGRAM_TOKEN}
    chat_id: str
    api_base: str = "https://api.telegram.org"


class TelegramChannel(Channel):
    type = "telegram"
    description = "Telegram bot message"
    Config = TelegramConfig

    async def send(self, n: Notification) -> None:
        c = self.config
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(f"{c.api_base}/bot{c.token}/sendMessage",
                                  json={"chat_id": c.chat_id, "text": n.text, "disable_web_page_preview": True})
            r.raise_for_status()


class NtfyConfig(ChannelConfig):
    topic: str
    server: str = "https://ntfy.sh"
    token: str | None = None  # ${NTFY_TOKEN} for protected topics


class NtfyChannel(Channel):
    type = "ntfy"
    description = "ntfy.sh push"
    Config = NtfyConfig

    PRIORITY = {"ALERT": "urgent", "WARN": "high", "OK": "default"}

    async def send(self, n: Notification) -> None:
        c = self.config
        headers = {"Title": n.title.encode("ascii", "replace").decode(),
                   "Priority": "urgent" if n.kind == "escalation" else self.PRIORITY.get(n.status, "default"),
                   "Tags": "rotating_light" if n.status == "ALERT" else ("white_check_mark" if n.kind == "recovery" else "warning")}
        if c.token:
            headers["Authorization"] = f"Bearer {c.token}"
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(f"{c.server.rstrip('/')}/{c.topic}", content=n.text.encode(), headers=headers)
            r.raise_for_status()


class DiscordConfig(ChannelConfig):
    webhook_url: str  # ${DISCORD_WEBHOOK}
    username: str = "kwatchdog"


class DiscordChannel(Channel):
    type = "discord"
    description = "Discord webhook"
    Config = DiscordConfig

    COLORS = {"ALERT": 0xFF1A1A, "WARN": 0x8B0000, "OK": 0x555555}

    async def send(self, n: Notification) -> None:
        payload = {"username": self.config.username,
                   "embeds": [{"title": n.title, "description": n.message[:4000],
                               "color": self.COLORS.get(n.status, 0x8B0000)}]}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(self.config.webhook_url, json=payload)
            r.raise_for_status()


class EmailConfig(ChannelConfig):
    host: str
    port: int = 587
    username: str | None = None
    password: str | None = None  # ${SMTP_PASSWORD}
    sender: str
    to: list[str]
    starttls: bool = True
    ssl: bool = False


class EmailChannel(Channel):
    type = "email"
    description = "SMTP email"
    Config = EmailConfig

    async def send(self, n: Notification) -> None:
        await asyncio.to_thread(self._send_sync, n)

    def _send_sync(self, n: Notification) -> None:
        c = self.config
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = n.title, c.sender, ", ".join(c.to)
        msg.set_content(n.text)
        ctx = ssl.create_default_context()
        if c.ssl:
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(c.host, c.port, timeout=_TIMEOUT, context=ctx)
        else:
            smtp = smtplib.SMTP(c.host, c.port, timeout=_TIMEOUT)
        with smtp:
            if c.starttls and not c.ssl:
                smtp.starttls(context=ctx)
            if c.username:
                smtp.login(c.username, c.password or "")
            smtp.send_message(msg)
