"""Helpers para formatear eventos Server-Sent Events."""
import asyncio
import json


def event(payload: dict) -> str:
    """Serializa un dict como evento SSE."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def text(delta: str) -> str:
    """Fragmento de texto del modelo."""
    return event({"text": delta})


def status(msg: str) -> str:
    """Mensaje de progreso mostrado en la UI."""
    return event({"status": msg})


def error(msg: str) -> str:
    """Error mostrado en la UI."""
    return event({"error": msg})


DONE = "data: [DONE]\n\n"

# Línea de comentario SSE (":" al inicio) — EventSource la ignora, pero al
# forzar un intento real de escritura al socket ayuda a que el servidor
# detecte una conexión muerta cuando is_disconnected() no se entera sola
# (ver routers/live.py).
PING = ": ping\n\n"


async def vigilar_desconexion(request, gen, ping_cada: float = 5.0):
    """
    Reemite los eventos (ya formateados) de `gen` y corta si el cliente se fue.

    Mismo patrón que /live/transcribe (ver routers/live.py): is_disconnected()
    solo se entera del corte si se intenta escribir al socket, así que en cada
    espera de más de `ping_cada` segundos se manda un PING. Cuando el cliente
    cancela (botón detener, cerrar la ventana) se cierra `gen` explícitamente:
    eso dispara sus finally y corta las llamadas al LLM y al scraper en vez de
    dejarlas terminar para nadie.
    """
    pendiente = None
    try:
        while True:
            if pendiente is None:
                pendiente = asyncio.ensure_future(gen.__anext__())
            try:
                ev = await asyncio.wait_for(asyncio.shield(pendiente), timeout=ping_cada)
            except asyncio.TimeoutError:
                if await request.is_disconnected():
                    return
                yield PING
                continue
            except StopAsyncIteration:
                pendiente = None
                return
            pendiente = None
            yield ev
            if await request.is_disconnected():
                return
    finally:
        if pendiente is not None and not pendiente.done():
            pendiente.cancel()
            try:
                await pendiente
            except BaseException:
                pass
        elif pendiente is not None and not pendiente.cancelled():
            # Terminó (StopAsyncIteration) mientras se esperaba el ping:
            # sin leer la excepción, asyncio la loguea como "Task exception
            # was never retrieved" (visto el 15/10/2026 en un expediente).
            pendiente.exception()
        await gen.aclose()
