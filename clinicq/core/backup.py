"""Encrypted backups of the ClinicQ database.

The database holds identifiable health information (POPIA "special personal
information"), so a backup copy on a USB stick or in the cloud must never be
readable by whoever finds it. Each backup is a consistent snapshot of the live
database (taken safely while ClinicQ is running), encrypted with a secret key.

    python -m core.backup new-key                  print a new key -> CLINICQ_BACKUP_KEY in .env
    python -m core.backup create                   make an encrypted backup in backups/
    python -m core.backup verify  <file>           decrypt in memory and check it is complete
    python -m core.backup restore <file> <new.db>  decrypt to a NEW database file

Keep the key somewhere safe and SEPARATE from the backups (e.g. the facility
manager's password manager or a sealed envelope in the safe). Without the key a
backup cannot be restored – by anyone.

Schedule `create` daily (Windows Task Scheduler / cron – see deploy/LOCAL_NETWORK_SETUP.md)
and copy the backups folder off the clinic computer regularly.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

from core import database as db

load_dotenv()

DEFAULT_BACKUP_DIR = db.BASE_DIR / "backups"
KEEP_BACKUPS = int(os.getenv("CLINICQ_BACKUP_KEEP", "30"))


class BackupError(Exception):
    """A backup could not be made, checked or restored. The message is safe to show."""


def new_key() -> str:
    return Fernet.generate_key().decode()


def _fernet(key: str | None = None) -> Fernet:
    key = key or os.getenv("CLINICQ_BACKUP_KEY")
    if not key:
        raise BackupError("No backup key. Run `python -m core.backup new-key` and put the key in .env as "
                          "CLINICQ_BACKUP_KEY=...")
    try:
        return Fernet(key.encode())
    except ValueError as e:
        raise BackupError("CLINICQ_BACKUP_KEY is not a valid key.") from e


def _snapshot_bytes() -> bytes:
    """A consistent copy of the live database, taken with SQLite's online backup API."""
    db.init_db()
    with tempfile.TemporaryDirectory() as tmp:
        copy_path = Path(tmp) / "snapshot.db"
        source = sqlite3.connect(db.get_db_path())
        target = sqlite3.connect(copy_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return copy_path.read_bytes()


def create_backup(backup_dir: Path | None = None, key: str | None = None, keep: int = KEEP_BACKUPS) -> Path:
    """Write an encrypted snapshot and delete the oldest backups beyond `keep`. Returns the new file."""
    fernet = _fernet(key)
    backup_dir = Path(backup_dir or os.getenv("CLINICQ_BACKUP_DIR", DEFAULT_BACKUP_DIR))
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"clinicq_{datetime.now():%Y%m%d_%H%M%S}.db.enc"
    path.write_bytes(fernet.encrypt(_snapshot_bytes()))
    for old in sorted(backup_dir.glob("clinicq_*.db.enc"))[:-keep]:
        old.unlink()
    db.add_audit("BACKUP", nurse_action="Encrypted backup made", details=path.name)
    return path


def _decrypt(path: Path, key: str | None = None) -> bytes:
    try:
        return _fernet(key).decrypt(Path(path).read_bytes())
    except InvalidToken as e:
        raise BackupError("This backup cannot be decrypted with this key (wrong key, or the file is damaged).") from e


def verify_backup(path: Path, key: str | None = None) -> dict:
    """Decrypt into a temporary file and check it: SQLite integrity + the audit hash chain."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "check.db"
        copy.write_bytes(_decrypt(path, key))
        conn = sqlite3.connect(copy)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            patients = conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
        finally:
            conn.close()
        old = os.environ.get("CLINICQ_DB_PATH")
        os.environ["CLINICQ_DB_PATH"] = str(copy)
        try:
            chain = db.verify_audit_chain()
        finally:
            if old is None:
                os.environ.pop("CLINICQ_DB_PATH", None)
            else:
                os.environ["CLINICQ_DB_PATH"] = old
    return {"ok": integrity == "ok" and chain["ok"], "integrity": integrity, "patients": patients,
            "audit_entries": chain["entries"], "audit_chain_ok": chain["ok"]}


def restore_backup(path: Path, target: Path, key: str | None = None) -> Path:
    """Decrypt a backup into a NEW file (never overwrites, so the live database is safe)."""
    target = Path(target)
    if target.exists():
        raise BackupError(f"{target} already exists. Restore to a new file name, check it, then switch "
                          "CLINICQ_DB_PATH to it.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_decrypt(path, key))
    return target


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    try:
        if command == "new-key":
            print(new_key())
        elif command == "create":
            print(f"Backup written: {create_backup()}")
        elif command == "verify" and len(argv) == 2:
            result = verify_backup(Path(argv[1]))
            print(("OK" if result["ok"] else "PROBLEM") + f": {result}")
            return 0 if result["ok"] else 1
        elif command == "restore" and len(argv) == 3:
            print(f"Restored to {restore_backup(Path(argv[1]), Path(argv[2]))}")
        else:
            print(__doc__)
            return 2
    except BackupError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
