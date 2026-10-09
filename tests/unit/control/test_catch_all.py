"""Reason codes and the catch-all tally. Qt-free: the panel only paints this."""

from __future__ import annotations

from sorter.control.catch_all import (
    BATCH_FULL,
    BELOW_FLOOR,
    EMPTY_KEY,
    ROUTED,
    SPECIAL,
    UNASSIGNED,
    UNKNOWN,
    CatchAllTally,
    assigned_session_line,
    classify_reason,
    fixed_by_assignment,
    is_special_label,
    open_bucket,
    percent,
    reason_summary,
    reason_tooltip,
)


def _ok(**overrides: object) -> dict:
    result: dict = {
        "ok": True,
        "slot": 0,
        "label": "BPS",
        "parent": None,
        "reason": UNASSIGNED,
    }
    result.update(overrides)
    return result


def test_special_labels_match_any_case() -> None:
    assert is_special_label("UPSIDE DOWN")
    assert is_special_label("  upside down ")
    assert not is_special_label("WIN")
    assert not is_special_label("")


def test_classify_reason_precedence() -> None:
    # The floor wins over a real bin, a halt, and a special label.
    assert classify_reason(label="WIN", slot=3, above_floor=False, halt=True, known=True) == BELOW_FLOOR
    assert classify_reason(label="UPSIDE DOWN", slot=0, above_floor=False, halt=False, known=True) == BELOW_FLOOR
    assert classify_reason(label="WIN", slot=4, above_floor=True, halt=True, known=True) == BATCH_FULL
    assert classify_reason(label="WIN", slot=3, above_floor=True, halt=False, known=True) == ROUTED
    # Special is checked before "unknown", so a trained class the model list
    # does not contain is still special rather than unknown.
    assert classify_reason(label="upside down", slot=0, above_floor=True, halt=False, known=False) == SPECIAL
    assert classify_reason(label="", slot=0, above_floor=True, halt=False, known=False) == UNKNOWN
    assert classify_reason(label="NOPE", slot=0, above_floor=True, halt=False, known=False) == UNKNOWN
    assert classify_reason(label="BPS", slot=0, above_floor=True, halt=False, known=True) == UNASSIGNED
    # A special label that already has a bin is just routed.
    assert classify_reason(label="UPSIDE DOWN", slot=2, above_floor=True, halt=False, known=True) == ROUTED


def test_add_counts_every_success_and_only_slot_zero_as_catch_all() -> None:
    tally = CatchAllTally()
    tally.add({"ok": False, "slot": 0, "label": "BPS"})
    tally.add(_ok(slot=3, label="WIN", reason=ROUTED))
    tally.add(_ok())
    assert tally.total == 2
    assert tally.catch_all_total == 1
    assert tally.top()[0].key == "BPS"
    assert tally.top()[0].count == 1


def test_key_is_parent_then_label_then_empty() -> None:
    tally = CatchAllTally()
    tally.add(_ok(label="WIN", parent="Brass"))
    tally.add(_ok(label="  ", parent="  "))
    tally.add(_ok(label="", parent=None))
    keys = {bucket.key for bucket in tally.top()}
    assert keys == {"Brass", EMPTY_KEY}
    assert tally.catch_all_total == 3


def test_children_are_counted_only_when_the_label_is_not_the_parent() -> None:
    tally = CatchAllTally()
    tally.add(_ok(label="WIN", parent="Brass", reason=UNASSIGNED))
    tally.add(_ok(label="FC", parent="Brass", reason=BELOW_FLOOR))
    tally.add(_ok(label="brass", parent="Brass", reason=UNASSIGNED))
    bucket = tally.top()[0]
    assert bucket.count == 3
    assert bucket.children == {"WIN": 1, "FC": 1}
    assert bucket.reasons == {UNASSIGNED: 2, BELOW_FLOOR: 1}


def test_top_orders_by_count_then_name_and_other_is_the_rest() -> None:
    tally = CatchAllTally()
    # Equal counts: name is the tie-break, case-insensitive.
    tally.add(_ok(label="IK"))
    tally.add(_ok(label="bps"))
    assert [bucket.key for bucket in tally.top()] == ["bps", "IK"]

    tally.reset()
    for index in range(12):
        name = f"H{index:02d}"
        for _ in range(12 - index):
            tally.add(_ok(label=name))
    top = tally.top()
    assert [bucket.key for bucket in top] == [f"H{index:02d}" for index in range(10)]
    assert top[0].count == 12
    assert tally.top(0) == []
    # H10 has 2 cases, H11 has 1.
    assert tally.other() == (2, 3)
    assert tally.catch_all_total == sum(range(1, 13))


def test_percent_rounds_and_is_zero_for_an_empty_whole() -> None:
    assert percent(319, 615) == 52
    assert percent(2, 3) == 67
    assert percent(0, 0) == 0
    assert percent(1, 0) == 0


