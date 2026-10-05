"""Network watchers: http/uptime, port/TCP, ping."""
from __future__ import annotations

import asyncio
import re
import ssl
import sys
import time
from urllib.parse import urlparse

import httpx
from pydantic import Field, field_validator

from ..core.models import Result, Status
from ..core.plugin import Watcher, WatcherConfig


class HttpConfig(WatcherConfig):
    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    expect_status: list[int] = Field(default_factory=lambda: [200])
    body_regex: str | None = None  # must match
    body_not_regex: str | None = None  # must NOT match
    latency_warn_ms: float | None = None
    latency_alert_ms: float | None = None
    follow_redirects: bool = True
    max_redirects: int = 5
    expect_final_url: str | None = None
    verify_tls: bool = True
    check_ssl: bool = True
    ssl_warn_days: int = 14
    ssl_alert_days: int = 3

    @field_validator("expect_status", mode="before")
    @classmethod
    def _status(cls, v):
        return [v] if isinstance(v, int) else v

    @field_validator("body_regex", "body_not_regex")
    @classmethod
    def _re(cls, v):
        if v:
            re.compile(v)
        return v


class HttpWatcher(Watcher):
    type = "http"
    description = "HTTP(S) uptime: status, latency, body regex, SSL expiry, redirects"
    Config = HttpConfig
    default_interval = 60

    async def check(self) -> Result:
        c: HttpConfig = self.config
        client = self.ctx.http()
        t0 = time.perf_counter()
        try:
            r = await client.request(c.method, c.url, headers=c.headers, timeout=self.timeout,
                                     follow_redirects=False)
            chain = [str(r.url)]
            while c.follow_redirects and r.is_redirect and len(chain) <= c.max_redirects + 1:
                nxt = r.next_request
                if nxt is None:
                    break
                r = await client.send(nxt, follow_redirects=False)
                chain.append(str(r.url))
        except httpx.TimeoutException:
            return Result.alert(f"timeout after {self.timeout:g}s", latency_ms=(time.perf_counter() - t0) * 1000)
        except httpx.HTTPError as e:
            return Result.alert(f"request failed: {type(e).__name__}: {e}")
        latency = (time.perf_counter() - t0) * 1000
        metrics = {"latency_ms": round(latency, 1), "status_code": r.status_code, "redirects": len(chain) - 1}
        raw = f"{c.method} {' -> '.join(chain)}\nHTTP {r.status_code}\n" + "\n".join(
            f"{k}: {v}" for k, v in r.headers.items()) + "\n\n" + r.text[:2000]
        problems: list[tuple[Status, str]] = []

        if len(chain) - 1 > c.max_redirects or (r.is_redirect and c.follow_redirects):
            problems.append((Status.ALERT, f"too many redirects ({len(chain) - 1} > {c.max_redirects})"))
        if r.status_code not in c.expect_status:
            problems.append((Status.ALERT, f"HTTP {r.status_code} (expected {', '.join(map(str, c.expect_status))})"))
        if c.expect_final_url and str(r.url).rstrip("/") != c.expect_final_url.rstrip("/"):
            problems.append((Status.WARN, f"ended at {r.url} (expected {c.expect_final_url})"))
        body = r.text
        if c.body_regex and not re.search(c.body_regex, body):
            problems.append((Status.ALERT, f"body does not match /{c.body_regex}/"))
        if c.body_not_regex and (m := re.search(c.body_not_regex, body)):
            problems.append((Status.ALERT, f"body matches /{c.body_not_regex}/: {m.group(0)[:60]!r}"))
        if c.latency_alert_ms and latency > c.latency_alert_ms:
            problems.append((Status.ALERT, f"slow: {latency:.0f}ms > {c.latency_alert_ms:g}ms"))
        elif c.latency_warn_ms and latency > c.latency_warn_ms:
            problems.append((Status.WARN, f"slow: {latency:.0f}ms > {c.latency_warn_ms:g}ms"))

        if c.check_ssl and c.url.startswith("https://"):
            try:
                days = await self._cert_days_left()
                metrics["ssl_days_left"] = round(days, 1)
                if days < c.ssl_alert_days:
                    problems.append((Status.ALERT, f"SSL cert expires in {days:.1f} days"))
                elif days < c.ssl_warn_days:
                    problems.append((Status.WARN, f"SSL cert expires in {days:.0f} days"))
            except Exception as e:  # cert problems are reported, not fatal
                problems.append((Status.ALERT, f"SSL check failed: {e}"))

        if problems:
            status = Status.worst(p[0] for p in problems)
            return Result(status, "; ".join(p[1] for p in problems), metrics, raw, latency)
        return Result.ok(f"HTTP {r.status_code} in {latency:.0f}ms", metrics=metrics, raw=raw, latency_ms=latency)

    async def _cert_days_left(self) -> float:
        cached = self.ctx.state_get("cert")
        if cached and time.time() - cached["checked"] < 3600:
            return (cached["not_after"] - time.time()) / 86400
        u = urlparse(self.config.url)
        host, port = u.hostname or "", u.port or 443
        ctx = ssl.create_default_context()
        if not self.config.verify_tls:
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=host), self.timeout)
        try:
            cert = writer.get_extra_info("peercert")
            if not cert:  # unverified mode returns {}: decode the binary form
                der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
                not_after = _der_not_after(der)
            else:
                not_after = parsedate_to_datetime(cert["notAfter"].replace("  ", " ")).timestamp() \
                    if "," in cert["notAfter"] else ssl.cert_time_to_seconds(cert["notAfter"])
        finally:
            writer.close()
        self.ctx.state_set("cert", {"checked": time.time(), "not_after": not_after})
        return (not_after - time.time()) / 86400


