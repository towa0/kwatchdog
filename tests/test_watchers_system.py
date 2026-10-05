import os
import shutil
import sys
from collections import namedtuple

import psutil
import pytest

from kwatchdog.core.models import Status
from kwatchdog.watchers.system import decode_throttled, systemd_result


async def test_process_by_name(make):
    me = psutil.Process().name()
    r = await make("process", process=me).check()
    assert r.status == Status.OK and r.metrics["count"] >= 1
    r = await make("process", process="no-such-process-kw").check()
    assert r.status == Status.ALERT


async def test_process_cmdline_and_min_count(make):
    r = await make("process", cmdline="pytest").check()
    assert r.status == Status.OK
    r = await make("process", process=psutil.Process().name(), min_count=100000).check()
    assert r.status == Status.ALERT and "need 100000" in r.message


async def test_process_pid_file(make, tmp_path):
    pf = tmp_path / "app.pid"
    pf.write_text(str(os.getpid()))
    assert (await make("process", pid_file=str(pf)).check()).status == Status.OK
    pf.write_text("999999999")
    assert (await make("process", pid_file=str(pf)).check()).status == Status.ALERT
    assert (await make("process", pid_file=str(tmp_path / "missing")).check()).status == Status.ALERT


def test_process_config_requires_target(make):
    with pytest.raises(Exception, match="set one of"):
        make("process")


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="windows services")
async def test_windows_service(make):
    r = await make("process", service="EventLog").check()
    assert r.status == Status.OK
    assert (await make("process", service="NoSuchServiceKW").check()).status == Status.ALERT


def test_systemd_result():
    assert systemd_result("x", {"ActiveState": "active", "SubState": "running"}).status == Status.OK
    assert systemd_result("x", {"ActiveState": "activating"}).status == Status.WARN
    r = systemd_result("x", {"ActiveState": "failed", "SubState": "failed", "Result": "exit-code", "NRestarts": "4"})
    assert r.status == Status.ALERT and "exit-code" in r.message and r.metrics["restarts"] == 4
    assert systemd_result("x", {"LoadState": "not-found"}).status == Status.ALERT


Usage = namedtuple("Usage", "total used free")


@pytest.mark.parametrize("used,status", [(50, Status.OK), (90, Status.WARN), (97, Status.ALERT)])
async def test_disk(make, monkeypatch, used, status):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(100 * 1024**3, used * 1024**3, (100 - used) * 1024**3))
    r = await make("disk", path=".").check()
    assert r.status == status and r.metrics["used_percent"] == used


async def test_disk_min_free(make, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(100 * 1024**3, 50 * 1024**3, 50 * 1024**3))
    assert (await make("disk", path=".", min_free_gb=60).check()).status == Status.ALERT


async def test_disk_real(make):
    r = await make("disk").check()
    assert r.status in (Status.OK, Status.WARN, Status.ALERT) and "free" in r.message


async def test_system(make, monkeypatch):
    VM = namedtuple("VM", "percent")
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 99.0)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: VM(40.0))
    monkeypatch.setattr("kwatchdog.watchers.system.read_temperature", lambda: 72.0)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    r = await make("system").check()
    assert r.status == Status.ALERT
    assert r.metrics == {"cpu_percent": 99.0, "ram_percent": 40.0, "temp_c": 72.0}
    assert "cpu 99% > 97%" in r.message and "temp 72C > 70C" in r.message


def test_pi_throttle_bits():
    assert decode_throttled(0x0) == (Status.OK, [])
    st, flags = decode_throttled(0x50000)
    assert st == Status.WARN and "under-voltage occurred" in flags
    st, flags = decode_throttled(0x50005)
    assert st == Status.ALERT and "under-voltage NOW" in flags and "throttled NOW" in flags
