from tikstractormf.archive import ledger_path_for, migrate_legacy_ledger


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
