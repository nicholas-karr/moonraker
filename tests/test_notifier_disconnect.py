import asyncio

from moonraker.components.notifier import Notifier


class FakeNotifier:
    min_disconnect_duration = 0

    def __init__(self):
        self.calls = []

    async def notify(self, event, args, message):
        self.calls.append((event, args, message))


class FakeKlippyConnection:
    def __init__(self, connected=False):
        self.connected = connected

    def is_connected(self):
        return self.connected


def test_stale_disconnect_timer_does_not_notify():
    manager = Notifier.__new__(Notifier)
    manager.disconnect_generation = 2
    notifier = FakeNotifier()

    asyncio.run(
        manager._notify_after_disconnect(
            notifier, FakeKlippyConnection(), "old disconnect", generation=1
        )
    )

    assert notifier.calls == []


def test_current_disconnect_timer_notifies_if_still_disconnected():
    manager = Notifier.__new__(Notifier)
    manager.disconnect_generation = 2
    notifier = FakeNotifier()

    asyncio.run(
        manager._notify_after_disconnect(
            notifier, FakeKlippyConnection(), "current disconnect", generation=2
        )
    )

    assert notifier.calls == [
        ("klippy_disconnect", [], "current disconnect")
    ]
