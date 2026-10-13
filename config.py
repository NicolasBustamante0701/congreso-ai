"""
Configuración central: rutas, credenciales, modelos y carga de prompts.

Todo lo que antes vivía disperso en la cabecera de server.py se centraliza aquí
para que routers/ y services/ no dependan del módulo de arranque.
"""
import contextvars
import datetime
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger("congreso-ai")

# ── Logging ──────────────────────────────────────────────────────────────────
# Empaquetada, el stdout del server lo recibe Electron y no lo ve nadie: sin
# archivo, una caída no dejaba rastro. Cada línea lleva el id del request (lo
# fija services/observability.py) para poder seguir un pedido de punta a punta.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(request_id)s] %(name)s: %(message)s"


class _RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_var.get()
        return True


def _log_dir() -> Path:
    if os.getenv("DIANA_LOG_DIR"):
        return Path(os.environ["DIANA_LOG_DIR"])
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "Diana"
    return Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "diana"


LOG_FILE = _log_dir() / "server.log"


def _data_dir() -> Path:
    """
    Datos persistentes del usuario (transcripciones). Nunca dentro del bundle:
    Resources/ de un .app puede ser de solo lectura y se reemplaza al
    actualizar. Electron pasa su userData en DIANA_DATA_DIR.
    """
    if os.getenv("DIANA_DATA_DIR"):
        return Path(os.environ["DIANA_DATA_DIR"])
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Diana"
    return Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "diana"