def _der_not_after(der: bytes) -> float:  # pragma: no cover - only for verify_tls: false
    try:
        from cryptography import x509

        return x509.load_der_x509_certificate(der).not_valid_after_utc.timestamp()
    except ImportError:
        raise RuntimeError("expiry with verify_tls=false needs 'cryptography'") from None


class PortConfig(WatcherConfig):
    host: str
    port: int = Field(ge=1, le=65535)
    latency_warn_ms: float | None = None


class PortWatcher(Watcher):
    type = "port"
    description = "TCP port accepts connections"
    Config = PortConfig
    default_interval = 30

    async def check(self) -> Result:
        c: PortConfig = self.config
        t0 = time.perf_counter()
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(c.host, c.port), self.timeout)
        except asyncio.TimeoutError:
            return Result.alert(f"{c.host}:{c.port} connect timeout")
        except OSError as e:
            return Result.alert(f"{c.host}:{c.port} closed: {e.strerror or e}")
        latency = (time.perf_counter() - t0) * 1000
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        status = Status.WARN if c.latency_warn_ms and latency > c.latency_warn_ms else Status.OK
        return Result(status, f"{c.host}:{c.port} open in {latency:.0f}ms", {"latency_ms": round(latency, 1)},
                      latency_ms=latency)


class PingConfig(WatcherConfig):
    host: str
    count: int = Field(3, ge=1, le=20)
    latency_warn_ms: float | None = None
    latency_alert_ms: float | None = None
    loss_warn_percent: float = 34
    loss_alert_percent: float = 100


_RTT_RE = re.compile(r"[=<]\s*(\d+(?:[.,]\d+)?)\s*ms", re.I)


def parse_ping(output: str) -> list[float]:
    """Extract per-reply RTTs. Locale-agnostic: Windows (any language) and iputils."""
    rtts = []
    for line in output.splitlines():
        low = line.lower()
        if "ttl" not in low:  # reply lines carry TTL; summary lines don't
            continue
        m = _RTT_RE.search(line)
        if m:
            rtts.append(float(m.group(1).replace(",", ".")))
    return rtts


class PingWatcher(Watcher):
    type = "ping"
    description = "ICMP ping (system ping binary): loss + RTT"
    Config = PingConfig
    default_interval = 60

    def _cmd(self) -> list[str]:
        c: PingConfig = self.config
        per = max(1, int(self.timeout / max(c.count, 1)))
        if sys.platform.startswith("win"):
            return ["ping", "-n", str(c.count), "-w", str(per * 1000), c.host]
        return ["ping", "-c", str(c.count), "-W", str(per), "-i", "0.3", c.host]

    async def check(self) -> Result:
        c: PingConfig = self.config
        try:
            proc = await asyncio.create_subprocess_exec(*self._cmd(), stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT)
        except FileNotFoundError:
            return Result.sleeping("ping binary not found")
        try:
            out_b, _ = await asyncio.wait_for(proc.communicate(), self.timeout + 2)
        except asyncio.TimeoutError:
            proc.kill()
            return Result.alert(f"{c.host}: ping timed out")
        out = out_b.decode(errors="replace")
        rtts = parse_ping(out)
        loss = 100.0 * (c.count - len(rtts)) / c.count
        metrics = {"loss_percent": round(loss, 1)}
        if not rtts:
            return Result(Status.ALERT, f"{c.host}: no reply (100% loss)", metrics, out)
        avg = sum(rtts) / len(rtts)
        metrics["latency_ms"] = round(avg, 2)
        status, msgs = Status.OK, [f"{c.host}: {avg:.1f}ms, {loss:.0f}% loss"]
        if loss >= c.loss_alert_percent:
            status = Status.ALERT
        elif loss >= c.loss_warn_percent:
            status = Status.WARN
        if c.latency_alert_ms and avg > c.latency_alert_ms:
            status = Status.ALERT
            msgs.append(f"> {c.latency_alert_ms:g}ms")
        elif c.latency_warn_ms and avg > c.latency_warn_ms:
            status = Status.worst([status, Status.WARN])
            msgs.append(f"> {c.latency_warn_ms:g}ms")
        return Result(status, " ".join(msgs), metrics, out, avg)
