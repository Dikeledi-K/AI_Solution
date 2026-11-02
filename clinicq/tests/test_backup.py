"""Encrypted backups: unreadable without the key, complete, and restorable."""

import sqlite3

import pytest

from core import backup
from core import database as db
from core.triage import run_triage


@pytest.fixture
def key():
    return backup.new_key()


def test_backup_is_encrypted_and_restores_completely(make_intake, tmp_path, key):
    intake = make_intake("I have chest pain", first_name="Thandeka", surname="Zwane")
    db.add_patient(intake, run_triage(intake))
    path = backup.create_backup(tmp_path / "backups", key=key)
    raw = path.read_bytes()
    assert b"Thandeka" not in raw and b"SQLite format" not in raw  # unreadable without the key
    check = backup.verify_backup(path, key=key)
    assert check["ok"] and check["patients"] == 1 and check["audit_chain_ok"]
    restored = backup.restore_backup(path, tmp_path / "restored.db", key=key)
    conn = sqlite3.connect(restored)
    assert conn.execute("SELECT first_name FROM patients").fetchone()[0] == "Thandeka"
    conn.close()


def test_backup_is_logged(tmp_path, key):
    backup.create_backup(tmp_path / "backups", key=key)
    assert db.get_audit_log("BACKUP")


def test_wrong_key_cannot_restore(tmp_path, key):
    path = backup.create_backup(tmp_path / "backups", key=key)
    with pytest.raises(backup.BackupError, match="cannot be decrypted"):
        backup.restore_backup(path, tmp_path / "x.db", key=backup.new_key())


def test_restore_never_overwrites_an_existing_database(tmp_path, key):
    path = backup.create_backup(tmp_path / "backups", key=key)
    existing = tmp_path / "live.db"
    existing.write_text("live data")
    with pytest.raises(backup.BackupError, match="already exists"):
        backup.restore_backup(path, existing, key=key)
    assert existing.read_text() == "live data"


def test_old_backups_are_removed_beyond_the_limit(tmp_path, key):
    folder = tmp_path / "backups"
    for i in range(3):
        (folder / f"clinicq_2020010{i}_000000.db.enc").parent.mkdir(parents=True, exist_ok=True)
        (folder / f"clinicq_2020010{i}_000000.db.enc").write_bytes(b"old")
    backup.create_backup(folder, key=key, keep=2)
    names = sorted(p.name for p in folder.glob("*.enc"))
    assert len(names) == 2 and "clinicq_20200100_000000.db.enc" not in names


def test_missing_key_gives_a_clear_message(tmp_path, monkeypatch):
    monkeypatch.delenv("CLINICQ_BACKUP_KEY", raising=False)
    with pytest.raises(backup.BackupError, match="new-key"):
        backup.create_backup(tmp_path / "backups")
