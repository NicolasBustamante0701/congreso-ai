"""Rutas de sesión en vivo: transcripción en tiempo real y análisis incremental."""
import asyncio

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from config import GROQ_API_KEY, logger, static_file
from live_transcriber import stream_transcription
from services import llm, sse, transcripts
from services.prompt_registry import LIVE_ANALYSIS_PROMPT

router = APIRouter()

# El frontend manda solo el tramo NUEVO desde el último análisis (no todo el
# acumulado — ver live.js runAnalysis), así que esto es un tope de seguridad
# para un tramo inusualmente largo, no el recorte principal como antes.
LIVE_EXCERPT_CHARS = 4000

NO_BUFFER = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@router.get("/live", response_class=HTMLResponse)
async def live_page():
    return static_file("live.html")


@router.get("/live/transcribe")
async def live_transcribe(request: Request,
                           video_id: str = Query(..., description="ID del video de YouTube"),
                           start: int = Query(0, ge=0, description="Segundo del video donde arrancar"),
                           titulo: str = Query("", max_length=300)):
    """
    SSE que emite líneas de transcripción en tiempo real y las guarda.

    Cada ventana se persiste apenas llega (services/transcripts.py): si se
    cierra la app o se corta la conexión, lo ya transcrito queda.
    """
    estado = {"offset": 0, "en_vivo": False, "persistir": True}

    def procesar(item: dict) -> dict | None:
        """Guarda lo que corresponda y devuelve el evento para el cliente (o None)."""
        if "info" in item:
            estado["en_vivo"] = item["info"]["en_vivo"]
            try:
                if estado["en_vivo"]:
                    # En vivo, ffmpeg cuenta desde 0 en cada captura: se sigue
                    # la numeración desde lo último guardado para que una
                    # reconexión no pise lo anterior.
                    estado["offset"] = transcripts.offset_captura(video_id)
                transcripts.iniciar_sesion(video_id, titulo, en_vivo=estado["en_vivo"])
            except Exception:
                logger.exception("no se pudo iniciar la sesión guardada de %s", video_id)
                estado["persistir"] = False
            return {"info": {"en_vivo": estado["en_vivo"], "offset": estado["offset"]}}

        v = item.pop("ventana", None)
        off = estado["offset"]
        if v is not None and estado["persistir"]:
            try:
                transcripts.guardar_ventana(video_id, v["start_s"] + off, v["end_s"] + off,
                                            item.get("text", ""), v["estado"])
            except Exception:
                # La transcripción sigue en pantalla aunque no se pueda guardar.
                logger.exception("no se pudo guardar la ventana %s de %s", v, video_id)
        if "text" in item:
            item["elapsed"] = item["elapsed"] + off
            item["timestamp"] = transcripts.fmt_ts(item["elapsed"])
            return item
        return item if ("status" in item or "error" in item) else None

    async def generate():
        if not GROQ_API_KEY:
            yield sse.error("Falta la key de Groq para transcribir. Pegala en Ajustes de IA.")
            return
        if not video_id:
            yield sse.error("Falta el parámetro video_id")
            return
        gen = stream_transcription(video_id, GROQ_API_KEY, start_seconds=start)
        next_task = None
        try:
            while True:
                # Un chunk en vivo tarda ~10-12s en llegar (espera audio real
                # + Whisper). Si solo chequeáramos is_disconnected() DESPUÉS
                # de cada yield (como hacía la versión anterior), una
                # desconexión a mitad de esa espera no se notaba hasta que el
                # próximo chunk terminara solo — para un stream infinito eso
                # es "nunca": ffmpeg quedaba huérfano.
                #
                # Por eso sondeamos la desconexión cada 5s con wait_for,
                # PERO protegiendo la tarea de fondo con asyncio.shield para
                # que el timeout no la cancele — solo la cancelamos de verdad
                # si además confirmamos que el cliente se fue. Así next_task
                # nunca queda "corriendo a medias" cuando llamamos a
                # gen.aclose(): o lo esperamos hasta que termine solo, o lo
                # cancelamos explícitamente y esperamos esa cancelación antes
                # de cerrar el generador. Esto también cubre el caso de que
                # Starlette cancele esta corrutina desde afuera (por su propio
                # detector de desconexión): el finally de abajo limpia
                # next_task pase lo que pase, así que aclose() nunca choca
                # con un __anext__() todavía en vuelo (RuntimeError: aclose():
                # asynchronous generator is already running).
                #
                # OJO: is_disconnected() por sí sola NO alcanza acá — depende
                # de que uvicorn haya entregado un mensaje "http.disconnect"
                # al canal receive(), y mientras este generador está ocupado
                # esperando el próximo chunk (sin leer el socket para nada),
                # uvicorn nunca llega a notarlo y is_disconnected() sigue
                # devolviendo False para siempre aunque el socket ya esté
                # cerrado. Por eso mandamos un "ping" SSE en cada timeout:
                # fuerza un intento real de escritura al socket, y si está
                # muerto, ESO es lo que hace que Starlette detecte el corte
                # y cancele este generador (que ya sabemos manejar limpio).
                #
                # Esto es "mejor esfuerzo": en algunos entornos ni siquiera
                # esto garantiza una limpieza rápida (visto en pruebas). La
                # garantía real de que ffmpeg no queda corriendo para siempre
                # vive en stream_transcription() — un hilo de sistema
                # operativo con su propio temporizador, independiente de
                # todo lo de acá.
                if next_task is None:
                    next_task = asyncio.ensure_future(gen.__anext__())
                try:
                    item = await asyncio.wait_for(asyncio.shield(next_task), timeout=5)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield sse.PING
                    continue
                except StopAsyncIteration:
                    next_task = None
                    break
                next_task = None
                ev = procesar(item)
                if ev is not None:
                    yield sse.event(ev)
                if await request.is_disconnected():
                    break
        except Exception as e:
            logger.exception("live_transcribe falló para %s", video_id)
            yield sse.error(str(e))
        finally:
            if next_task is not None and not next_task.done():
                next_task.cancel()
                try:
                    await next_task
                except BaseException:
                    pass
            # Cierra el generador explícitamente: dispara el finally de
            # stream_transcription() que mata el proceso ffmpeg, sin depender
            # de que la cancelación por desconexión se propague sola.
            await gen.aclose()
            if estado["persistir"]:
                try:
                    transcripts.marcar_estado(video_id, "detenida")
                except Exception:
                    logger.exception("no se pudo cerrar la sesión guardada de %s", video_id)
        yield sse.DONE

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers=NO_BUFFER)


@router.post("/live/analyze")
async def live_analyze(request: Request):
    """
    Analiza un TRAMO NUEVO del transcript (no el acumulado completo — el
    frontend manda solo las líneas desde el último análisis, ver
    runAnalysis() en live.js) y arma un bloque de resumen fechado que el
    frontend agrega a la lista, sin reemplazar los anteriores.
    """
    body = await request.json()
    transcript = body.get("transcript", "").strip()
    titulo = body.get("titulo", "sesión en vivo")

    async def generate():
        if not transcript:
            yield sse.error("Sin transcripción aún.")
            return

        excerpt = transcript[-LIVE_EXCERPT_CHARS:]
        messages = [
            {"role": "system", "content": LIVE_ANALYSIS_PROMPT},
            {"role": "user", "content": f'Sesión: "{titulo}"\n\nTramo nuevo de la transcripción:\n{excerpt}'},
        ]
        async for kind, payload in llm.stream(messages, max_tokens=400, temperature=0.3):
            if kind == "text":
                yield sse.text(payload)
            elif kind == "status":
                yield sse.status(payload)
            else:
                yield sse.error(payload)
                return
        yield sse.DONE

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
