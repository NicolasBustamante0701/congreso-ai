"""
Punto de entrada de la API.

La lógica vive en:
  routers/   — endpoints HTTP, uno por área funcional
  services/  — orquestación del chat, cliente LLM, PDFs, Word
  prompts/   — prompts del sistema en Markdown
  config.py  — rutas, credenciales y modelos
"""
import asyncio
import os
import signal
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import (
    ALLOWED_ORIGIN,
    LLM_FALLBACKS,
    LLM_PROVIDER,
    LOG_FILE,
    PORT,
    STATIC_DIR,
    logger,
)
from routers import (
    chat,
    expediente,
    export,
    investigacion,
    live,
    pages,
    pdfs,
    sesiones,
    settings,
    transcripts,
)
from services import llm, normas, seguimiento
from services.observability import RequestContextMiddleware


def _vigilar_padre() -> None:
    """
    Si Electron muere sin cerrar el server (crash, kill -9, la app forzada a
    salir), el server quedaba huérfano para siempre: escuchando en el puerto
    y con sus ffmpeg corriendo. main.js pasa su PID en DIANA_PARENT_PID; un
    hilo del sistema (no una tarea de asyncio, que depende del event loop)
    comprueba cada 3 s que el padre siga siendo ese y, si no, termina el
    grupo de procesos entero — el server es líder de grupo (spawn detached),
    así que caen también los ffmpeg.
    """
    padre = os.getenv("DIANA_PARENT_PID")
    if not padre or not padre.isdigit() or os.name != "posix":
        return
    padre = int(padre)

    def bucle():
        while True:
            time.sleep(3)
            if os.getppid() != padre:
                logger.warning("Electron (pid %s) ya no está: el server se apaga solo", padre)
                try:
                    os.killpg(os.getpgrp(), signal.SIGTERM)
                finally:
                    time.sleep(5)
                    os._exit(0)

    threading.Thread(target=bucle, name="vigilar-padre", daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Diana arrancando: proveedor=%s fallbacks=%s log=%s",
                LLM_PROVIDER, LLM_FALLBACKS or "ninguno", LOG_FILE)
    # En segundo plano: abrir la conexión tarda hasta 3.5s y el servidor tiene
    # que estar escuchando ya — Electron espera contra el puerto (ver main.js).
    warmup = asyncio.create_task(llm.warmup())
    _vigilar_padre()
    # Revisión periódica de los proyectos seguidos, solo mientras la app
    # está abierta (ver services/seguimiento.py).
    revision = asyncio.create_task(seguimiento.bucle_automatico())
    # La Constitución y los Reglamentos se bajan una vez (~20 MB) y quedan
    # guardados: así la primera pregunta de procedimiento no espera la descarga.
    normas_listas = asyncio.create_task(normas.preparar())
    yield
    warmup.cancel()
    revision.cancel()
    normas_listas.cancel()
    logger.info("Diana apagándose")


def create_app() -> FastAPI:
    app = FastAPI(title="Diana", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ALLOWED_ORIGIN],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    for module in (pages, chat, sesiones, live, transcripts, investigacion, pdfs, export, expediente, settings):
        app.include_router(module.router)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    # log_config=None: uvicorn no instala sus propios handlers y sus errores
    # ("Exception in ASGI application") llegan al log a archivo de config.py.
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_config=None)
