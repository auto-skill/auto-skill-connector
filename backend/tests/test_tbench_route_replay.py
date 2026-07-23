import importlib.util
import asyncio
import time
from pathlib import Path

import httpx


MODULE_PATH = Path(__file__).resolve().parents[1] / "bench" / "tbench_route_replay.py"
SPEC = importlib.util.spec_from_file_location("tbench_route_replay", MODULE_PATH)
assert SPEC and SPEC.loader
tbench_route_replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tbench_route_replay)


def test_summarize_reports_route_concentration():
    rows = [
        {
            "status_code": 200,
            "tier": "full",
            "latency_ms": 10,
            "queue_wait_ms": 1,
            "retry_wait_ms": 0,
            "results": [{"name": "generic"}],
        },
        {
            "status_code": 200,
            "tier": "none",
            "latency_ms": 20,
            "queue_wait_ms": 2,
            "retry_wait_ms": 0,
            "results": [{"name": "generic"}],
        },
        {
            "status_code": 200,
            "tier": "hint",
            "latency_ms": 30,
            "queue_wait_ms": 3,
            "retry_wait_ms": 5,
            "results": [{"name": "specific"}],
        },
        {
            "status_code": 500,
            "tier": "none",
            "latency_ms": 40,
            "queue_wait_ms": 4,
            "retry_wait_ms": 0,
            "results": [],
        },
    ]

    summary = tbench_route_replay.summarize(rows)

    assert summary["tasks"] == 4
    assert summary["successful_requests"] == 3
    assert summary["tier_counts"] == {"full": 1, "hint": 1, "none": 2}
    assert summary["unique_top_skills"] == 2
    assert summary["most_common_top_skills"][0] == ("generic", 2)
    assert summary["maximum_top_skill_share"] == 0.666667
    assert summary["top_skill_hhi"] == 0.555556
    assert summary["latency_ms"]["median"] == 25
    assert summary["queue_wait_ms"]["median"] == 2.5
    assert summary["retry_wait_ms"]["p95"] == 5


def test_request_pacer_spaces_concurrent_request_starts():
    async def run():
        pacer = tbench_route_replay.RequestPacer(0.02)
        starts = []

        async def record():
            await pacer.wait()
            starts.append(time.monotonic())

        await asyncio.gather(record(), record(), record())
        return starts

    starts = asyncio.run(run())
    assert starts[1] - starts[0] >= 0.015
    assert starts[2] - starts[1] >= 0.015


def test_replay_task_separates_service_queue_and_retry_time(monkeypatch):
    class Clock:
        def __init__(self):
            self.value = 0.0

        def __call__(self):
            return self.value

        def advance(self, seconds):
            self.value += seconds

    class Pacer:
        async def wait(self):
            clock.advance(0.003)

    class Semaphore:
        async def __aenter__(self):
            clock.advance(0.005)

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Client:
        def __init__(self):
            self.calls = 0

        async def post(self, *args, **kwargs):
            clock.advance(0.007)
            self.calls += 1
            status = 429 if self.calls == 1 else 200
            return httpx.Response(
                status,
                headers={"retry-after": "11"} if status == 429 else {},
                json={"tier": "hint", "results": [{"name": "specific"}]},
            )

    async def fake_sleep(seconds):
        assert seconds == 11.0
        clock.advance(seconds)

    clock = Clock()
    monkeypatch.setattr(tbench_route_replay.time, "perf_counter", clock)
    monkeypatch.setattr(tbench_route_replay.asyncio, "sleep", fake_sleep)

    row = asyncio.run(
        tbench_route_replay.replay_task(
            Client(),
            Semaphore(),
            "https://autoskill.test",
            {"id": "task", "instruction": "do the task"},
            5,
            Pacer(),
            1,
        )
    )

    assert row["status_code"] == 200
    assert row["latency_ms"] == 14.0
    assert row["queue_wait_ms"] == 16.0
    assert row["retry_wait_ms"] == 11000.0
