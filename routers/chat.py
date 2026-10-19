"""Endpoint /chat — delega toda la lógica en ChatOrchestrator."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from config import LLM_API_KEY, LLM_PROVIDER
from services import sse
from services.orchestrator import ChatOrchestrator

router = APIRouter()


@router.post("/chat")
async def chat(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "El pedido no es JSON válido."}, status_code=400)
    messages = body.get("messages") if isinstance(body, dict) else None
    # Sin pregunta no hay nada que responder: antes igual se llamaba al
    # modelo, que contestaba con una lista de proyectos que nadie pidió.
    if (not isinstance(messages, list) or not messages
            or not all(isinstance(m, dict) for m in messages)
            or not str(messages[-1].get("content") or "").strip()):
        return JSONResponse({"error": "Falta la pregunta."}, status_code=400)

    if not LLM_API_KEY:
        async def err():
            yield sse.error(
                f"No hay una API key de {LLM_PROVIDER.capitalize()} configurada. "
                "Abre Ajustes (abajo a la izquierda) → Inteligencia artificial y pega una."
            )
        return StreamingResponse(err(), media_type="text/event-stream")

    orchestrator = ChatOrchestrator(messages, extension=body.get("extension"))
    return StreamingResponse(sse.vigilar_desconexion(request, orchestrator.run()),
                             media_type="text/event-stream")
