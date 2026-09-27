"""
Capa sobre el cliente de chat: cliente, clasificación de errores y streaming
con reintento sobre rate limit.

Es agnóstica del proveedor: habla siempre por el SDK de `openai` contra el
endpoint que fije config.LLM_PROVIDER — hoy Gemini, y también sirven Groq,
Cerebras y OpenAI, que exponen el mismo formato de chat completions. Solo
cambian la URL base, la key y los modelos (ver config.py).

OJO, no confundir con Groq-el-de-Whisper: la transcripción de audio
(live_transcriber.py y scraper.py) habla con Groq directo, con su propio SDK y
su propia GROQ_API_KEY, y no pasa por acá. Este módulo se llamaba `groq.py`
justamente por esa confusión, de cuando Groq era además el proveedor de chat.

Los límites de cada proveedor son distintos (Groq: tokens por minuto; Gemini:
requests por día en el tier gratis) — parse_retry_seconds/is_rate_limit
reconocen los formatos de error de ambos.
"""
import asyncio
import re

from openai import AsyncOpenAI, Timeout

from config import (
    FIRST_TOKEN_TIMEOUT,
    LLM_CONNECT_TIMEOUT,
    LLM_FALLBACKS,
    LLM_PROVIDER,
    LLM_READ_TIMEOUT,
    PROVIDERS,
    ROUTER_TIMEOUT,
    logger,
)
from services.observability import timed

_PROVIDER_LABEL = LLM_PROVIDER.capitalize()

RETRY_FALLBACK_SECONDS = 12.0
MAX_ATTEMPTS = 3

# Con un proveedor de respaldo disponible no vale la pena dejar al usuario
# mirando la pantalla un minuto por un rate limit: más que esto, se cambia.
MAX_RATE_LIMIT_WAIT_WITH_FALLBACK = 8.0

_TIMEOUT = Timeout(LLM_READ_TIMEOUT, connect=LLM_CONNECT_TIMEOUT)


def _label(provider: str) -> str:
    return provider.capitalize()


def _extra_params(provider: str) -> dict:
    # Los modelos "flash" de Gemini piensan por default y, a través del shim
    # de OpenAI, ese razonamiento se cuela como texto normal en la respuesta.
    return {"reasoning_effort": "low"} if provider == "gemini" else {}


def model_for(provider: str, role: str) -> str:
    return PROVIDERS[provider]["router_model" if role == "router" else "main_model"]


def provider_chain() -> list[str]:
    """Proveedor activo primero, después los de respaldo con key cargada."""
    return [LLM_PROVIDER] + LLM_FALLBACKS


# Un cliente por proveedor para todo el proceso, así se reusa el pool de
# conexiones que abre warmup(). Async: el cliente síncrono bloqueaba el event
# loop durante toda la generación y congelaba cualquier otro request. Sin
# reintentos del SDK: los reintentos y el cambio de proveedor los hace
# stream() acá abajo, con visibilidad para el usuario.
_clients: dict[str, AsyncOpenAI] = {}


def get_client(provider: str = LLM_PROVIDER) -> AsyncOpenAI:
    if provider not in _clients:
        cfg = PROVIDERS[provider]
        _clients[provider] = AsyncOpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"],
                                         timeout=_TIMEOUT, max_retries=0)
    return _clients[provider]


async def warmup() -> None:
    """
    Abre la conexión con el proveedor antes de que el usuario escriba: la
    primera request paga DNS + TCP + TLS (1-3.5s medido contra Gemini).
    models.list() no consume cuota de generación. Falla en silencio: es una
    optimización, no un requisito.
    """
    try:
        with timed("LLM warmup", provider=LLM_PROVIDER):
            await get_client().models.list()
    except Exception:
        pass


# ── Clasificación de errores ─────────────────────────────────────────────────