DATA_DIR = _data_dir()
DB_FILE = DATA_DIR / "diana.db"


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(LOG_FORMAT)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(LOG_FILE, maxBytes=2_000_000,
                                            backupCount=5, encoding="utf-8"))
    except OSError as e:
        print(f"[diana] sin log a archivo ({LOG_FILE}): {e}", file=sys.stderr)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(_RequestIdFilter())
        root.addHandler(h)
    # El middleware ya registra cada request con su duración.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for ruidoso in ("httpx", "httpx2", "httpcore", "openai", "groq"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)

    def _hook(exc_type, exc, tb):
        logger.critical("Excepción no capturada", exc_info=(exc_type, exc, tb))

    def _thread_hook(args):
        logger.critical("Excepción no capturada en el hilo %s", args.thread.name if args.thread else "?",
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _hook
    threading.excepthook = _thread_hook


if not logging.getLogger().handlers:
    _setup_logging()

# ── Rutas ────────────────────────────────────────────────────────────────────
# Con PyInstaller los datos van a sys._MEIPASS; en dev, al directorio del repo.
FROZEN      = getattr(sys, "frozen", False)
BASE_DIR    = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parent
STATIC_DIR  = BASE_DIR / "static"
PROMPTS_DIR = BASE_DIR / "prompts"

# ── Credenciales ─────────────────────────────────────────────────────────────
# `load_dotenv()` a secas busca el .env caminando hacia arriba desde el cwd, y
# en el .app empaquetado el cwd lo fija Electron: si algún día cambia, las keys
# desaparecen sin ruido y la app abre muerta. Por eso la ruta es explícita.
#
# El .env es solo para desarrollo. El DMG sale SIN credenciales (build-mac.yml
# falla si encuentra un .env en dist/server): cada usuario pega su key en
# Ajustes y Electron la pasa por variable de entorno (main.js, aiEnv). Si el
# archivo no existe, load_dotenv no hace nada y el server arranca igual; el
# chat avisa que falta la key.
ENV_FILE = (Path(sys.executable).resolve().parent if FROZEN else BASE_DIR) / ".env"
load_dotenv(ENV_FILE)

# GROQ_API_KEY es independiente del selector de proveedor de abajo: la
# transcripción de audio en vivo (Whisper, en live_transcriber.py y
# scraper.py) SIEMPRE habla con Groq directo, sin importar qué proveedor de
# chat esté activo — Gemini/OpenAI no exponen ese mismo endpoint de Whisper.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# ── Proveedor de LLM para chat ────────────────────────────────────────────────
# El producto final habla con la API de OpenAI. Mientras tanto, para probar sin
# quemar cuota/plata, se puede apuntar a cualquier endpoint compatible con el
# formato OpenAI (Gemini y Groq lo son) con solo cambiar LLM_PROVIDER en .env —
# el resto del código (services/llm.py, orchestrator, etc.) no cambia, porque
# todos hablan a través del mismo cliente `openai.OpenAI(base_url=...)`.
#
# Para pasar a producción con OpenAI real: LLM_PROVIDER=openai + OPENAI_API_KEY
# en .env. No hace falta tocar nada más.
#
# El default es "gemini" porque es el proveedor que se usa de verdad. Antes era "groq": si por lo que sea el .env no
# carga, la app terminaba pidiéndole a Groq con la key vacía y el error que
# salía era "falta la GROQ_API_KEY" — un desvío hacia un proveedor que no es
# el que está configurado.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()

_PROVIDERS = {
    "groq": {
        "base_url":     "https://api.groq.com/openai/v1",
        "api_key":      GROQ_API_KEY,
        # Los llama-3.x se dieron de baja (404, verificado 26/09/2026).
        "router_model": "openai/gpt-oss-20b",
        "main_model":   "openai/gpt-oss-120b",
    },
    "gemini": {
        "base_url":     "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key":      os.getenv("GEMINI_API_KEY", ""),
        # "gemini-flash-latest" resuelve a un modelo distinto según el momento
        # (visto: cambió a "gemini-3.6-flash", con cuota gratis de solo 20
        # pedidos/día) — "-lite-latest" es más estable y con más cuota libre
        # en esta cuenta, verificado.
        "router_model": "gemini-flash-lite-latest",
        "main_model":   "gemini-flash-lite-latest",
        # Cuando el modelo principal responde 503 ("high demand") se prueba
        # con estos antes de cambiar de proveedor. El 13/10/2026 los dos
        # "-latest" daban 503 mientras este respondía (lento: 10-17 s al
        # primer token, por eso tiene su propio timeout en llm.stream).
        "modelos_respaldo": ["gemini-3-flash-preview"],
    },
    "cerebras": {
        "base_url":     "https://api.cerebras.ai/v1",
        "api_key":      os.getenv("CEREBRAS_API_KEY", ""),
        "router_model": "gpt-oss-120b",
        "main_model":   "gpt-oss-120b",
    },
    # Plan gratis "Experiment" de Mistral: sin tarjeta (solo SMS), ~1000
    # millones de tokens al mes y 128k de contexto. Es el respaldo que entra
    # cuando Gemini gratis está saturado: Groq gratis corta en 8000 tokens
    # por pedido y una consulta de 15 días de proyectos pide ~24 000.
    "mistral": {
        "base_url":     "https://api.mistral.ai/v1",
        "api_key":      os.getenv("MISTRAL_API_KEY", ""),
        # Verificado el 13/10/2026 con una key del plan gratis: small y medium
        # daban 429 permanente y large 403 ("not available in your
        # subscription tier"); estos tres responden y llaman herramientas.
        "router_model": "open-mistral-nemo",
        "main_model":   "ministral-14b-latest",
        "modelos_respaldo": ["open-mistral-nemo", "ministral-8b-latest"],
        # Modelos chicos: con 0.4 inventaban datos (un año para una ley, que
        # el Pleno "aprobó" algo que solo estaba agendado). Más bajo, se pegan
        # más al texto de las herramientas.
        "temperatura": 0.1,
    },
    "openai": {
        "base_url":     None,  # SDK usa el endpoint oficial por default
        "api_key":      os.getenv("OPENAI_API_KEY", ""),
        "router_model": os.getenv("OPENAI_ROUTER_MODEL", "gpt-4o-mini"),
        "main_model":   os.getenv("OPENAI_MAIN_MODEL", "gpt-4o"),
    },
}

if LLM_PROVIDER not in _PROVIDERS:
    logger.warning("LLM_PROVIDER=%r no existe, se usa gemini", LLM_PROVIDER)
    LLM_PROVIDER = "gemini"
PROVIDERS = _PROVIDERS

# Si el proveedor activo falla o no responde, se prueba con los demás que
# tengan key cargada, en este orden. Groq casi siempre está (su key es
# obligatoria para Whisper). LLM_FALLBACKS=none lo desactiva; una lista
# ("groq,cerebras") fija el orden.
_fallbacks_env = os.getenv("LLM_FALLBACKS", "").strip().lower()
if _fallbacks_env == "none":
    _orden = []
elif _fallbacks_env:
    _orden = [p.strip() for p in _fallbacks_env.split(",")]
else:
    # Mistral antes que Groq: acepta las consultas grandes que Groq gratis
    # rechaza con 413.
    _orden = ["mistral", "groq", "gemini", "cerebras", "openai"]
LLM_FALLBACKS = [p for p in _orden
                 if p in _PROVIDERS and p != LLM_PROVIDER and _PROVIDERS[p]["api_key"]]

# connect: sin red o DNS caído falla rápido. read: máximo silencio entre dos
# fragmentos del stream antes de dar al proveedor por colgado (el default del
# SDK era 600 s × 3 intentos).
#
# Con un respaldo disponible se espera menos: medido el 26/09/2026, Gemini
# (tier gratis) tardó entre 0.5 y 28 s en dar el primer token para la MISMA
# pregunta. Pasado FIRST_TOKEN_TIMEOUT (stream) o ROUTER_TIMEOUT (router, sin
# streaming) se pasa al siguiente proveedor. El último de la cadena espera
# hasta LLM_READ_TIMEOUT.
LLM_CONNECT_TIMEOUT = float(os.getenv("LLM_CONNECT_TIMEOUT", 10))
LLM_READ_TIMEOUT    = float(os.getenv("LLM_READ_TIMEOUT", 60))
FIRST_TOKEN_TIMEOUT = float(os.getenv("FIRST_TOKEN_TIMEOUT", 15))
ROUTER_TIMEOUT      = float(os.getenv("ROUTER_TIMEOUT", 15))
WHISPER_TIMEOUT     = float(os.getenv("WHISPER_TIMEOUT", 120))

if LLM_PROVIDER not in _PROVIDERS:
    # Un typo en el .env tumbaba el import entero (KeyError) y la app no
    # abría. Mejor arrancar con el default y dejarlo en el log.
    logging.getLogger("congreso-ai").warning(
        "LLM_PROVIDER '%s' no existe; uso gemini", LLM_PROVIDER)
    LLM_PROVIDER = "gemini"
_provider_cfg = _PROVIDERS[LLM_PROVIDER]
LLM_BASE_URL  = _provider_cfg["base_url"]
LLM_API_KEY   = _provider_cfg["api_key"]
ROUTER_MODEL  = _provider_cfg["router_model"]
MAIN_MODEL    = _provider_cfg["main_model"]

# ── Servidor ─────────────────────────────────────────────────────────────────
PORT         = int(os.getenv("PORT", 8732))
ALLOWED_ORIGIN = f"http://localhost:{PORT}"


# Perú no tiene horario de verano: UTC-5 fijo es exacto y no depende de que
# el sistema (o el bundle de PyInstaller) traiga la base tzdata.
LIMA_TZ = datetime.timezone(datetime.timedelta(hours=-5), "America/Lima")


def ahora_lima() -> "datetime.datetime":
    """
    Fecha y hora actual en Lima, sin tzinfo (naive).

    Naive a propósito: SPLEY y los portales devuelven fechas sin huso, y
    compararlas contra la hora local de la Mac (o contra UTC, como hacía
    fetch_proyectos con utcnow) corría la ventana de días hasta 5 horas: de
    noche en Lima, "hoy" en UTC ya era mañana.
    """
    return datetime.datetime.now(LIMA_TZ).replace(tzinfo=None)


def load_prompt(name: str) -> str:
    """Carga un prompt desde prompts/<name>.md."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def static_file(name: str) -> str:
    """Lee un archivo de static/ como texto (usa BASE_DIR, no el cwd)."""
    return (STATIC_DIR / name).read_text(encoding="utf-8")
