"""
Punto de entrada de la API.

La lógica vive en:
  routers/   — endpoints HTTP, uno por área funcional
  services/  — orquestación del chat, cliente LLM, PDFs, Word
  prompts/   — prompts del sistema en Markdown
  config.py  — rutas, credenciales y modelos
"""
import asyncio
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
from routers import chat, expediente, export, live, pages, pdfs, sesiones, settings
from services import llm
from services.observability import RequestContextMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Diana arrancando: proveedor=%s fallbacks=%s log=%s",
                LLM_PROVIDER, LLM_FALLBACKS or "ninguno", LOG_FILE)
    # En segundo plano: abrir la conexión tarda hasta 3.5s y el servidor tiene
    # que estar escuchando ya — Electron espera contra el puerto (ver main.js).
    warmup = asyncio.create_task(llm.warmup())
    yield
    warmup.cancel()
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

    for module in (pages, chat, sesiones, live, pdfs, export, expediente, settings):
        app.include_router(module.router)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    # log_config=None: uvicorn no instala sus propios handlers y sus errores
    # ("Exception in ASGI application") llegan al log a archivo de config.py.
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_config=None)
