import httpx
import pytest

from kwatchdog.core.plugin import WatcherContext, registries


class Ctx(WatcherContext):
    """In-memory watcher context with an optional mocked HTTP transport."""

    def __init__(self, handler=None, history=None, heartbeats=None, now=None):
        super().__init__("proj", "w")
        self._history = history or {}
        self._heartbeats = heartbeats or {}
        self._now = now
        if handler is not None:
            self._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def now(self):
        return self._now if self._now is not None else super().now()


@pytest.fixture
def make():
    """make('http', ctx=..., url=...) -> watcher instance validated through its schema."""
    wreg, _ = registries()

    def _make(type_, ctx=None, timeout=5.0, **opts):
        cls = wreg.get(type_)
        return cls("w", cls.Config.model_validate(opts), ctx or Ctx(), timeout=timeout)

    return _make
