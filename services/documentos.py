"""
PDFs completos para preguntar sobre ellos.

Al chat solo entra el comienzo del documento (MAX_TEXT_CHARS en pdf.py): un
PDF de 178 páginas se cortaba en la 42 y Diana — con razón — contestaba que
el artículo 90 no estaba en lo que tenía. Ahora el documento entero se
guarda por página en la base local y, para cada pregunta, se recuperan las
páginas pertinentes:

  1. referencias exactas: "artículo 90", "art. 90", "página 120" → la
     página donde aparece ese encabezado (y la siguiente, porque un artículo
     suele seguir en la otra página);
  2. si no, búsqueda por palabras (BM25 en FTS5).

Se le pasa al modelo el texto COMPLETO de esas páginas con su número físico,
para que cite de dónde sale cada dato.
"""
import hashlib
import re

from config import ahora_lima
from services import db
from services.transcripts import _consulta_fts

MAX_PAGINAS = 6
RE_ARTICULO = re.compile(r"\bart(?:[íi]culo|\.)\s*(\d{1,4})", re.I)
RE_PAGINA = re.compile(r"\bp[áa]gina\s+(\d{1,4})", re.I)


def guardar(nombre: str, origen: str, paginas: list[str]) -> str:
    """Guarda el documento (si ya estaba, no lo duplica) y devuelve su id."""
    h = hashlib.sha1()
    for p in paginas:
        h.update(p.encode("utf-8", "ignore"))
    doc_id = h.hexdigest()[:12]
    with db.conexion() as con:
        if con.execute("SELECT 1 FROM documento WHERE id = ?", (doc_id,)).fetchone():
            return doc_id
        con.execute("INSERT INTO documento (id, nombre, origen, paginas, creado) VALUES (?, ?, ?, ?, ?)",
                    (doc_id, nombre[:300], origen[:500], len(paginas), ahora_lima().strftime("%Y-%m-%d %H:%M:%S")))
        con.executemany("INSERT INTO documento_pagina (doc_id, num, texto) VALUES (?, ?, ?)",
                        [(doc_id, i, t) for i, t in enumerate(paginas, start=1) if t])
    return doc_id


def info(doc_id: str) -> dict | None:
    with db.conexion() as con:
        f = con.execute("SELECT * FROM documento WHERE id = ?", (doc_id,)).fetchone()
    return dict(f) if f else None


def paginas_relevantes(doc_id: str, pregunta: str) -> list[tuple[int, str]]:
    """[(número de página, texto)] en orden de página, como máximo MAX_PAGINAS."""
    elegidas: list[int] = []
    with db.conexion() as con:
        def agregar(n):
            if n not in elegidas and len(elegidas) < MAX_PAGINAS:
                elegidas.append(n)

        for m in RE_PAGINA.finditer(pregunta):
            agregar(int(m.group(1)))
        for m in RE_ARTICULO.finditer(pregunta):
            n = m.group(1)
            # El encabezado del artículo, no cualquier mención ("ver artículo 90").
            patron = re.compile(rf"(?:^|\n)\s*Art[íi]culo\s+{n}\s*[°º.\-–:]", re.I)
            for num, texto in con.execute(
                    "SELECT num, texto FROM documento_pagina WHERE doc_id = ? AND texto LIKE ? ORDER BY num",
                    (doc_id, f"%{n}%")):
                if patron.search(texto):
                    agregar(num)
                    agregar(num + 1)
                    break
        consulta = _consulta_fts(RE_ARTICULO.sub(" ", pregunta))
        if consulta and len(elegidas) < MAX_PAGINAS:
            for (num,) in con.execute(
                    """SELECT p.num FROM documento_fts JOIN documento_pagina p ON p.rowid_ = documento_fts.rowid
                       WHERE documento_fts MATCH ? AND p.doc_id = ? ORDER BY bm25(documento_fts) LIMIT ?""",
                    (consulta, doc_id, MAX_PAGINAS)):
                agregar(num)
        if not elegidas:
            return []
        filas = []
        for n in sorted(elegidas):
            f = con.execute("SELECT num, texto FROM documento_pagina WHERE doc_id = ? AND num = ?",
                            (doc_id, n)).fetchone()
            if f:
                filas.append((f["num"], f["texto"]))
    return filas
