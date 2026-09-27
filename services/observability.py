"""Id por request y medición de llamadas externas (LLM, scraping, Whisper)."""
import asyncio
import time
import uuid
from contextlib import contextmanager

from config import logger, request_id_var

# Llamadas externas más lentas que esto se registran como WARNING.
SLOW_SECONDS = 15.0


class RequestContextMiddleware:
    """
    ASGI puro (no BaseHTTPMiddleware, que bufferea las StreamingResponse): fija
    el id del request antes de llamar a la app, así las tareas que Starlette
    crea para el stream SSE lo heredan en su contexto.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        token = request_id_var.set(uuid.uuid4().hex[:8])
        path = scope.get("path", "")
        method = scope.get("method", "")
        status = None
        start = time.monotonic()

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception("%s %s reventó", method, path)
            raise
        finally:
            if not path.startswith("/static"):
                logger.info("%s %s -> %s en %.2fs", method, path, status,
                            time.monotonic() - start)
            request_id_var.reset(token)


@contextmanager
def timed(what: str, **ctx):
    """Registra duración y resultado de una llamada externa. Re-lanza la excepción."""
    detalle = " ".join(f"{k}={v}" for k, v in ctx.items())
    start = time.monotonic()
    try:
        yield
    except (GeneratorExit, asyncio.CancelledError):
        logger.info("%s cancelado tras %.2fs (cliente desconectado) %s", what,
                    time.monotonic() - start, detalle)
        raise
    except BaseException as e:
        logger.warning("%s FALLÓ tras %.2fs %s: %s: %s", what, time.monotonic() - start,
                       detalle, type(e).__name__, str(e)[:300])
        raise
    dur = time.monotonic() - start
    nivel = logger.warning if dur > SLOW_SECONDS else logger.info
    nivel("%s ok en %.2fs %s", what, dur, detalle)
