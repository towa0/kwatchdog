import textwrap

import pytest

from kwatchdog.core.models import Result, Status
from kwatchdog.core.storage import Store
from kwatchdog.tui.app import WatchdogApp
from kwatchdog.tui.art import DOBERMAN, MASK
from kwatchdog.tui.screens import DetailScreen, HelpScreen, WatcherForm
from kwatchdog.tui.state import fuzzy_score, sparkline


def test_art_and_mask_aligned():
    art, mask = DOBERMAN.splitlines(), MASK.splitlines()
    assert len(art) == len(mask) and all(len(a) == len(m) for a, m in zip(art, mask))
    assert all(ord(c) < 128 for c in DOBERMAN)  # pure ASCII


def test_fuzzy_and_sparkline():
    assert fuzzy_score("", "x") == 0
    assert fuzzy_score("web", "web/api") > fuzzy_score("wa", "web/api")
    assert fuzzy_score("wapi", "web/api") is not None
    assert fuzzy_score("zzz", "web/api") is None
    assert sparkline([1, 2, 3, 4], 4) == "▁▃▆█"
    assert sparkline([], 4) == ""
    assert sparkline([5, 5], 4) == "▁▁"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHDOG_HOME", str(tmp_path))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent("""
        settings: {heartbeat_port: null}
        projects:
          web:
            watchers:
              - {name: api, type: port, host: 127.0.0.1, port: 9}
              - {name: site, type: port, host: 127.0.0.1, port: 10}
          other:
            watchers:
              - {name: bad, type: nope}
    """))
    store = Store(tmp_path / "watchdog.db")
    store.record_result("web/api", Result(Status.ALERT, "down", latency_ms=5))
    store.record_result("web/site", Result(Status.OK, "up", latency_ms=7))
    store.add_event("web/api", "alert", "ALERT", "down", "bell:ok")
    return cfg, store


async def test_client_mode_renders_state(setup):
    cfg, store = setup
    app = WatchdogApp(cfg, splash=False)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        snap = app.snap
        assert snap.counts()[Status.ALERT] == 1 and snap.counts()[Status.OK] == 1
        assert any("unknown watcher type 'nope'" in e for e in snap.errors)
        main = app.main
        assert main.has_class("alerting")
        table = main.query_one("#table")
        assert table.row_count == 3
        assert main.query_one("#feed").row_count == 1
        assert "daemon SLEEPING" in str(main.query_one("#topbar").render())
        # actions go through the command queue
        table.focus()
        await pilot.press("r")
        await pilot.press("d")
        await pilot.pause(0.1)
        kinds = [k for k, _, _ in store.take_commands()]
        assert kinds == ["run", "disable"]
        # drill-down
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert isinstance(app.screen, DetailScreen)
        assert app.screen.query_one("#detail-results").row_count == 1
        await pilot.press("escape")
        await pilot.press("question_mark")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        # fuzzy search filters the table
        await pilot.press("slash")
        for ch in "site":
            await pilot.press(ch)
        await pilot.pause(0.2)
        assert main.query_one("#table").row_count == 1


async def test_form_validates_before_saving(setup):
    cfg, _ = setup
    app = WatchdogApp(cfg, splash=False)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.press("a")
        await pilot.pause(0.3)
        form = app.screen
        assert isinstance(form, WatcherForm)
        form.query_one("#f-project").value = "web"
        form.query_one("#f-name").value = "new"
        await pilot.press("ctrl+s")  # http without url -> error, nothing written
        await pilot.pause(0.1)
        assert "url" in str(form.query_one("#form-error").render())
        assert "new" not in cfg.read_text()
        form.query_one("#o-url").value = "https://example.com"
        await pilot.press("ctrl+s")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, WatcherForm)
        assert "https://example.com" in cfg.read_text()


async def test_splash_dismisses(setup):
    cfg, _ = setup
    app = WatchdogApp(cfg, splash=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.2)
        assert type(app.screen).__name__ == "SplashScreen"
        await pilot.pause(1.6)
        assert app.screen is app.main