def parse_retry_seconds(e) -> float:
    """Extrae los segundos de espera del mensaje de rate limit (Groq o Gemini)."""
    s = str(e)
    # Groq: "Please try again in 2.5s" / Gemini: "Please retry in 2.5s"
    m = re.search(r"(?:try again|retry) in ([0-9.]+)s", s, re.IGNORECASE)
    if m:
        return float(m.group(1)) + 0.5
    # "try again in 750ms"
    m = re.search(r"(?:try again|retry) in ([0-9.]+)ms", s, re.IGNORECASE)
    if m:
        return float(m.group(1)) / 1000 + 0.5
    # "try again in 1m30s"
    m = re.search(r"(?:try again|retry) in (\d+)m(\d+(?:\.\d+)?)s", s, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2)) + 0.5
    # Gemini: retryDelay estilo protobuf, ej. "retryDelay': '17s'"
    m = re.search(r"retryDelay[\"']?\s*[:=]\s*[\"']?([0-9.]+)s", s, re.IGNORECASE)
    if m:
        return float(m.group(1)) + 0.5
    return RETRY_FALLBACK_SECONDS


def is_rate_limit(e) -> bool:
    s = str(e).lower()
    # Un 402 (cuenta sin saldo, ej. Cerebras) trae "quota" en el texto pero
    # esperar no lo arregla: reintentar solo sumaba 24 s antes del error.
    if "error code: 402" in s or "payment_required" in s:
        return False
    return any(x in s for x in ("rate limit", "429", "tokens per", "quota", "per day",
                                 "resource_exhausted", "resource exhausted"))


