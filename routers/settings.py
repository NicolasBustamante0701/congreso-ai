"""Ajustes de IA: estado del proveedor y prueba de una API key antes de guardarla.

La key la guarda Electron (cifrada, en la máquina del usuario) y se la pasa a
este proceso como variable de entorno al arrancarlo — ver aiEnv() en main.js.
Acá solo se informa qué hay configurado y se valida una key nueva.
"""
import asyncio
import time

from fastapi import APIRouter, Request
from openai import AsyncOpenAI

from config import GROQ_API_KEY, LLM_API_KEY, LLM_PROVIDER, PROVIDERS
from services import llm
from services.observability import timed

router = APIRouter()


# Probar la key generando una respuesta mínima, no listando modelos: con un
# proyecto bloqueado por Google, la lista de modelos responde bien y la
# generación da 403 "Your project has been denied access" (18/10/2026). La
# pantalla decía "Funcionando" mientras todo lo respondía el respaldo.
_PRUEBA_CACHE: dict = {}
_PRUEBA_TTL = 10 * 60


def _mensaje_error(e: Exception, provider: str) -> str:
    texto = str(e)
    nombre = provider.capitalize()
    if "denied access" in texto or "PERMISSION_DENIED" in texto:
        return (f"{nombre} bloqueó el proyecto de esta key. Crea una key en un proyecto "
                "nuevo (en AI Studio: «Create API key in new project»).")
    if llm.is_auth_error(e):
        return f"La key no es válida para {nombre}. Revisa que la copiaste completa y que es de {nombre}."
    return llm.friendly_error(e, provider=provider)


async def _probar_key(provider: str, api_key: str) -> tuple[bool, str]:
    client = AsyncOpenAI(api_key=api_key, base_url=PROVIDERS[provider]["base_url"],
                         timeout=20, max_retries=0)
    try:
        with timed("Prueba de API key", provider=provider):
            await client.chat.completions.create(
                model=llm.model_for(provider, "router"), max_tokens=5,
                messages=[{"role": "user", "content": "Responde: ok"}])
        return True, ""
    except Exception as e:
        # Saturado o con límite de uso no es culpa de la key: funciona.
        if llm.is_rate_limit(e) or llm.is_overloaded(e):
            return True, ""
        return False, _mensaje_error(e, provider)
    finally:
        await client.close()


_PRUEBA_EN_CURSO: dict = {}


async def _probar_y_guardar(clave):
    funciona, error = await _probar_key(*clave)
    _PRUEBA_CACHE[clave] = (time.monotonic(), funciona, error)
    return funciona, error


@router.get("/settings/ai")
async def ai_status(probar: bool = False):
    """
    Sin `probar`, responde al instante con la última prueba conocida (o
    "funciona": null) y prueba la key en segundo plano: lo pide la app al
    arrancar y una espera de 2-3 s ahí reiniciaba el chat que el usuario ya
    había empezado (16/10/2026). Ajustes pide `probar=1` y espera el
    resultado real.
    """
    funciona, error = None, ""
    if LLM_API_KEY:
        clave = (LLM_PROVIDER, LLM_API_KEY)
        hit = _PRUEBA_CACHE.get(clave)
        if hit and time.monotonic() - hit[0] < _PRUEBA_TTL:
            funciona, error = hit[1], hit[2]
        else:
            tarea = _PRUEBA_EN_CURSO.get(clave)
            if tarea is None or tarea.done():
                tarea = _PRUEBA_EN_CURSO[clave] = asyncio.create_task(_probar_y_guardar(clave))
            if probar:
                funciona, error = await asyncio.shield(tarea)
    return {
        "provider": LLM_PROVIDER,
        "ready": bool(LLM_API_KEY),
        "funciona": funciona,
        "error": error,
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
        return {"ok": False, "error": "Pega una API key."}

    ok, error = await _probar_key(provider, api_key)
    return {"ok": True} if ok else {"ok": False, "error": error}


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

    # ffmpeg: en el .app viaja dentro del bundle (imageio-ffmpeg); sin él no
    # hay transcripción. Se ejecuta de verdad (-version), no solo se busca.
    from live_transcriber import ffmpeg_exe

    def _ffmpeg():
        import subprocess
        exe = ffmpeg_exe()
        try:
            r = subprocess.run([exe, "-version"], capture_output=True, timeout=10)
            linea = r.stdout.decode(errors="replace").splitlines()[0] if r.stdout else ""
            return {"ok": r.returncode == 0, "detalle": linea[:80] or f"código {r.returncode}", "ruta": exe}
        except Exception as e:
            return {"ok": False, "detalle": type(e).__name__, "ruta": exe}

    ffmpeg = await asyncio.to_thread(_ffmpeg)
    try:
        sesiones = transcripts.listar_sesiones()
        base = {"ok": True, "ruta": str(DB_FILE), "sesiones": len(sesiones)}
    except Exception as e:
        base = {"ok": False, "ruta": str(DB_FILE), "detalle": str(e)[:160]}
    return {
        "comprobado": ahora_lima().strftime("%d/%m/%Y %H:%M:%S"),
        "ia": {"proveedor": LLM_PROVIDER, "configurada": bool(LLM_API_KEY)},
        "transcripcion": bool(GROQ_API_KEY),
        "ffmpeg": ffmpeg,
        "fuentes": resultados,
        "base_de_datos": base,
        "log": str(LOG_FILE),
    }
