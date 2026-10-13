"""
Transcripciones guardadas: listar, abrir, borrar, buscar, preguntar e informe.

Todo lo de acá trabaja sobre lo que ya se transcribió (services/transcripts):
abrir una sesión terminada o preguntarle algo no vuelve a procesar el video.
"""
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from config import LLM_API_KEY, load_prompt, logger
from services import llm, sse, transcripts

router = APIRouter()

QA_PROMPT = load_prompt("session_qa")
INFORME_PROMPT = load_prompt("resumen_sesion")

# Informe: hasta este tamaño la transcripción va entera en un solo pedido.
# Más larga, primero se sacan notas por bloques y el informe se arma con las
# notas (sesiones de 3-7 h no entran en un pedido).
MAX_CHARS_INFORME_DIRECTO = 24000
SEGUNDOS_POR_BLOQUE = 15 * 60

NOTAS_PROMPT = (
    "Saca notas de este tramo de una sesión parlamentaria, solo con lo que dice el texto. "
    "Una viñeta por hecho relevante, cada una con su marca de tiempo [HH:MM:SS] copiada del "
    "texto: temas, quién habla (solo si el texto lo nombra), propuestas, cifras, decisiones "
    "explícitas, compromisos. Sin introducción ni conclusiones. Máximo 15 viñetas."
)


def _sin_key():
    async def err():
        yield sse.error("No hay una API key de IA configurada. Abre Ajustes de IA y pega una.")
        yield sse.DONE
    return StreamingResponse(err(), media_type="text/event-stream")


def _no_encontrada(video_id: str):
    return JSONResponse({"error": f"No hay una transcripción guardada para {video_id}."},
                        status_code=404)


def _texto_cobertura(datos: dict) -> str:
    c = datos["cobertura"]
    s = datos["sesion"]
    partes = [f"Transcrito desde {c['desde']} hasta {c['hasta']}."]
    if c["huecos"]:
        partes.append("Partes NO procesadas: " + ", ".join(f"{a}–{b}" for a, b in c["huecos"]) + ".")
    if c["errores"]:
        partes.append(f"Ventanas de 10 s con error de transcripción en: {', '.join(c['errores'][:20])}.")
    # Siempre explícito: el título de YouTube conserva "🔴 EN VIVO" aunque la
    # transmisión ya haya terminado, y sin esta línea el modelo deducía del
    # título que los tiempos eran "de captura" en un video grabado.
    if s["en_vivo"]:
        partes.append("Se transcribió mientras estaba EN VIVO: los tiempos son de captura, no del video.")
    else:
        partes.append("Se transcribió el VIDEO GRABADO: los tiempos corresponden al minuto exacto del video, "
                      "aunque el título diga 'EN VIVO'.")
    if s["estado"] == "en_curso":
        partes.append("La transcripción todavía está en curso: puede faltar lo más reciente.")
    return " ".join(partes)


async def _recolectar(messages, max_tokens) -> str:
    texto = []
    async for kind, payload in llm.stream(messages, max_tokens=max_tokens, temperature=0.2):
        if kind == "text":
            texto.append(payload)
        elif kind == "error":
            raise RuntimeError(payload)
    return "".join(texto)


@router.get("/transcripts")
async def listar():
    return {"sesiones": transcripts.listar_sesiones()}


@router.get("/transcripts/buscar")
async def buscar(q: str = Query(..., min_length=2, max_length=200),
                 video_id: str | None = Query(None)):
    """Busca en TODAS las sesiones guardadas (o en una) sin volver a transcribir."""
    return {"resultados": transcripts.buscar(q, video_id=video_id, limite=30)}


@router.get("/transcripts/{video_id}")
async def obtener(video_id: str):
    datos = transcripts.obtener(video_id)
    return datos if datos else _no_encontrada(video_id)


@router.delete("/transcripts/{video_id}")
async def borrar(video_id: str):
    if not transcripts.borrar(video_id):
        return _no_encontrada(video_id)
    logger.info("transcripción de %s borrada por el usuario", video_id)
    return {"ok": True}


