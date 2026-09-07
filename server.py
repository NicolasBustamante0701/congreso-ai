"""
Punto de entrada de la API.

La lógica vive en:
  routers/   — endpoints HTTP, uno por área funcional
  services/  — orquestación del chat, cliente LLM, PDFs, Word
  prompts/   — prompts del sistema en Markdown
  config.py  — rutas, credenciales y modelos
"""
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import ALLOWED_ORIGIN, PORT, STATIC_DIR
from routers import chat, expediente, export, live, pages, pdfs, sesiones
from services import llm


def create_app() -> FastAPI:
    app = FastAPI(title="Diana")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ALLOWED_ORIGIN],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    for module in (pages, chat, sesiones, live, pdfs, export, expediente):
        app.include_router(module.router)

    # En un hilo aparte: abrir la conexión tarda hasta 3.5s y el servidor tiene
    # que estar escuchando ya — Electron reintenta contra el puerto durante
    # 16s y si no responde muestra la pantalla de error (ver main.js).
    threading.Thread(target=llm.warmup, daemon=True).start()

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT)
