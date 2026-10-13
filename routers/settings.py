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


@router.get("/diagnostico")
async def diagnostico():
    """
    Qué funciona ahora mismo, para la pantalla de Ajustes → Diagnóstico.
    Las fuentes se prueban de verdad (un GET corto a cada portal) y se dice
    cuánto tardaron o por qué fallaron, con la hora de la comprobación.
    """
    import asyncio
    import time

    import scraper
    from config import DB_FILE, LOG_FILE, ahora_lima
    from services import transcripts

    fuentes = {
        "SPLEY (proyectos de ley)": f"{scraper.SPLEY_API}/periodo-parlamentario",
        "Agenda de las cámaras": scraper.AGENDA_COMUNICACIONES,
        "Portal del Congreso": scraper.CONGRESO + "/",
    }

    async def probar(nombre, url):
        t = time.monotonic()
        try:
            async with scraper._client() as c:
                c.timeout = 8
                r = await c.get(url)
            ok = r.status_code < 500
            return {"nombre": nombre, "ok": ok, "ms": int((time.monotonic() - t) * 1000),
                    "detalle": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"nombre": nombre, "ok": False, "ms": int((time.monotonic() - t) * 1000),
                    "detalle": type(e).__name__}

    resultados = await asyncio.gather(*(probar(n, u) for n, u in fuentes.items()))
    try:
        sesiones = transcripts.listar_sesiones()
        base = {"ok": True, "ruta": str(DB_FILE), "sesiones": len(sesiones)}
    except Exception as e:
        base = {"ok": False, "ruta": str(DB_FILE), "detalle": str(e)[:160]}
    return {
        "comprobado": ahora_lima().strftime("%d/%m/%Y %H:%M:%S"),
        "ia": {"proveedor": LLM_PROVIDER, "configurada": bool(LLM_API_KEY)},
        "transcripcion": bool(GROQ_API_KEY),
        "fuentes": resultados,
        "base_de_datos": base,
        "log": str(LOG_FILE),
    }
