"""
Proyectos seguidos y alertas de cambios.

El usuario sigue un proyecto por su número; Diana guarda una "huella" de lo
que SPLEY documenta del expediente y, en cada revisión, la compara con la
nueva. Solo es cambio lo que tiene contenido:

  estado        el estado del proyecto es otro
  seguimiento   aparece un paso nuevo en el seguimiento (fecha + estado +
                comisión), p. ej. el decreto a una comisión o un dictamen
  documento     aparece un adjunto nuevo (por su URL)

Reordenar, reformatear o volver a publicar lo mismo no genera alerta: la
huella se arma con esos campos, no con el texto completo. La UNIQUE de la
tabla alerta evita repetir la misma alerta si una revisión se corre dos
veces.

Cuándo se revisa: al pedirlo el usuario, y cada REVISION_HORAS mientras la
app está abierta (bucle en server.py). Con la app cerrada no corre nada:
no hay un servicio residente.
"""
import asyncio
import json
import uuid

from config import ahora_lima, logger
from services import db

REVISION_HORAS = 3
_lock = asyncio.Lock()   # una revisión completa a la vez


def _ahora() -> str:
    return ahora_lima().strftime("%Y-%m-%d %H:%M:%S")


def huella(exp: dict) -> dict:
    pasos = sorted({f"{s.get('fecha', '')}|{s.get('estado', '')}|{s.get('comision', '')}"
                    for s in exp.get("seguimiento") or []})
    docs = sorted({a.get("url", "") for a in exp.get("todos_los_adjuntos") or [] if a.get("url")})
    return {"estado": exp.get("estado") or "", "pasos": pasos, "docs": docs}


def diferencias(antes: dict, ahora: dict) -> list[tuple[str, str]]:
    """(tipo, detalle) de lo que cambió entre dos huellas."""
    cambios = []
    if antes.get("estado") and ahora["estado"] and antes["estado"] != ahora["estado"]:
        cambios.append(("estado", f"Cambió de estado: {antes['estado']} → {ahora['estado']}"))
    for p in sorted(set(ahora["pasos"]) - set(antes.get("pasos") or [])):
        fecha, estado, comision = (p.split("|") + ["", "", ""])[:3]
        texto = " · ".join(x for x in (fecha, estado, comision) if x)
        cambios.append(("seguimiento", f"Nuevo paso en el seguimiento: {texto}"))
    nuevos_docs = set(ahora["docs"]) - set(antes.get("docs") or [])
    if nuevos_docs:
        cambios.append(("documento", f"{len(nuevos_docs)} documento(s) nuevo(s) en el expediente"))
    return cambios


async def _fetch(numero: str) -> dict:
    from scraper import fetch_expediente  # import diferido: scraper es pesado
    return await fetch_expediente(numero)


async def seguir(numero: str) -> dict:
    """Empieza a seguir un proyecto. La primera huella es la línea base: no alerta."""
    numero = numero.strip().upper()
    exp = await _fetch(numero)
    if not isinstance(exp, dict) or exp.get("error") or not exp.get("numero"):
        raise ValueError((exp or {}).get("error") or f"SPLEY no devolvió el expediente {numero}.")
    with db.conexion() as con:
        con.execute(
            """INSERT INTO seguimiento (numero, titulo, estado, enlace, huella, creado, ultima_revision)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(numero) DO NOTHING""",
            (exp["numero"], exp.get("titulo") or "", exp.get("estado") or "",
             exp.get("enlace_expediente") or "", json.dumps(huella(exp)), _ahora(), _ahora()))
    return obtener(exp["numero"])


def dejar(numero: str) -> bool:
    with db.conexion() as con:
        return con.execute("DELETE FROM seguimiento WHERE numero = ?", (numero,)).rowcount > 0


def obtener(numero: str) -> dict | None:
    with db.conexion() as con:
        f = con.execute("SELECT * FROM seguimiento WHERE numero = ?", (numero,)).fetchone()
    return _fila(f) if f else None


def _fila(f) -> dict:
    d = dict(f)
    d.pop("huella", None)
    return d


def listar() -> list[dict]:
    with db.conexion() as con:
        filas = con.execute(
            """SELECT s.*, (SELECT COUNT(*) FROM alerta a WHERE a.numero = s.numero AND a.leida = 0) AS sin_leer
               FROM seguimiento s ORDER BY s.creado DESC""").fetchall()
    return [_fila(f) for f in filas]


