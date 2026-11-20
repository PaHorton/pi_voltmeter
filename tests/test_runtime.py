import threading

import pytest

from voltmeter.runtime import PluginRuntime, PluginSchedule


class FakePlugin:
    def __init__(self, fail=False):
        self.fail = fail
        self.closed = threading.Event()

    def open(self):
        if self.fail:
            raise OSError("disconnected")

    def sample(self):
        return []

    def close(self):
        self.closed.set()


def test_failing_plugin_does_not_block_another():
    failed, working = FakePlugin(True), FakePlugin()
    published = threading.Event()
    runtime = PluginRuntime([PluginSchedule("bad", failed, 0.1),
                             PluginSchedule("good", working, 0.1)],
                            lambda readings: published.set())
    runtime.start()
    try:
        assert published.wait(2)
        assert failed.closed.wait(2)
    finally:
        runtime.close()
    assert working.closed.is_set()


def test_runtime_rejects_invalid_schedules():
    with pytest.raises(ValueError):
        PluginSchedule("bad", FakePlugin(), 0)
    schedule = PluginSchedule("same", FakePlugin(), 1)
    with pytest.raises(ValueError):
        PluginRuntime([schedule, schedule], lambda readings: None)
