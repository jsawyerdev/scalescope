from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi import FastAPI

from scalescope import main


def test_lifespan_awaits_background_task_before_closing_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    original_state = dict(main.app_state)

    class FakeStore:
        def __init__(self, db_path: str) -> None:
            self.db_path = db_path

        def close(self) -> None:
            events.append("store_closed")

    async def fake_simulation_loop(store: FakeStore) -> None:
        try:
            await asyncio.Future()
        finally:
            events.append("task_cancelled")

    async def run_lifespan() -> None:
        async with main.lifespan(FastAPI()):
            await asyncio.sleep(0)

    monkeypatch.setattr(main, "settings", replace(main.settings, mode="demo"))
    monkeypatch.setattr(main, "Store", FakeStore)
    monkeypatch.setattr(main, "_simulation_loop", fake_simulation_loop)
    try:
        asyncio.run(run_lifespan())
    finally:
        main.app_state.clear()
        main.app_state.update(original_state)

    assert events == ["task_cancelled", "store_closed"]
