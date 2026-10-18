"""Endpoint /chat — delega toda la lógica en ChatOrchestrator."""
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from config import LLM_API_KEY, LLM_PROVIDER
from services import sse
from services.orchestrator import ChatOrchestrator

router = APIRouter()


@router.post("/chat")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])

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