def alertas(solo_sin_leer: bool = False, limite: int = 100) -> list[dict]:
    sql = """SELECT a.*, s.titulo FROM alerta a JOIN seguimiento s ON s.numero = a.numero"""
    if solo_sin_leer:
        sql += " WHERE a.leida = 0"
    sql += " ORDER BY a.creada DESC, a.id DESC LIMIT ?"
    with db.conexion() as con:
        return [dict(f) for f in con.execute(sql, (limite,)).fetchall()]


def marcar_leidas(numero: str | None = None) -> int:
    with db.conexion() as con:
        if numero:
            return con.execute("UPDATE alerta SET leida = 1 WHERE numero = ? AND leida = 0", (numero,)).rowcount
        return con.execute("UPDATE alerta SET leida = 1 WHERE leida = 0").rowcount


async def revisar(numero: str) -> dict:
    """Revisa un proyecto: guarda las alertas nuevas y devuelve qué encontró."""
    with db.conexion() as con:
        f = con.execute("SELECT * FROM seguimiento WHERE numero = ?", (numero,)).fetchone()
    if f is None:
        raise KeyError(numero)
    try:
        exp = await _fetch(numero)
        if not isinstance(exp, dict) or exp.get("error"):
            raise RuntimeError((exp or {}).get("error") or "respuesta vacía")
    except Exception as e:
        # Fallo de la fuente: se registra, NO se toma como "sin cambios".
        with db.conexion() as con:
            con.execute("UPDATE seguimiento SET ultima_revision = ?, ultimo_error = ? WHERE numero = ?",
                        (_ahora(), f"{type(e).__name__}: {str(e)[:160]}", numero))
        return {"numero": numero, "ok": False, "error": str(e)[:160], "nuevas": 0}

    nueva = huella(exp)
    cambios = diferencias(json.loads(f["huella"] or "{}"), nueva)
    nuevas = 0
    with db.conexion() as con:
        for tipo, detalle in cambios:
            nuevas += con.execute(
                "INSERT OR IGNORE INTO alerta (numero, creada, tipo, detalle, enlace) VALUES (?, ?, ?, ?, ?)",
                (numero, _ahora(), tipo, detalle, exp.get("enlace_expediente") or f["enlace"])).rowcount
        con.execute(
            """UPDATE seguimiento SET huella = ?, estado = ?, titulo = ?, ultima_revision = ?, ultimo_error = NULL
               WHERE numero = ?""",
            (json.dumps(nueva), exp.get("estado") or f["estado"], exp.get("titulo") or f["titulo"], _ahora(), numero))
    return {"numero": numero, "ok": True, "nuevas": nuevas, "cambios": [d for _, d in cambios]}


async def revisar_todos(motivo: str = "manual") -> dict:
    """
    Revisa todos los seguidos, de a dos en paralelo. Si ya hay una revisión
    corriendo, no arranca otra (evita ejecuciones duplicadas cuando coinciden
    el bucle automático y el botón).
    """
    if _lock.locked():
        return {"en_curso": True}
    async with _lock:
        corrida = uuid.uuid4().hex[:8]
        numeros = [s["numero"] for s in listar()]
        logger.info("seguimiento %s: revisando %d proyectos (%s)", corrida, len(numeros), motivo)
        sem = asyncio.Semaphore(2)

        async def uno(n):
            async with sem:
                return await revisar(n)

        resultados = await asyncio.gather(*(uno(n) for n in numeros))
        nuevas = sum(r["nuevas"] for r in resultados)
        fallas = [r["numero"] for r in resultados if not r["ok"]]
        logger.info("seguimiento %s: %d alertas nuevas, %d fallas %s", corrida, nuevas, len(fallas), fallas or "")
        return {"corrida": corrida, "revisados": len(numeros), "nuevas": nuevas, "fallas": fallas,
                "terminado": _ahora()}


async def bucle_automatico():
    """Revisión periódica mientras el server está vivo (lo lanza server.py)."""
    await asyncio.sleep(120)   # dejar arrancar la app tranquila
    while True:
        try:
            if listar():
                await revisar_todos("automática")
        except Exception:
            logger.exception("la revisión automática del seguimiento falló")
        await asyncio.sleep(REVISION_HORAS * 3600)
