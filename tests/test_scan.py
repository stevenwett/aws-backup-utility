from pathlib import Path

from s3backup import scan


def make_tree(root: Path):
    (root / "a.txt").write_text("hello")          # 5 bytes
    (root / "b.bin").write_text("xxxxxxxxxx")      # 10 bytes
    sub = root / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("ccc")              # 3 bytes
    (root / ".DS_Store").write_text("junk")        # excluded
    cache = root / "cache"
    cache.mkdir()
    (cache / "big.tmp").write_text("zzzzz")        # excluded via dir/pattern


def test_scan_counts_files_and_bytes(tmp_path):
    make_tree(tmp_path)
    result = scan.scan_local(tmp_path, exclude=[])
    # 3 real files + .DS_Store + cache/big.tmp = 5 files
    assert result.files == 5
    assert result.bytes == 5 + 10 + 3 + 4 + 5


def test_scan_respects_excludes(tmp_path):
    make_tree(tmp_path)
    result = scan.scan_local(tmp_path, exclude=[".DS_Store", "cache"])
    assert result.files == 3
    assert result.bytes == 5 + 10 + 3


def test_scan_excludes_by_glob(tmp_path):
    make_tree(tmp_path)
    result = scan.scan_local(tmp_path, exclude=["*.tmp", ".DS_Store"])
    assert result.files == 3  # a.txt, b.bin, sub/c.txt


def test_human_bytes():
    assert scan.human_bytes(0) == "0 B"
    assert scan.human_bytes(512) == "512 B"
    assert scan.human_bytes(1536) == "1.5 KB"
    assert scan.human_bytes(2_100_000_000_000) == "1.9 TB"
