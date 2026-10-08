"""The status-message ring (issue #112, B4) — UI-free, no Qt needed."""

from __future__ import annotations

from sorter.ui.message_log import ERROR, INFO, MessageLog


def test_the_ring_keeps_only_the_newest_entries() -> None:
    log = MessageLog(maxlen=10)

    for i in range(15):
        log.add(f"line {i}")

    assert [e.text for e in log.entries()] == [f"line {i}" for i in range(5, 15)]


def test_text_is_kept_whole() -> None:
    long = "Run error: " + "x" * 5000
    log = MessageLog()

    log.add(long, level=ERROR)

    assert log.entries()[0].text == long
    assert long in log.dump()


def test_an_unknown_level_is_recorded_as_info() -> None:
    log = MessageLog()

    log.add("hello", level="shouting")

    assert log.entries()[0].level == INFO


def test_an_in_progress_line_is_replaced_by_the_next_one() -> None:
    log = MessageLog()

    log.add("Connecting to COM3…")
    log.add("Connected to COM3.")

    assert [e.text for e in log.entries()] == ["Connected to COM3."]


def test_a_run_of_progress_steps_occupies_one_entry() -> None:
    log = MessageLog(maxlen=5)
    log.add("Run started.")

    for _case in range(50):
        for step in ("Feeding (slot 1)…", "Capturing & cropping…", "Classifying…", "Sorting to slot 1…"):
            log.add(step)

    assert [e.text for e in log.entries()] == ["Run started.", "Sorting to slot 1…"]


def test_an_error_is_appended_after_the_step_it_interrupted() -> None:
    log = MessageLog()

    log.add("Classifying…")
    log.add("Run error: boom", level=ERROR)
    log.add("Feeding (slot 0)…")

    assert [e.text for e in log.entries()] == ["Classifying…", "Run error: boom", "Feeding (slot 0)…"]


def test_progress_can_be_stated_explicitly() -> None:
    log = MessageLog()

    log.add("Downloading m: 10%", progress=True)
    log.add("Downloading m: 20%", progress=True)
    log.add("Imported m.")
    log.add("Saved.", progress=False)
    log.add("Also saved…", progress=False)
    log.add("Next.")

    assert [e.text for e in log.entries()] == ["Imported m.", "Saved.", "Also saved…", "Next."]


def test_listeners_hear_appends_and_replacements() -> None:
    log = MessageLog()
    heard: list[tuple[str, bool]] = []
    log.subscribe(lambda entry, replaced: heard.append((entry.text, replaced)))

    log.add("a")
    log.add("b…")
    log.add("c")

    assert heard == [("a", False), ("b…", False), ("c", True)]


def test_dump_marks_errors_and_clear_empties() -> None:
    log = MessageLog()
    log.add("fine", stamp=0)
    log.add("broken", level=ERROR, stamp=0)

    lines = log.dump().splitlines()

    assert lines[0].endswith(" fine")
    assert lines[1].endswith(" [error] broken")
    log.clear()
    assert len(log) == 0
    assert log.dump() == ""


def test_an_error_is_never_a_placeholder() -> None:
    log = MessageLog()

    log.add("Run error: stopped mid-flush…", level=ERROR)
    log.add("Idle.")

    assert [e.text for e in log.entries()] == ["Run error: stopped mid-flush…", "Idle."]
