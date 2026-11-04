from tikstractormf.archive import DownloadLedger, ledger_path_for, migrate_legacy_ledger


def test_migrate_legacy_ledger_moves_file(tmp_path):
    root = tmp_path / "dl"
    root.mkdir()
    legacy_dir = root / ".tikstractormf"
    legacy_dir.mkdir()
    legacy_file = legacy_dir / "dedup.jsonl"
    legacy_file.write_text('{"post_id": "p1", "index": 1}\n', encoding="utf-8")

    assert migrate_legacy_ledger(root) is True
    target = ledger_path_for(root)
    assert target.exists()
    assert "p1" in target.read_text(encoding="utf-8")


def test_ledger_version_roundtrip(tmp_path):
    path = tmp_path / "dedup.jsonl"
    ledger = DownloadLedger(path)
    ledger.record("p", 1, tmp_path / "f.jpg", version="abc")
    ledger2 = DownloadLedger(path)
    ledger2.load()
    assert ledger2.has("p", 1, "abc")
    assert not ledger2.has("p", 1, "def")
    assert not ledger2.has("p", 1)


def test_ledger_legacy_entry_defaults_empty_version(tmp_path):
    path = tmp_path / "dedup.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"post_id": "p", "index": 1}\n', encoding="utf-8")
    ledger = DownloadLedger(path)
    ledger.load()
    assert ledger.has("p", 1, "")
    assert not ledger.has("p", 1, "abc")
