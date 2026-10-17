"""Every shared conformance scenario, against both the sync and the async client."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from conformance_runner import (
    EXPECTED_SCENARIOS,
    SCENARIO_DIR,
    call_async,
    call_sync,
    check,
    load_scenarios,
    make_async,
    make_sync,
)

SCENARIOS = load_scenarios()


def test_scenarios_found() -> None:
    # Review 17d SDK-10: never skipped. Missing fixtures (for example a source distribution without the
    # conformance/ directory) must FAIL, not turn the 2 x N replay tests into a silent green run.
    assert SCENARIO_DIR.is_dir(), f"conformance scenarios not found at {SCENARIO_DIR}"
    assert len(SCENARIOS) >= EXPECTED_SCENARIOS, f"{len(SCENARIOS)} scenarios found, expected {EXPECTED_SCENARIOS}"


@pytest.mark.parametrize("scn", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_sync(scn: Dict[str, Any]) -> None:
    client, replay, clock = make_sync(scn)
    outcome, mailbox, err = call_sync(client, scn)
    check(scn, outcome, err, mailbox, client, replay, clock)


@pytest.mark.parametrize("scn", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
async def test_async(scn: Dict[str, Any]) -> None:
    client, replay, clock = make_async(scn)
    outcome, mailbox, err = await call_async(client, scn)
    check(scn, outcome, err, mailbox, client, replay, clock)