def test_reason_summary_and_tooltip() -> None:
    tally = CatchAllTally()
    tally.add(_ok(label="WIN", parent="Brass", reason=UNASSIGNED))
    tally.add(_ok(label="FC", parent="Brass", reason=BELOW_FLOOR))
    bucket = tally.top()[0]
    # Equal counts break the tie on the reason code, so below_floor leads.
    assert reason_summary(bucket) == "Below floor 1, Unassigned 1"
    assert reason_tooltip(bucket) == "Below floor: 1\nUnassigned: 1\n\nLabels:\nFC: 1\nWIN: 1"

    alone = CatchAllTally()
    alone.add(_ok(reason=SPECIAL, label="UPSIDE DOWN"))
    assert reason_summary(alone.top()[0]) == "Upside down"
    assert reason_tooltip(alone.top()[0]) == "Upside down: 1"


def test_open_ranking_drops_assigned_unassigned_and_unknown_cases() -> None:
    tally = CatchAllTally()
    for _ in range(10):
        tally.add(_ok(label="BPS", reason=UNASSIGNED))
    tally.add(_ok(label="BPS", reason=BELOW_FLOOR))
    for _ in range(3):
        tally.add(_ok(label="IK", reason=UNASSIGNED))
    tally.add(_ok(label="GHOST", reason=UNKNOWN))
    tally.add(_ok(label="UPSIDE DOWN", reason=SPECIAL))
    tally.add(_ok(label="CBC", reason=BATCH_FULL))

    def has_slot(key: str) -> bool:
        return key in {"BPS", "GHOST", "UPSIDE DOWN", "CBC"}

    top, other = tally.open_ranking(has_slot)
    by_key = {bucket.key: bucket for bucket in top}
    # The bin does not fix below floor, upside down, or a full batch.
    assert by_key["BPS"].count == 1
    assert by_key["BPS"].reasons == {BELOW_FLOOR: 1}
    assert "UPSIDE DOWN" in by_key and by_key["UPSIDE DOWN"].count == 1
    assert by_key["CBC"].reasons == {BATCH_FULL: 1}
    # Unknown leaves once it has a slot. IK has no slot, so it stays whole.
    assert "GHOST" not in by_key
    assert by_key["IK"].count == 3
    # Remaining counts re-rank: IK's 3 beat BPS's leftover 1.
    assert [bucket.key for bucket in top][:2] == ["IK", "BPS"]
    assert other == (0, 0)
    # The physical total is unchanged.
    assert tally.catch_all_total == 17
    assert fixed_by_assignment(tally.buckets()[0]) == 10

    # Unassigning brings the full row back.
    again, _rest = tally.open_ranking(lambda _key: False)
    restored = next(bucket for bucket in again if bucket.key == "BPS")
    assert restored.count == 11
    assert restored.reasons[UNASSIGNED] == 10


def test_open_ranking_without_a_limit_returns_every_visible_key() -> None:
    tally = CatchAllTally()
    for index in range(12):
        tally.add(_ok(label=f"H{index:02d}"))
    tally.add(_ok(label="H00", reason=BELOW_FLOOR))

    visible, other = tally.open_ranking(lambda key: key == "H00", n=None)
    assert other == (0, 0)
    # Equal remaining counts, so the name orders them. H00 kept only the below-floor case.
    assert [bucket.key for bucket in visible] == [f"H{index:02d}" for index in range(12)]
    assert visible[0].count == 1
    assert visible[0].reasons == {BELOW_FLOOR: 1}

    top, rest = tally.open_ranking(lambda key: False)
    assert [bucket.key for bucket in top] == ["H00"] + [f"H{index:02d}" for index in range(1, 10)]
    assert rest == (2, 2)


def test_open_ranking_promotes_the_next_headstamp_into_the_top_ten() -> None:
    tally = CatchAllTally()
    for index in range(12):
        for _ in range(12 - index):
            tally.add(_ok(label=f"H{index:02d}"))
    top, other = tally.open_ranking(lambda key: key == "H00")
    assert [bucket.key for bucket in top] == [f"H{index:02d}" for index in range(1, 11)]
    assert other == (1, 1)
    assert open_bucket(tally.buckets()[0], has_slot=True) is None


def test_a_reduced_bucket_does_not_keep_child_labels() -> None:
    tally = CatchAllTally()
    tally.add(_ok(label="WIN", parent="Brass", reason=UNASSIGNED))
    tally.add(_ok(label="FC", parent="Brass", reason=BELOW_FLOOR))
    original = tally.top()[0]
    reduced = open_bucket(original, has_slot=True)
    assert reduced is not None
    assert reduced.count == 1
    assert reduced.children == {}
    assert reason_summary(reduced) == "Below floor"
    assert "Labels:" not in reason_tooltip(reduced)
    # Nothing was fixed, so the original bucket (and its children) stay.
    assert open_bucket(original, has_slot=False) is original


def test_assigned_session_line() -> None:
    assert assigned_session_line([]) == ""
    assert assigned_session_line([("BPS", [], 4)]) == ""
    assert assigned_session_line([("BPS", [6], 42)]) == "Assigned this session: BPS → #6 (42 already in bin 0)"
    assert (
        assigned_session_line([("BPS", [6], 42), ("IK", [7], 17)])
        == "Assigned this session: BPS → #6 (42 already in bin 0), IK → #7 (17)"
    )


def test_reset_clears_totals_and_buckets() -> None:
    tally = CatchAllTally()
    tally.add(_ok())
    tally.reset()
    assert tally.total == 0
    assert tally.catch_all_total == 0
    assert tally.top() == []
    assert tally.other() == (0, 0)