@router.post("/transcripts/{video_id}/preguntar")
async def preguntar(video_id: str, request: Request):
    body = await request.json()
    pregunta = (body.get("pregunta") or "").strip()[:1000]
    historial = [m for m in (body.get("historial") or [])[-6:]
                 if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
    if not pregunta:
        return JSONResponse({"error": "Falta la pregunta."}, status_code=400)
    datos = transcripts.obtener(video_id)
    if not datos:
        return _no_encontrada(video_id)
    if not LLM_API_KEY:
        return _sin_key()

    fragmentos, modo = transcripts.fragmentos_para_pregunta(video_id, pregunta)

    async def generate():
        if not fragmentos:
            # Sin evidencia no se le pregunta al modelo: contestaría de memoria.
            yield sse.text("No aparece en lo transcrito de esta sesión. "
                           + ("Prueba con otras palabras o con un intervalo de tiempo "
                              "(por ejemplo, \"entre 00:20:00 y 00:30:00\")."
                              if modo != "rango" else "En ese intervalo no hay texto transcrito.")
                           + "\n\n_" + _texto_cobertura(datos) + "_")
            yield sse.DONE
            return
        yield sse.status(f"Revisando {len(fragmentos)} tramos de la transcripción...")
        contexto = (f"Sesión: \"{datos['sesion']['titulo']}\"\n"
                    f"Cobertura: {_texto_cobertura(datos)}\n"
                    f"Cómo se eligieron los fragmentos: {modo}\n\n"
                    f"FRAGMENTOS (transcripción automática):\n{transcripts.como_texto(fragmentos)}")
        messages = ([{"role": "system", "content": QA_PROMPT}]
                    + historial
                    + [{"role": "user", "content": f"{contexto}\n\nPREGUNTA: {pregunta}"}])
        async for kind, payload in llm.stream(messages, max_tokens=1500, temperature=0.2):
            if kind == "text":
                yield sse.text(payload)
            elif kind == "status":
                yield sse.status(payload)
            else:
                yield sse.error(payload)
                return
        yield sse.event({"fragmentos": [{"ts": f["ts"], "start_s": f["start_s"]} for f in fragmentos]})
        yield sse.DONE

    return StreamingResponse(sse.vigilar_desconexion(request, generate()),
                             media_type="text/event-stream")


@router.post("/transcripts/{video_id}/informe")
async def informe(video_id: str, request: Request):
    """Informe final de la sesión con lo transcrito. Se guarda al terminar."""
    datos = transcripts.obtener(video_id)
    if not datos:
        return _no_encontrada(video_id)
    if not LLM_API_KEY:
        return _sin_key()
    segs = datos["segmentos"]

    async def generate():
        if not segs:
            yield sse.error("Todavía no hay texto transcrito en esta sesión.")
            return
        cobertura = _texto_cobertura(datos)
        completo = transcripts.como_texto(segs)

        if len(completo) <= MAX_CHARS_INFORME_DIRECTO:
            material = f"TRANSCRIPCIÓN COMPLETA:\n{completo}"
        else:
            # Map: notas por bloque de ~15 min, con sus marcas de tiempo.
            bloques: list[list[dict]] = []
            for s in segs:
                if not bloques or s["start_s"] - bloques[-1][0]["start_s"] >= SEGUNDOS_POR_BLOQUE:
                    bloques.append([])
                bloques[-1].append(s)
            notas = []
            for i, b in enumerate(bloques, 1):
                yield sse.status(f"Leyendo la sesión: tramo {i} de {len(bloques)} "
                                 f"({b[0]['ts']} – {transcripts.fmt_ts(b[-1]['end_s'])})...")
                try:
                    nota = await _recolectar(
                        [{"role": "system", "content": NOTAS_PROMPT},
                         {"role": "user", "content": transcripts.como_texto(b)}], 900)
                except Exception as e:
                    logger.warning("informe %s: falló el tramo %d: %s", video_id, i, e)
                    nota = (f"- [{b[0]['ts']}] (No se pudo procesar este tramo: {e}. "
                            "Su contenido no está en este informe.)")
                notas.append(f"### Tramo {b[0]['ts']} – {transcripts.fmt_ts(b[-1]['end_s'])}\n{nota}")
            material = "NOTAS POR TRAMO (sacadas de la transcripción):\n\n" + "\n\n".join(notas)

        yield sse.status("Armando el informe...")
        messages = [
            {"role": "system", "content": INFORME_PROMPT},
            {"role": "user", "content": (f"Sesión: \"{datos['sesion']['titulo']}\"\n"
                                         f"Cobertura: {cobertura}\n\n{material}")},
        ]
        texto = []
        async for kind, payload in llm.stream(messages, max_tokens=6000, temperature=0.2):
            if kind == "text":
                texto.append(payload)
                yield sse.text(payload)
            elif kind == "status":
                yield sse.status(payload)
            else:
                yield sse.error(payload)
                return
        if texto:
            transcripts.guardar_informe(video_id, "".join(texto))
        yield sse.DONE

    return StreamingResponse(sse.vigilar_desconexion(request, generate()),
                             media_type="text/event-stream")
