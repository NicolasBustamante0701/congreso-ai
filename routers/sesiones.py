"""Rutas de sesiones: listado de videos, transcripción y resumen."""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from config import GROQ_API_KEY, LLM_API_KEY, logger, static_file
from scraper import fetch_videos_youtube, get_yt_captions, transcribe_with_whisper
from services import llm, sse
from services.prompt_registry import build_sesion_prompt

router = APIRouter()

SESION_SYSTEM = (
    "Eres Diana, experta en análisis parlamentario del Congreso del Perú. "
    "Analizas transcripts de sesiones y los conviertes en resúmenes ejecutivos con tablas."
)


@router.get("/sessions", response_class=HTMLResponse)
async def sessions_page():
    return static_file("sessions.html")


@router.get("/sesiones/cookies-status")
async def sesiones_cookies_status():
    from scraper import COOKIE_PATHS, _get_cookie_path

    path = _get_cookie_path()
    return {"ok": bool(path), "path": path, "search_paths": COOKIE_PATHS}


@router.get("/sesiones/videos")
async def sesiones_videos():
    return await fetch_videos_youtube(limit=15)


async def _stream_resumen(titulo: str, texto: str):
    """Genera el resumen de un transcript y lo emite como SSE."""
    messages = [
        {"role": "system", "content": SESION_SYSTEM},
        {"role": "user", "content": build_sesion_prompt(titulo, texto)},
    ]
    async for kind, payload in llm.stream(messages, max_tokens=3000, temperature=0.3):
        if kind == "text":
            yield sse.text(payload)
        elif kind == "status":
            yield sse.status(payload)
        else:
            yield sse.error(payload)
            return
    yield sse.DONE


@router.post("/sesiones/resumir")
async def sesiones_resumir(request: Request):
    body = await request.json()
    video_id = body.get("video_id", "")
    titulo = body.get("titulo", "este video")
    en_vivo = body.get("en_vivo", False)

    async def generate():
        if not video_id:
            yield sse.error("Falta el ID del video")
            return

        # ── Fase 1: subtítulos de YouTube ─────────────────────────
        yield sse.status("Buscando subtítulos en YouTube...")
        loop = asyncio.get_running_loop()
        tr = await loop.run_in_executor(None, get_yt_captions, video_id)

        # ── Fase 2: Whisper si no hay subtítulos ──────────────────
        if not tr:
            if not GROQ_API_KEY:
                yield sse.error("No hay subtítulos disponibles para este video.")
                return

            minutes = 5 if en_vivo else 10
            label = (f"los últimos {minutes} min del stream en vivo" if en_vivo
                     else f"los primeros {minutes} min")
            yield sse.status(
                f"No hay subtítulos. Descargando audio ({label})... esto toma ~2 minutos."
            )

            try:
                tr = await loop.run_in_executor(
                    None, transcribe_with_whisper, video_id, GROQ_API_KEY, minutes
                )
            except Exception as e:
                logger.exception("Whisper falló para %s", video_id)
                yield sse.error(f"No se pudo transcribir el audio: {str(e)[:200]}")
                return
            if not tr.get("ok"):
                yield sse.error(tr.get("error", "No se pudo transcribir el audio."))
                return
            yield sse.status(f"Audio transcrito. Analizando ({tr.get('nota', '')})...")
        else:
            yield sse.status("Subtítulos obtenidos. Analizando sesión...")

        # El frontend usa esto para ofrecer la descarga del transcript crudo.
        yield sse.event({
            "transcript_raw": tr["text"],
            "video_url": f"https://www.youtube.com/watch?v={video_id}",
            "video_titulo": titulo,
        })

        async for ev in _stream_resumen(titulo, tr["text"]):
            yield ev

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/sesiones/resumir-texto")
async def sesiones_resumir_texto(request: Request):
    """Resume un transcript que el usuario pegó manualmente."""
    body = await request.json()
    texto = body.get("texto", "").strip()
    titulo = body.get("titulo", "esta sesión")

    async def generate():
        if not texto:
            yield sse.error("No hay texto para resumir.")
            return
        if not LLM_API_KEY:
            yield sse.error("Falta la API key del proveedor de chat activo.")
            return

        yield sse.status("Analizando transcripción...")
        async for ev in _stream_resumen(titulo, texto):
            yield ev

    return StreamingResponse(generate(), media_type="text/event-stream")
