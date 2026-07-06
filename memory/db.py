"""
SQLite store — порт TencentDB-Agent-Memory на Python.
Таблицы: l0_conversations, l1_records, l1_vec (sqlite-vec), l1_fts (FTS5).
"""
from __future__ import annotations
import sqlite3, struct, time, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "mind.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    c = _conn()
    c.executescript("""
    -- L0: сырые диалоги
    CREATE TABLE IF NOT EXISTS l0_conversations (
        record_id   TEXT PRIMARY KEY,
        session_key TEXT NOT NULL,
        session_id  TEXT DEFAULT '',
        role        TEXT NOT NULL,
        message_text TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        timestamp   REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_l0_session ON l0_conversations(session_key, recorded_at);

    -- L1: атомарные факты
    CREATE TABLE IF NOT EXISTS l1_records (
        record_id       TEXT PRIMARY KEY,
        session_key     TEXT NOT NULL,
        session_id      TEXT DEFAULT '',
        content         TEXT NOT NULL,
        type            TEXT NOT NULL,
        priority        INTEGER DEFAULT 50,
        scene_name      TEXT DEFAULT '',
        source_msg_ids  TEXT DEFAULT '[]',
        metadata_json   TEXT DEFAULT '{}',
        timestamp_str   TEXT DEFAULT '',
        timestamp_start TEXT DEFAULT '',
        timestamp_end   TEXT DEFAULT '',
        created_time    TEXT NOT NULL,
        updated_time    TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_l1_session ON l1_records(session_key);
    CREATE INDEX IF NOT EXISTS idx_l1_type    ON l1_records(type);

    -- L2: сцены (markdown файлы на диске, индекс здесь)
    CREATE TABLE IF NOT EXISTS l2_scene_index (
        filename    TEXT PRIMARY KEY,
        session_key TEXT NOT NULL,
        summary     TEXT DEFAULT '',
        heat        INTEGER DEFAULT 1,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );

    -- L3: персона (markdown файл на диске, метаданные здесь)
    CREATE TABLE IF NOT EXISTS l3_persona_meta (
        session_key TEXT PRIMARY KEY,
        updated_at  TEXT NOT NULL,
        scene_count INTEGER DEFAULT 0,
        atom_count  INTEGER DEFAULT 0
    );

    -- Убеждения Мизури
    CREATE TABLE IF NOT EXISTS beliefs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        text            TEXT NOT NULL,
        level           TEXT NOT NULL,
        strength        REAL NOT NULL,
        challenge_count INTEGER DEFAULT 0,
        formed_at       REAL NOT NULL
    );

    -- Внутреннее состояние
    CREATE TABLE IF NOT EXISTS internal_state (
        id         INTEGER PRIMARY KEY CHECK (id=1),
        energy     REAL DEFAULT 0.7,
        tension    REAL DEFAULT 0.25,
        openness   REAL DEFAULT 0.45,
        now_text   TEXT DEFAULT 'тишина.',
        background TEXT DEFAULT 'привычная усталость',
        lingering  TEXT DEFAULT '',
        updated_at REAL NOT NULL
    );

    -- Self-study
    CREATE TABLE IF NOT EXISTS self_study (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        topic       TEXT NOT NULL,
        source      TEXT NOT NULL,
        mizuri_take TEXT NOT NULL,
        ts          REAL NOT NULL
    );
    """)
    c.commit()
    _seed(c)
    c.close()


def _seed(c: sqlite3.Connection) -> None:
    now = time.time()
    if not c.execute("SELECT 1 FROM internal_state WHERE id=1").fetchone():
        c.execute(
            "INSERT INTO internal_state VALUES (1,0.7,0.25,0.45,'тишина.','привычная усталость','',?)",
            (now,)
        )
    if not c.execute("SELECT 1 FROM beliefs").fetchone():
        bp = ROOT / "data" / "initial_beliefs.json"
        if bp.exists():
            for b in json.loads(bp.read_text("utf-8")):
                c.execute(
                    "INSERT INTO beliefs (text,level,strength,formed_at) VALUES (?,?,?,?)",
                    (b["text"], b["level"], float(b["strength"]), now)
                )
    c.commit()


# ── vector helpers ────────────────────────────────────────────────────────────

def pack_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)

def unpack_vec(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob)//4}f", blob))

def cosine(a: list[float], b: list[float]) -> float:
    import math
    dot = sum(x*y for x,y in zip(a,b))
    na = math.sqrt(sum(x*x for x in a))
    nb = math.sqrt(sum(y*y for y in b))
    return dot/(na*nb) if na*nb else 0.0
