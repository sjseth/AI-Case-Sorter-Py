"""The suite must never write to the contributor's real data folder.

`tests/conftest.py` points CASESORTER_DATA_DIR at a throwaway directory for
every test. These pin that, so removing or breaking the fixture fails loudly
instead of silently overwriting someone's models again.
"""

from __future__ import annotations

import pytest

from sorter import paths


def test_the_data_root_is_a_throwaway_directory(tmp_path_factory: pytest.TempPathFactory) -> None:
    root = paths.app_data_dir()
    assert root.is_relative_to(tmp_path_factory.getbasetemp())
    assert root != paths._os_data_dir()


def test_a_checkpoint_written_through_paths_stays_out_of_the_real_folder() -> None:
    # The exact path the overwrite went through: model 2's checkpoint.
    target = paths.model_trained_path(2)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"CHECKPOINT")
    assert not target.is_relative_to(paths._os_data_dir())
