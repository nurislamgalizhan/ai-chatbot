"""The status page must survive a media directory that is not mounted."""

from __future__ import annotations

import shutil

from app.services.monitoring import disk_usage_for


def test_missing_media_dir_falls_back_to_the_nearest_existing_parent(tmp_path) -> None:
    missing = tmp_path / "volume" / "media"
    usage = disk_usage_for(str(missing))
    assert usage.total == shutil.disk_usage(tmp_path).total
    assert usage.free > 0


def test_existing_dir_is_measured_directly(tmp_path) -> None:
    assert disk_usage_for(str(tmp_path)).total == shutil.disk_usage(tmp_path).total
