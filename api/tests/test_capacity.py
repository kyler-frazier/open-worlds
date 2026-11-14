"""The host memory budget rule, kept pure so it is testable without a DB."""

from __future__ import annotations

import pytest

from open_worlds.common.capacity import (
    CapacityExceeded,
    check_memory_budget,
    memory_headroom_mb,
)


def test_no_budget_means_no_limit():
    assert memory_headroom_mb(1_000_000, None) is None
    check_memory_budget(in_use_mb=1_000_000, needed_mb=8192, budget_mb=None)


def test_fits_when_headroom_covers_the_world():
    check_memory_budget(in_use_mb=16384, needed_mb=8192, budget_mb=24576)


def test_refused_when_it_would_oversubscribe():
    with pytest.raises(CapacityExceeded) as exc:
        check_memory_budget(in_use_mb=20480, needed_mb=8192, budget_mb=24576)
    assert "20480 MB of its 24576 MB" in str(exc.value)
    assert "8192 MB" in str(exc.value)


def test_exactly_full_still_fits():
    check_memory_budget(in_use_mb=16384, needed_mb=8192, budget_mb=24576)
    with pytest.raises(CapacityExceeded):
        check_memory_budget(in_use_mb=16385, needed_mb=8192, budget_mb=24576)
