"""Ajustes de IA: estado del proveedor y prueba de una API key antes de guardarla.

La key la guarda Electron (cifrada, en la máquina del usuario) y se la pasa a
este proceso como variable de entorno al arrancarlo — ver aiEnv() en main.js.
Acá solo se informa qué hay configurado y se valida una key nueva.
"""
from fastapi import APIRouter, Request
from openai import AsyncOpenAI

from config import GROQ_API_KEY, LLM_API_KEY, LLM_PROVIDER, PROVIDERS
from services import llm
from services.observability import timed

router = APIRouter()


@router.get("/settings/ai")
async def ai_status():
    return {
        "provider": LLM_PROVIDER,
        "ready": bool(LLM_API_KEY),
        "transcripcion": bool(GROQ_API_KEY),
    }


@router.post("/settings/ai/test")
async def test_key(request: Request):
    body = await request.json()
    provider = str(body.get("provider") or "").lower()
    api_key = str(body.get("api_key") or "").strip()
    if provider not in PROVIDERS:
        return {"ok": False, "error": "Proveedor desconocido."}
    if not api_key or len(api_key) > 300:
        return {"ok": False, "error": "Pegá una API key."}

    client = AsyncOpenAI(api_key=api_key, base_url=PROVIDERS[provider]["base_url"],
                         timeout=15, max_retries=0)
    try:
        with timed("Prueba de API key", provider=provider):
            await client.models.list()
        return {"ok": True}
    except Exception as e:
        if llm.is_auth_error(e):
            nombre = provider.capitalize()
            return {"ok": False, "error": f"La key no es válida para {nombre}. "
                                          f"Revisá que la copiaste completa y que es de {nombre}."}
        return {"ok": False, "error": llm.friendly_error(e, provider=provider)}
    finally:
        await client.close()
