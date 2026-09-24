import asyncio
import math
import time

from moonraker.components.presence_prune import PresencePrune


class FakeDatabase:
    def __init__(self, items):
        self.deleted = []
        self._items = items

    async def ns_items(self, namespace):
        return self._items

    async def delete_batch(self, namespace, keys):
        self.deleted.append((namespace, keys))


class FakeServer:
    error = Exception

    def __init__(self, database):
        self.database = database

    def lookup_component(self, name):
        assert name == "database"
        return self.database


def run_prune(items, max_age_secs=60):
    database = FakeDatabase(items)
    component = PresencePrune.__new__(PresencePrune)
    component.server = FakeServer(database)
    component.namespace = "presence"
    component.max_age_secs = max_age_secs
    asyncio.run(component._prune_handler(100))
    return database.deleted


def test_prune_removes_old_and_non_finite_heartbeats():
    deleted = run_prune([
        ("fresh", time.time() * 1000),
        ("old", 0),
        ("invalid", math.nan),
        ("wrong_type", "yesterday"),
    ])

    assert deleted == [("presence", ["old", "invalid", "wrong_type"])]


def test_prune_removes_stale_millisecond_heartbeat():
    stale_ms = (time.time() - 10 * 86400) * 1000

    assert run_prune([("stale", stale_ms)]) == [("presence", ["stale"])]


def test_prune_keeps_everything_when_all_fresh():
    assert run_prune([("fresh", time.time() * 1000)]) == []
