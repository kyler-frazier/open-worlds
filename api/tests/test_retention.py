from datetime import UTC, datetime, timedelta

from open_worlds.common.retention import BackupRef, select_backups_to_prune

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _ref(idx: int, kind: str) -> BackupRef:
    return BackupRef(id=f"{kind}-{idx}", kind=kind, created_at=_BASE + timedelta(days=idx))


def test_prunes_only_oldest_auto_beyond_max():
    backups = [_ref(i, "AUTO") for i in range(5)]
    pruned = select_backups_to_prune(backups, max_auto=3)
    pruned_ids = {b.id for b in pruned}
    # Keep the 3 newest (days 4,3,2); prune days 1,0.
    assert pruned_ids == {"AUTO-0", "AUTO-1"}


def test_manual_backups_never_pruned_and_not_counted():
    backups = [_ref(i, "MANUAL") for i in range(10)] + [_ref(i, "AUTO") for i in range(5)]
    pruned = select_backups_to_prune(backups, max_auto=2)
    assert all(b.kind == "AUTO" for b in pruned)
    assert {b.id for b in pruned} == {"AUTO-0", "AUTO-1", "AUTO-2"}


def test_no_pruning_when_under_limit():
    backups = [_ref(i, "AUTO") for i in range(2)]
    assert select_backups_to_prune(backups, max_auto=5) == []
