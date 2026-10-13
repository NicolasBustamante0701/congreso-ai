"""
Búsqueda unificada de la barra superior.

Una consulta va, en paralelo, a cada fuente donde puede haber algo:
  proyectos       SPLEY por tema (palabra en el título) — fuente oficial
  transcripciones lo que se dijo en las sesiones ya transcritas (FTS local)
  agenda          actividades de los próximos 14 días (agenda oficial)
Un número de proyecto ("00571-2026-2031-CD", "571/2026") se reconoce aparte
y se ofrece abrir su expediente directamente.

Cada fuente informa su propio estado (ok / error) y por qué es relevante
cada resultado: si una fuente no responde se dice, en vez de devolver "no
hay resultados". No hay ranking semántico: coincidencia literal y BM25,
que es lo que se puede explicar.
"""
import asyncio
import re

from services import transcripts

RE_NUMERO = re.compile(r"^\s*(\d{1,5})\s*[-/]\s*(\d{4})(?:\s*-\s*\d{4})?(?:\s*-\s*[A-Z]{2})?\s*$", re.I)
TIMEOUT_FUENTE = 25


def _palabras(q: str) -> list[str]:
    return [w for w in re.findall(r"\w+", q.lower()) if len(w) >= 3]


async def _proyectos(q: str) -> dict:
    from scraper import fetch_proyectos
    r = await fetch_proyectos(materia=q, limit=10)
    if r.get("error"):
        return {"estado": "error", "detalle": r["error"]}
    items = r.get("items") or []
    return {"estado": "ok", "total": r.get("total_disponible") or len(items),
            "criterio": r.get("criterio") or f"título contiene «{q}»",
            "items": [{k: p.get(k, "") for k in ("numero", "titulo", "estado", "fecha_presentacion", "camara", "enlace")}
                      for p in items]}


async def _agenda(q: str) -> dict:
    from scraper import fetch_agenda_camaras
    r = await fetch_agenda_camaras(dias=14)
    if r.get("error"):
        return {"estado": "error", "detalle": r["error"]}
    palabras = _palabras(q)
    def coincide(s):
        texto = " ".join(str(s.get(k, "")) for k in ("tema", "lugar", "organiza", "camara")).lower()
        return palabras and all(p in texto for p in palabras)
    items = [s for s in r.get("sesiones", []) if coincide(s)]
    return {"estado": "ok", "total": len(items), "criterio": "todas las palabras en tema, lugar u organizador",
            "items": items[:10]}


async def _transcripciones(q: str) -> dict:
    items = transcripts.buscar(q, limite=10)
    return {"estado": "ok", "total": len(items), "criterio": "palabras en lo transcrito (BM25)", "items": items}


async def buscar(q: str) -> dict:
    q = q.strip()
    m = RE_NUMERO.match(q)
    numero = None
    if m:
        numero = q.upper().replace("/", "-").replace(" ", "")

    async def con_tope(nombre, coro):
        try:
            return nombre, await asyncio.wait_for(coro, TIMEOUT_FUENTE)
        except TimeoutError:
            return nombre, {"estado": "error", "detalle": f"no respondió en {TIMEOUT_FUENTE} s"}
        except Exception as e:
            return nombre, {"estado": "error", "detalle": type(e).__name__}

    tareas = [con_tope("transcripciones", _transcripciones(q))]
    if not numero:
        tareas += [con_tope("proyectos", _proyectos(q)), con_tope("agenda", _agenda(q))]
    fuentes = dict(await asyncio.gather(*tareas))
    return {"q": q, "numero": numero, "fuentes": fuentes}
