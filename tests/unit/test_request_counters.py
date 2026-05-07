"""Unit tests for framework.graphql.server._wrap_with_counters."""

from unittest.mock import AsyncMock

import pytest

from framework.graphql.errors import MultiplicityViolationError
from framework.graphql.server import _wrap_with_counters
from framework.stats import RequestCounters


async def test_wrap_increments_request_count_on_success() -> None:
    rc = RequestCounters()
    inner = AsyncMock(return_value={"id": "1"})
    wrapped = _wrap_with_counters("getUser", inner, rc)

    result = await wrapped(None, None, id="1")

    assert result == {"id": "1"}
    assert rc.request_count_by_op == {"getUser": 1}
    assert rc.errors == {}


async def test_wrap_increments_for_each_call() -> None:
    rc = RequestCounters()
    inner = AsyncMock(return_value=None)
    wrapped = _wrap_with_counters("getUser", inner, rc)

    for _ in range(5):
        await wrapped(None, None)

    assert rc.request_count_by_op == {"getUser": 5}


async def test_wrap_classifies_multiplicity_violation() -> None:
    rc = RequestCounters()
    inner = AsyncMock(side_effect=MultiplicityViolationError("getUser", 2, {"id": "1"}))
    wrapped = _wrap_with_counters("getUser", inner, rc)

    with pytest.raises(MultiplicityViolationError):
        await wrapped(None, None, id="1")

    # Counter incremented BEFORE exception (every invocation counts).
    assert rc.request_count_by_op == {"getUser": 1}
    assert rc.errors == {"multiplicity.violation": 1}


async def test_wrap_classifies_internal_error_for_other_exceptions() -> None:
    rc = RequestCounters()
    inner = AsyncMock(side_effect=RuntimeError("postgres connection lost"))
    wrapped = _wrap_with_counters("getUser", inner, rc)

    with pytest.raises(RuntimeError):
        await wrapped(None, None)

    assert rc.request_count_by_op == {"getUser": 1}
    assert rc.errors == {"framework.internal_error": 1}


async def test_wrap_accumulates_per_op() -> None:
    rc = RequestCounters()
    inner_get = AsyncMock(return_value=None)
    inner_list = AsyncMock(return_value=[])
    wrap_get = _wrap_with_counters("getUser", inner_get, rc)
    wrap_list = _wrap_with_counters("listUsersByTeam", inner_list, rc)

    for _ in range(3):
        await wrap_get(None, None)
    for _ in range(2):
        await wrap_list(None, None)

    assert rc.request_count_by_op == {"getUser": 3, "listUsersByTeam": 2}
