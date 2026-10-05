import textwrap

from kwatchdog.core.config import load_config
from kwatchdog.core.plugin import Channel, Registry, Watcher, load_external_plugins, registries

GOOD = '''
from kwatchdog.core.plugin import Watcher, WatcherConfig
from kwatchdog.core.models import Result

class Cfg(WatcherConfig):
    n: int = 1

class Dummy(Watcher):
    type = "dummy"
    Config = Cfg
    async def check(self):
        return Result.ok(f"n={self.config.n}")
'''

NEEDS_DEP = '''
from kwatchdog.core.plugin import Watcher
class Fancy(Watcher):
    type = "fancy"
    requires = ("definitely_not_installed_module_xyz",)
'''


def test_builtins_discovered():
    w, c = registries()
    for t in ["http", "port", "ping", "process", "systemd", "file", "disk", "system", "logtail", "git",
              "scraper", "datafile", "json", "heartbeat", "shell", "price"]:
        assert t in w.items, t
    for t in ["bell", "desktop", "telegram", "ntfy", "discord", "email"]:
        assert t in c.items, t
    assert not w.errors and not c.errors


async def test_external_plugin_loaded(tmp_path):
    (tmp_path / "good.py").write_text(GOOD)
    (tmp_path / "broken.py").write_text("this is not python(")
    (tmp_path / "empty.py").write_text("x = 1\n")
    (tmp_path / "_private.py").write_text("raise SystemExit")
    wr, cr = Registry(Watcher), Registry(Channel)
    errors = load_external_plugins(tmp_path, [wr, cr])
    assert "dummy" in wr.items
    assert wr.items["dummy"].source.endswith("good.py")
    assert any("broken.py" in k and "SyntaxError" in v for k, v in errors.items())
    assert any("empty.py" in k for k in errors)
    assert not any("_private" in k for k in errors)
    w = wr.items["dummy"]("x", wr.items["dummy"].Config(n=3))
    assert (await w.check()).message == "n=3"


def test_missing_optional_dep_disables_watcher(tmp_path):
    (tmp_path / "fancy.py").write_text(NEEDS_DEP)
    wr, cr = Registry(Watcher), Registry(Channel)
    load_external_plugins(tmp_path, [wr, cr])
    cfg = load_config(None, wr, cr, text=textwrap.dedent("""
        projects: {p: {watchers: [{name: f, type: fancy}]}}
    """))
    spec = cfg.watchers()[0]
    assert spec.unavailable and "pip install definitely_not_installed_module_xyz" in spec.unavailable
    assert not spec.runnable and not cfg.errors
    assert any("disabled" in w for w in cfg.warnings)


def test_platform_gate():
    w, _ = registries()
    import sys

    reason = w.items["systemd"].unavailable_reason()
    if sys.platform.startswith("linux"):
        assert reason is None or "systemctl" in reason
    else:
        assert "only works on linux" in reason


async def test_readme_tutorial_plugin(tmp_path):
    """The 'write your own watcher' example in examples/plugins must keep working."""
    from pathlib import Path

    src = Path(__file__).parent.parent / "examples" / "plugins"
    wr, cr = Registry(Watcher), Registry(Channel)
    assert not load_external_plugins(src, [wr, cr])
    cls = wr.items["inbox"]
    for i in range(12):
        (tmp_path / f"job{i}").write_text("x")
    r = await cls("q", cls.Config(path=str(tmp_path))).check()
    assert r.status.value == "WARN" and r.metrics["files"] == 12


def test_reserved_option_names_rejected(tmp_path):
    (tmp_path / "clash.py").write_text(textwrap.dedent("""
        from kwatchdog.core.plugin import Watcher, WatcherConfig
        class C(WatcherConfig):
            name: str = 'x'
        class W(Watcher):
            type = 'clash'
            Config = C
    """))
    wr, cr = Registry(Watcher), Registry(Channel)
    errors = load_external_plugins(tmp_path, [wr, cr])
    assert "clash" not in wr.items
    assert any("reserved option name(s): name" in v for v in errors.values())
