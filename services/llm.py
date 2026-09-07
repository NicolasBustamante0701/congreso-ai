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

from openai import OpenAI

from config import LLM_API_KEY, LLM_BASE_URL, LLM_PROVIDER, MAIN_MODEL, logger

_PROVIDER_LABEL = LLM_PROVIDER.capitalize()

RETRY_FALLBACK_SECONDS = 12.0
MAX_ATTEMPTS = 3

# Los modelos "flash" de Gemini piensan por default y, a través del shim de
# OpenAI, ese razonamiento se cuela como texto normal en la respuesta (se ve
# como el modelo "pensando en voz alta" antes de la respuesta real). Groq/
# OpenAI no tienen este comportamiento con sus modelos actuales, así que el
# parámetro solo se manda cuando el proveedor activo es Gemini.
_EXTRA_PARAMS = {"reasoning_effort": "low"} if LLM_PROVIDER == "gemini" else {}


# Un solo cliente para todo el proceso. Antes se construía uno nuevo por turno
# (ChatOrchestrator.__init__), y cada uno traía su propio pool de conexiones:
# el pool que se calienta en un turno se tiraba junto con el cliente, así que
# no había nada que reusar y precalentar era imposible. Con uno solo, la
# conexión abierta en el arranque sirve para todos los turnos siguientes.
_client_default: OpenAI | None = None


def get_client(api_key: str | None = None) -> OpenAI:
    """
    Cliente del proveedor LLM activo. Sin argumento devuelve el compartido.

    Con `api_key` explícita construye uno aparte — no se cachea, porque la key
    es distinta de la del entorno.
    """
    if api_key:
        return OpenAI(api_key=api_key, base_url=LLM_BASE_URL)

    global _client_default
    if _client_default is None:
        _client_default = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    return _client_default


def warmup() -> None:
    """
    Abre la conexión con el proveedor antes de que el usuario escriba.

    La primera request de un proceso paga DNS + TCP + TLS: medido contra
    Gemini, entre 1 y 3.5 segundos que se le cargaban enteros al primer
    mensaje del usuario. Haciéndola en el arranque, ese costo se paga mientras
    la ventana todavía está cargando y el primer mensaje sale ~1s más rápido.

    Se usa models.list() a propósito: abre la misma conexión que después
    reusan los pedidos de chat, pero no consume la cuota de generación (el
    tier gratis de Gemini son 15 requests por minuto y hay que cuidarlos).

    Falla en silencio: sin red, sin key o con el endpoint caído, la app tiene
    que arrancar igual — esto es una optimización, no un requisito.
    """
    try:
        get_client().models.list()
        logger.info("Conexión con %s precalentada", _PROVIDER_LABEL)
    except Exception as e:
        logger.debug("Warmup de %s falló (no es grave): %s", _PROVIDER_LABEL, e)


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


def friendly_error(e) -> str:
    """Traduce la excepción a un mensaje que se le puede mostrar al usuario."""
    s = str(e).lower()
    if is_auth_error(e):
        var = _ENV_KEY.get(LLM_PROVIDER, "la API key")
        return (
            f"La API key de {_PROVIDER_LABEL} no es válida o falta. "
            f"Revisá {var} en el archivo .env de la raíz del proyecto "
            f"(hay una plantilla en .env.example) y reiniciá la app."
        )
    if "per day" in s or "tpd" in s or "perday" in s:
        m = re.search(r"try again in ([0-9hms.]+)", s)
        cuando = "en un rato"
        if m:
            mins = re.search(r"(\d+)m", m.group(1))
            cuando = f"en ~{mins.group(1)} min" if mins else f"en {m.group(1)}"
        return f"Llegamos al límite de tokens por ahora. Vuelve a intentar {cuando}."
    if is_rate_limit(e):
        return "Muchas consultas muy rápido. Espera unos segundos y vuelve a intentarlo."
    return "Hubo un problema al conectar. Intentá de nuevo."


# ── Streaming ────────────────────────────────────────────────────────────────

async def stream_deltas(client, messages, *, model=MAIN_MODEL, max_tokens=2048,
                        temperature=0.4):
    """Itera los fragmentos de texto de una respuesta en streaming."""
    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        stream=True,
        **_EXTRA_PARAMS,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


async def stream_with_retry(client, messages, *, model=MAIN_MODEL, max_tokens=2048,
                            temperature=0.4, on_retry=None):
    """
    Igual que stream_deltas pero reintenta ante rate limit, esperando el tiempo
    exacto que indica el proveedor en el error.

    Emite tuplas ("text", delta) o ("status", mensaje). Si tras MAX_ATTEMPTS
    sigue fallando, emite ("error", mensaje_amigable).
    """
    last_exc = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            async for delta in stream_deltas(client, messages, model=model,
                                             max_tokens=max_tokens,
                                             temperature=temperature):
                yield ("text", delta)
            return
        except Exception as e:
            last_exc = e
            if is_rate_limit(e) and attempt < MAX_ATTEMPTS - 1:
                wait = parse_retry_seconds(e)
                logger.warning("%s rate limit (intento %d/%d), esperando %.1fs",
                               _PROVIDER_LABEL, attempt + 1, MAX_ATTEMPTS, wait)
                msg = f"Límite de {_PROVIDER_LABEL}, reintentando en {wait:.0f}s..."
                if on_retry:
                    on_retry(wait)
                yield ("status", msg)
                await asyncio.sleep(wait)
            else:
                break

    if last_exc:
        logger.error("%s stream falló definitivamente: %s", _PROVIDER_LABEL, last_exc)
        yield ("error", friendly_error(last_exc))
