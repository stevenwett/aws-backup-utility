from pathlib import Path

import pytest

from s3backup import manifest


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    monkeypatch.setenv("S3BACKUP_CONFIG", str(tmp_path / "config.toml"))


def test_build_manifest_excludes(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "a.txt").write_text("hi")
    (root / ".DS_Store").write_text("junk")
    m = manifest.build_manifest(root, exclude=[".DS_Store"])
    assert "a.txt" in m
    assert ".DS_Store" not in m
    size, mtime = m["a.txt"]
    assert size == 2 and mtime > 0


def test_diff_detects_added(tmp_path):
    prev = {"a.txt": (2, 100)}
    cur = {"a.txt": (2, 100), "new.txt": (5, 200)}
    changes = manifest.diff(prev, cur)
    assert changes.added == ["new.txt"]
    assert changes.modified == []
    assert changes.has_changes


def test_diff_detects_modified_size_and_mtime(tmp_path):
    prev = {"a.txt": (2, 100), "b.txt": (3, 100)}
    cur = {"a.txt": (9, 100), "b.txt": (3, 999)}  # size change, mtime change
    changes = manifest.diff(prev, cur)
    assert set(changes.modified) == {"a.txt", "b.txt"}
    assert changes.count == 2


def test_diff_no_changes(tmp_path):
    prev = {"a.txt": (2, 100)}
    cur = {"a.txt": (2, 100)}
    changes = manifest.diff(prev, cur)
    assert not changes.has_changes


def test_diff_ignores_local_deletions(tmp_path):
    prev = {"a.txt": (2, 100), "gone.txt": (1, 50)}
    cur = {"a.txt": (2, 100)}
    changes = manifest.diff(prev, cur)
    assert not changes.has_changes  # deletion alone doesn't trigger a sync


def test_save_and_load_roundtrip(tmp_path):
    m = {"a.txt": (2, 100), "sub/b.bin": (10, 200)}
    manifest.save_manifest("job1", m)
    loaded = manifest.load_manifest("job1")
    assert loaded == m


def test_load_missing_returns_empty(tmp_path):
    assert manifest.load_manifest("never-saved") == {}