# Variable de entorno que hay que completar para cada proveedor, para poder
# decirle al usuario exactamente qué le falta en vez de un "error genérico".
_ENV_KEY = {
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def is_auth_error(e) -> bool:
    """
    Key ausente, inválida o sin permisos. Reintentar no sirve de nada.

    Cada proveedor lo reporta distinto — Groq: 401 'invalid_api_key';
    Gemini: 400 'Please pass a valid API key' con status INVALID_ARGUMENT
    (sí, 400, no 401) — así que se reconocen por texto y no solo por código.
    """
    s = str(e).lower()
    marcas = ("invalid_api_key", "invalid api key", "valid api key",
              "api key not valid", "api_key_invalid", "unauthorized",
              "permission_denied", "missing credentials")
    if any(m in s for m in marcas):
        return True
    return "error code: 401" in s or "error code: 403" in s


def is_tool_format_error(e) -> bool:
    """El modelo generó un tool_call malformado — conviene reintentar sin tools."""
    # Un 400 por key inválida NO es un tool_call malformado. Sin esta guarda,
    # Gemini (que responde 400 a una key mala) caía acá y el orquestador
    # reintentaba con el modelo grande: dos llamadas fallidas y un mensaje
    # final de "problema al conectar" que no decía nada del verdadero motivo.
    if is_auth_error(e):
        return False
    s = str(e)
    return "tool_use_failed" in s or "failed_generation" in s or "400" in s


def _es_cuota_diaria(e) -> bool:
    s = str(e).lower()
    return "per day" in s or "tpd" in s or "perday" in s


def friendly_error(e, provider: str = LLM_PROVIDER) -> str:
    """Traduce la excepción a un mensaje que se le puede mostrar al usuario."""
    s = str(e).lower()
    if is_auth_error(e):
        var = _ENV_KEY.get(provider, "la API key")
        return (
            f"La API key de {_label(provider)} no es válida o falta. "
            f"Abrí tu perfil (abajo a la izquierda) → Ajustes de IA y pegá una válida. "
            f"Si corrés desde el código, también podés ponerla en {var} del archivo .env."
        )
    if _es_cuota_diaria(e):
        m = re.search(r"try again in ([0-9hms.]+)", s)
        cuando = "en un rato"
        if m:
            mins = re.search(r"(\d+)m", m.group(1))
            cuando = f"en ~{mins.group(1)} min" if mins else f"en {m.group(1)}"
        return f"Llegamos al límite de tokens por ahora. Vuelve a intentar {cuando}."
    if is_rate_limit(e):
        return "Muchas consultas muy rápido. Espera unos segundos y vuelve a intentarlo."
    if "timeout" in type(e).__name__.lower() or "timed out" in s:
        return f"{_label(provider)} tardó demasiado en responder. Intentá de nuevo en un momento."
    return "Hubo un problema al conectar. Intentá de nuevo."


# ── Llamadas ─────────────────────────────────────────────────────────────────

async def complete_router(messages, *, tools, tool_choice="required", max_tokens=512,
                          temperature=0.2):
    """
    Llamada sin streaming del router (Fase 1), con respaldo de proveedor.
    Devuelve el choice. Un tool_call malformado se re-lanza sin probar otro
    proveedor: el orquestador ya sabe responder sin herramientas.
    """
    last_exc = None
    chain = provider_chain()
    for i, provider in enumerate(chain):
        model = model_for(provider, "router")
        timeout = ROUTER_TIMEOUT if i < len(chain) - 1 else LLM_READ_TIMEOUT
        try:
            with timed("LLM router", provider=provider, model=model):
                resp = await get_client(provider).chat.completions.create(
                    model=model, messages=messages, tools=tools, tool_choice=tool_choice,
                    max_tokens=max_tokens, temperature=temperature, stream=False,
                    timeout=timeout,
                )
            return resp.choices[0]
        except Exception as e:
            last_exc = last_exc or e
            if is_tool_format_error(e):
                raise
    # Se reporta el error del proveedor principal: es el que el usuario
    # configuró, y el de un respaldo sin saldo confundiría más que ayudar.
    raise last_exc


async def _deltas(provider, messages, *, role, max_tokens, temperature):
    stream = await get_client(provider).chat.completions.create(
        model=model_for(provider, role),
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        stream=True,
        **_extra_params(provider),
    )
    try:
        async for chunk in stream:
            if not chunk.choices:
                continue
            if chunk.choices[0].finish_reason == "length":
                logger.warning("Respuesta de %s cortada por max_tokens=%d", provider, max_tokens)
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
    finally:
        await stream.close()


async def stream(messages, *, role="main", max_tokens=2048, temperature=0.4):
    """
    Respuesta en streaming con reintento sobre rate limit y cambio de
    proveedor si el activo falla, se cuelga o se queda sin cuota.

    Emite tuplas ("text", delta), ("status", mensaje) o ("error", mensaje).
    Solo se cambia de proveedor si todavía no salió texto: a mitad de una
    respuesta, empezar otra de cero la duplicaría en pantalla.
    """
    chain = provider_chain()
    primary_exc = None
    for i, provider in enumerate(chain):
        if i > 0:
            yield ("status", f"{_label(chain[i - 1])} no respondió, probando con {_label(provider)}...")
        hay_respaldo = i < len(chain) - 1
        for attempt in range(MAX_ATTEMPTS):
            emitted = False
            deltas = _deltas(provider, messages, role=role,
                             max_tokens=max_tokens, temperature=temperature)
            try:
                with timed("LLM stream", provider=provider, model=model_for(provider, role),
                           intento=attempt + 1, max_tokens=max_tokens):
                    try:
                        first = await asyncio.wait_for(
                            anext(deltas), FIRST_TOKEN_TIMEOUT if hay_respaldo else None)
                    except StopAsyncIteration:
                        return
                    emitted = True
                    yield ("text", first)
                    async for delta in deltas:
                        yield ("text", delta)
                return
            except Exception as e:
                if provider == chain[0]:
                    primary_exc = e
                if emitted:
                    logger.error("Stream de %s cortado a mitad de la respuesta: %s", provider, e)
                    yield ("error", "La respuesta se cortó a mitad de camino. Intentá de nuevo.")
                    return
                if is_rate_limit(e) and not _es_cuota_diaria(e) and attempt < MAX_ATTEMPTS - 1:
                    wait = parse_retry_seconds(e)
                    if not hay_respaldo or wait <= MAX_RATE_LIMIT_WAIT_WITH_FALLBACK:
                        logger.warning("%s rate limit (intento %d/%d), esperando %.1fs",
                                       provider, attempt + 1, MAX_ATTEMPTS, wait)
                        yield ("status", f"Límite de {_label(provider)}, reintentando en {wait:.0f}s...")
                        await asyncio.sleep(wait)
                        continue
                break
            finally:
                await deltas.aclose()

    logger.error("LLM falló en todos los proveedores (%s)", ", ".join(chain))
    yield ("error", friendly_error(primary_exc, provider=chain[0]))
