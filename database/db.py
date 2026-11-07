"""
Database access and initialization layer for si4k.git-sync.
Ensures WAL mode and consistent busy timeout across all threads/processes.
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DEFAULT_DB_PATH = Path(os.environ.get("SI4K_SYNC_DB_PATH", Path.home() / ".local/share/si4k-git-sync/sync-state.sqlite"))


def get_db_path() -> Path:
    env_path = os.environ.get("SI4K_SYNC_DB_PATH")
    if env_path:
        return Path(env_path)
    return DEFAULT_DB_PATH


def init_db(db_path: Path | None = None) -> Path:
    target_path = db_path or get_db_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    
    with sqlite3.connect(target_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
    return target_path


@contextmanager
def get_connection(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    target_path = db_path or get_db_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(target_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
    finally:
        conn.close()
