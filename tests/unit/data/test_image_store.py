"""File-operation logic shared by the model image manager and the evaluator.

Pure pathlib — no tkinter/cv2/PyTorch — so it runs under every interpreter.
"""

from __future__ import annotations

from pathlib import Path

from sorter.data import image_store


def _touch(folder: Path, *names: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).touch()


# ----- filename parsing ------------------------------------------------------


def test_parse_headstamp() -> None:
    assert image_store.parse_headstamp("WIN__123.jpg") == "WIN"
    assert image_store.parse_headstamp("/a/b/FC 9MM__99.png") == "FC 9MM"
    # No separator: the whole stem is returned so the file still sorts/displays.
    assert image_store.parse_headstamp("loose.jpg") == "loose"


def test_has_headstamp() -> None:
    assert image_store.has_headstamp("WIN__1.jpg") is True
    assert image_store.has_headstamp("loose.jpg") is False


# ----- listing ---------------------------------------------------------------


def test_list_images_filters_and_sorts(tmp_path: Path) -> None:
    _touch(tmp_path, "B__2.jpg", "a__1.JPG", "c__3.PNG", "notes.txt", "x__4.tiff")
    (tmp_path / "sub").mkdir()  # directories excluded
    names = [p.name for p in image_store.list_images(tmp_path)]
    # .txt and .tiff excluded; case-insensitive name sort.
    assert names == ["a__1.JPG", "B__2.jpg", "c__3.PNG"]


def test_list_images_missing_dir(tmp_path: Path) -> None:
    assert image_store.list_images(tmp_path / "nope") == []


# ----- filtering -------------------------------------------------------------


def test_filter_all_returns_everything(tmp_path: Path) -> None:
    _touch(tmp_path, "WIN__1.jpg", "FC__2.jpg")
    files = image_store.list_images(tmp_path)
    assert image_store.filter_images(files, image_store.ALL_FILTER, ["WIN", "FC"]) == files
    # Empty selection behaves like "All".
    assert image_store.filter_images(files, "", ["WIN", "FC"]) == files


def test_filter_by_prefix(tmp_path: Path) -> None:
    _touch(tmp_path, "WIN__1.jpg", "WIN__2.jpg", "WINCHESTER__3.jpg", "FC__4.jpg")
    files = image_store.list_images(tmp_path)
    got = [p.name for p in image_store.filter_images(files, "WIN", ["WIN", "WINCHESTER", "FC"])]
    # Exact prefix match on "WIN__" — WINCHESTER must not leak in.
    assert got == ["WIN__1.jpg", "WIN__2.jpg"]


def test_filter_unknown_headstamp(tmp_path: Path) -> None:
    _touch(tmp_path, "WIN__1.jpg", "MYSTERY__2.jpg", "loose.jpg")
    files = image_store.list_images(tmp_path)
    got = {p.name for p in image_store.filter_images(files, image_store.UNKNOWN_FILTER, ["WIN"])}
    # Unknown prefix and no-prefix files both count as "unknown".
    assert got == {"MYSTERY__2.jpg", "loose.jpg"}


# ----- reclassify ------------------------------------------------------------


def test_reclassify_renames_prefix(tmp_path: Path) -> None:
    src = tmp_path / "WIN__123.jpg"
    src.touch()
    dest = image_store.reclassify(src, "FC")
    assert dest is not None
    assert dest.name == "FC__123.jpg"
    assert dest.exists() and not src.exists()


def test_reclassify_noop_when_same(tmp_path: Path) -> None:
    src = tmp_path / "WIN__1.jpg"
    src.touch()
    assert image_store.reclassify(src, "WIN") is None
    assert src.exists()


def test_reclassify_skips_when_target_exists(tmp_path: Path) -> None:
    src = tmp_path / "WIN__1.jpg"
    src.touch()
    (tmp_path / "FC__1.jpg").touch()
    assert image_store.reclassify(src, "FC") is None
    assert src.exists()  # original untouched


def test_reclassify_requires_separator(tmp_path: Path) -> None:
    src = tmp_path / "loose.jpg"
    src.touch()
    assert image_store.reclassify(src, "FC") is None
    assert image_store.reclassify(src, "") is None


# ----- delete ----------------------------------------------------------------


def test_delete(tmp_path: Path) -> None:
    src = tmp_path / "WIN__1.jpg"
    src.touch()
    assert image_store.delete(src) is True
    assert not src.exists()
    # Deleting a missing file fails gracefully.
    assert image_store.delete(src) is False
