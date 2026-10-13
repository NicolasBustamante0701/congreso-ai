"""
Base de datos local de Diana (SQLite, stdlib) en DATA_DIR/diana.db.

Un solo archivo con esquema versionado (PRAGMA user_version): cada versión
nueva es una migración que solo AGREGA tablas o columnas, así una
actualización de la app nunca borra lo que el usuario ya tenía guardado.

  v1  sesiones transcritas, ventanas de audio y su índice FTS5
  v2  proyectos seguidos y alertas de cambios
  v3  documentos PDF completos, por página, con índice FTS5
"""
import sqlite3
from contextlib import contextmanager

import config
from config import logger

DB_PATH = config.DB_FILE
VERSION_ESQUEMA = 3

_MIGRACIONES = {
    1: """
        CREATE TABLE sesion (
            video_id   TEXT PRIMARY KEY,
            titulo     TEXT NOT NULL DEFAULT '',
            url        TEXT NOT NULL DEFAULT '',
            en_vivo    INTEGER NOT NULL DEFAULT 0,
            estado     TEXT NOT NULL DEFAULT 'en_curso',
            creado     TEXT NOT NULL,
            actualizado TEXT NOT NULL,
            informe    TEXT,
            informe_generado TEXT
        );
        CREATE TABLE ventana (
            id       INTEGER PRIMARY KEY,
            video_id TEXT NOT NULL REFERENCES sesion(video_id) ON DELETE CASCADE,
            start_s  INTEGER NOT NULL,
            end_s    INTEGER NOT NULL,
            texto    TEXT NOT NULL DEFAULT '',
            estado   TEXT NOT NULL,
            UNIQUE (video_id, start_s)
        );
        CREATE INDEX ventana_video ON ventana(video_id, start_s);
        CREATE VIRTUAL TABLE ventana_fts USING fts5(
            texto, content='ventana', content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TRIGGER ventana_ai AFTER INSERT ON ventana BEGIN
            INSERT INTO ventana_fts(rowid, texto) VALUES (new.id, new.texto);
        END;
        CREATE TRIGGER ventana_ad AFTER DELETE ON ventana BEGIN
            INSERT INTO ventana_fts(ventana_fts, rowid, texto) VALUES ('delete', old.id, old.texto);
        END;
        CREATE TRIGGER ventana_au AFTER UPDATE ON ventana BEGIN
            INSERT INTO ventana_fts(ventana_fts, rowid, texto) VALUES ('delete', old.id, old.texto);
            INSERT INTO ventana_fts(rowid, texto) VALUES (new.id, new.texto);
        END;
    """,
    2: """
        CREATE TABLE seguimiento (
            numero   TEXT PRIMARY KEY,
            titulo   TEXT NOT NULL DEFAULT '',
            estado   TEXT NOT NULL DEFAULT '',
            enlace   TEXT NOT NULL DEFAULT '',
            huella   TEXT NOT NULL DEFAULT '{}',
            creado   TEXT NOT NULL,
            ultima_revision TEXT,
            ultimo_error    TEXT
        );
        CREATE TABLE alerta (
            id      INTEGER PRIMARY KEY,
            numero  TEXT NOT NULL REFERENCES seguimiento(numero) ON DELETE CASCADE,
            creada  TEXT NOT NULL,
            tipo    TEXT NOT NULL,
            detalle TEXT NOT NULL,
            enlace  TEXT NOT NULL DEFAULT '',
            leida   INTEGER NOT NULL DEFAULT 0,
            UNIQUE (numero, tipo, detalle)
        );
        CREATE INDEX alerta_no_leida ON alerta(leida, creada);
    """,
    3: """
        CREATE TABLE documento (
            id      TEXT PRIMARY KEY,
            nombre  TEXT NOT NULL,
            origen  TEXT NOT NULL DEFAULT '',
            paginas INTEGER NOT NULL,
            creado  TEXT NOT NULL
        );
        CREATE TABLE documento_pagina (
            rowid_  INTEGER PRIMARY KEY,
            doc_id  TEXT NOT NULL REFERENCES documento(id) ON DELETE CASCADE,
            num     INTEGER NOT NULL,
            texto   TEXT NOT NULL,
            UNIQUE (doc_id, num)
        );
        CREATE VIRTUAL TABLE documento_fts USING fts5(
            texto, content='documento_pagina', content_rowid='rowid_',
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TRIGGER documento_pagina_ai AFTER INSERT ON documento_pagina BEGIN
            INSERT INTO documento_fts(rowid, texto) VALUES (new.rowid_, new.texto);
        END;
        CREATE TRIGGER documento_pagina_ad AFTER DELETE ON documento_pagina BEGIN
            INSERT INTO documento_fts(documento_fts, rowid, texto) VALUES ('delete', old.rowid_, old.texto);
        END;
    """,
}


@contextmanager
def conexion():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA foreign_keys = ON")
        _migrar(con)
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _migrar(con):
    actual = con.execute("PRAGMA user_version").fetchone()[0]
    if actual >= VERSION_ESQUEMA:
        return
    con.execute("PRAGMA journal_mode = WAL")
    for v in range(actual + 1, VERSION_ESQUEMA + 1):
        con.executescript(_MIGRACIONES[v])
        con.execute(f"PRAGMA user_version = {v}")
        logger.info("diana.db migrada a la versión %d", v)
