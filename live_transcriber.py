"""
Pipeline de transcripción en vivo para streams de YouTube.
Extrae audio en crudo con ffmpeg (streaming, sin archivos intermedios) y
transcribe con Groq Whisper en ventanas de 10s traslapadas 2s entre sí, para
no perder palabras que caigan justo en el corte entre chunks.
"""
import array
import asyncio
import difflib
import math
import os
import shutil
import signal
import struct
import threading
import time

# NOTE: Using YouTube streams via yt-dlp + ffmpeg may conflict with YouTube ToS
# in a public production context. Fine for personal/dev use.

SAMPLE_RATE      = 16000  # Hz — óptimo para Whisper
CHUNK_SECONDS    = 10     # cuánto avanza el "reloj" de la transcripción por chunk
OVERLAP_SECONDS  = 2      # cuánto se repite del final del chunk anterior
BYTES_PER_SEC    = SAMPLE_RATE * 2  # s16le mono = 2 bytes/muestra
WINDOW_BYTES     = (CHUNK_SECONDS + OVERLAP_SECONDS) * BYTES_PER_SEC
ADVANCE_BYTES    = CHUNK_SECONDS * BYTES_PER_SEC

# Red de seguridad: si el cliente se desconecta de un stream EN VIVO
# (infinito) y el servidor no llega a notarlo (limitación de detección de
# desconexión de uvicorn/ASGI en ciertos entornos — ver routers/live.py),
# ffmpeg quedaría corriendo para siempre. Este tope garantiza que, en el
# peor caso, la sesión se corta sola en vez de filtrar el proceso sin límite.
#
# OJO: esto NO debe funcionar como límite operativo — hay sesiones reales
# del Congreso de 7+ horas (ver /sesiones/videos), y la transcripción en
# vivo tiene que poder seguir un stream indefinidamente mientras el cliente
# siga conectado. Por eso el valor es deliberadamente muy alto: es solo un
# backstop para sesiones abandonadas/olvidadas, no un tiempo esperado de uso.
MAX_SESSION_SECONDS = 12 * 60 * 60  # 12 h — backstop, no límite operativo

# Whisper "alucina" (inventa texto — despedidas, subtítulos de relleno,
# frases repetidas) cuando le mandás una ventana en silencio o casi
# silencio: no fue entrenado para decir "no hay nada acá", así que rellena
# con lo más probable estadísticamente. Por eso medimos el volumen ANTES de
# llamar a la API y, si no hay suficiente señal, ni siquiera la llamamos.
SILENCE_RMS_THRESHOLD = 400  # escala int16 (0-32767); ajustable si hace falta


def get_stream_url(video_id: str) -> tuple:
    """
    Returns (stream_url, is_live).
    Uses yt-dlp to resolve the best audio HLS URL.

    is_live = está transmitiéndose AHORA MISMO. OJO: esto NO es lo mismo
    que "was_live" (fue una transmisión en vivo pero ya terminó) — un video
    was_live es un VOD normal que soporta seek con -ss como cualquier otro,
    así que no lo tratamos como en vivo acá (antes esto estaba mezclado y
    hacía que el seek a un minuto exacto se ignorara en cualquier video que
    alguna vez hubiera sido una transmisión en vivo).
    """
    import yt_dlp

    from scraper import _ydl_cookie_opts

    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio[protocol=m3u8_native]/bestaudio[protocol=m3u8]/bestaudio/best",
        **_ydl_cookie_opts(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    is_live = bool(info.get("is_live"))

    # Prefer the resolved URL from the selected format
    fmts = info.get("requested_formats") or [info]
    stream_url = fmts[0].get("url") or info.get("url", "")
    if not stream_url:
        raise ValueError("No se pudo resolver la URL del stream de YouTube.")
    return stream_url, is_live


# Prompt de estilo para Whisper: sin esto, el modelo tiende a "limpiar" el habla
# (omite muletillas y a veces palabras sueltas en tramos de voz baja).
TRANSCRIBE_PROMPT = (
    "Transcripción literal y textual en español de una sesión del Congreso del Perú. "
    "Incluye todas las muletillas y sonidos de habla como \"eh\", \"este\", \"o sea\", "
    "\"ajá\", \"mmm\", \"bueno\", sin resumir, sin limpiar ni omitir palabras, "
    "tal como se escuchan, incluso en voz baja o de fondo."
)


def _wav_header(pcm_len: int, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Header WAV mínimo (PCM s16le mono) para envolver audio crudo en memoria."""
    channels, bits = 1, 16
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    return (
        b"RIFF" + struct.pack("<I", 36 + pcm_len) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits)
        + b"data" + struct.pack("<I", pcm_len)
    )


def _transcribe_pcm(pcm: bytes, api_key: str) -> str:
    """Transcribe una ventana de audio PCM crudo con Groq Whisper."""
    if len(pcm) < 4096:  # ventana casi vacía (silencio total / arranque)
        return ""

    from groq import Groq

    # timeout/max_retries acotados: esta llamada corre en un hilo bloqueante
    # (run_in_executor) que asyncio NO puede interrumpir si el cliente se
    # desconecta — sin este límite, un rate-limit con reintentos puede
    # colgar la llamada decenas de segundos y ffmpeg sigue vivo mientras
    # tanto sin que ninguna cancelación del lado async pueda hacer nada.
    client = Groq(api_key=api_key, timeout=15, max_retries=1)
    result = client.audio.transcriptions.create(
        file=("chunk.wav", _wav_header(len(pcm)) + pcm),
        model="whisper-large-v3-turbo",
        language="es",
        response_format="text",
        prompt=TRANSCRIBE_PROMPT,
        temperature=0,
    )
    text = result if isinstance(result, str) else getattr(result, "text", "")
    return text.strip()


def _rms(pcm: bytes) -> int:
    """
    RMS de audio PCM s16le mono.

    Esto lo hacía audioop.rms(), pero audioop se eliminó de la stdlib en
    Python 3.13 (PEP 594) y sin esto el módulo ni siquiera importa — lo que
    tumbaba el servidor entero, no solo el vivo, porque routers/live.py lo
    importa al arranque. La versión en Python puro cuesta 28 ms por ventana
    de 12 s (medido en esta Mac): 0.24 % del tiempo real del chunk, así que
    no vale la pena arrastrar una dependencia binaria para esto.
    """
    muestras = array.array("h")
    muestras.frombytes(pcm[:len(pcm) // 2 * 2])  # descarta un byte suelto al final
    if not muestras:
        return 0
    return int(math.sqrt(sum(m * m for m in muestras) / len(muestras)))


def _es_error_de_key(exc) -> bool:
    """Key de Groq ausente, inválida o sin permisos — reintentar no sirve."""
    s = str(exc).lower()
    return ("invalid api key" in s or "invalid_api_key" in s
            or "error code: 401" in s or "error code: 403" in s
            or "unauthorized" in s)


def _is_silence(pcm: bytes) -> bool:
    """RMS del audio crudo (PCM s16le) — filtro previo a Whisper para no
    alucinar texto sobre tramos sin voz real."""
    if len(pcm) < 2:
        return True
    return _rms(pcm) < SILENCE_RMS_THRESHOLD


def ffmpeg_exe() -> str:
    """
    Ruta al binario de ffmpeg.

    Primero el del sistema (PATH). Si no está — el caso normal en una Mac sin
    Homebrew — usa el que trae el paquete `imageio-ffmpeg`, que se instala con
    un pip install y no necesita brew ni sudo. Si tampoco está, devuelve
    "ffmpeg" para que el error que vea el usuario sea el de siempre.
    """
    ruta = shutil.which("ffmpeg")
    if ruta:
        return ruta
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _merge_overlap(prev_text: str, new_text: str, max_words: int = 12) -> str:
    """
    Los chunks se solapan OVERLAP_SECONDS entre sí, así que el inicio de
    new_text suele repetir literalmente el final de prev_text. Recorta ese
    tramo repetido para no mostrarlo dos veces en la transcripción.
    """
    if not prev_text or not new_text:
        return new_text
    prev_words = prev_text.split()[-max_words:]
    new_words = new_text.split()
    sm = difflib.SequenceMatcher(None, prev_words, new_words[:max_words])
    match = sm.find_longest_match(0, len(prev_words), 0, min(max_words, len(new_words)))
    if match.size >= 2:  # al menos 2 palabras en común para confiar en el match
        return " ".join(new_words[match.b + match.size:])
    return new_text


async def stream_transcription(video_id: str, api_key: str, start_seconds: int = 0):
    """
    Async generator that yields dicts as each audio chunk is transcribed.
    start_seconds: segundo del video donde arrancar la captura — permite
    seguir al usuario si adelantó/atrasó un video ya grabado en vez de
    transcribir siempre desde 0:00. Se ignora para streams en vivo.
    Yields:
      {"status": "..."} — status updates
      {"timestamp": "mm:ss", "text": "...", "elapsed": int} — transcription lines
      {"error": "..."} — on failure
    """
    loop = asyncio.get_event_loop()

    # ── Step 1: resolve stream URL ──────────────────────────────
    yield {"status": "Resolviendo URL del stream..."}
    try:
        stream_url, is_live = await loop.run_in_executor(None, get_stream_url, video_id)
    except Exception as e:
        yield {"error": f"No se pudo obtener el stream: {e}"}
        return

    kind = "live" if is_live else "video"
    start_seconds = max(0, start_seconds) if not is_live else 0
    yield {"status": f"Stream resuelto ({kind}). Iniciando captura de audio..."}

    # ── Step 2: ffmpeg emite PCM crudo continuo por stdout ──────
    cmd = [ffmpeg_exe(), "-y"]
    if start_seconds:
        # -ss ANTES de -i: seek de entrada, salta directo al segmento HLS
        # correspondiente en vez de descargar y descartar todo lo anterior.
        cmd += ["-ss", str(start_seconds)]
    cmd += [
        "-i", stream_url,
        "-vn",                   # audio only
        "-ar", str(SAMPLE_RATE), # 16 kHz — Whisper optimal
        "-ac", "1",               # mono
        # Sube el volumen de forma adaptativa: refuerza los tramos bajos
        # (voces lejanas del micrófono, murmullos) sin saturar los picos.
        "-af", "dynaudnorm=f=150:g=15:m=25:p=0.95",
        "-f", "s16le",
        "pipe:1",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    yield {"status": "Capturando audio... (primera transcripción en ~10 s)"}

    # Leer y transcribir en el MISMO loop bloqueaba a ffmpeg: mientras
    # esperábamos a Whisper (1-2s+ por chunk), nadie drenaba proc.stdout, su
    # buffer de pipe (64KB en Linux) se llenaba, y para un stream en vivo
    # ffmpeg terminaba estancado esperando poder escribir — dejaba de avanzar
    # para siempre, sin morir ni dar error. Por eso una tarea aparte drena el
    # pipe todo el tiempo hacia una cola, desacoplada del ritmo de Whisper.
    audio_q: asyncio.Queue = asyncio.Queue(maxsize=200)  # ~ minutos de margen

    async def _drain_stdout():
        try:
            while True:
                data = await proc.stdout.read(65536)
                await audio_q.put(data or None)
                if not data:
                    return
        except asyncio.CancelledError:
            pass
        except Exception:
            await audio_q.put(None)

    reader_task = asyncio.ensure_future(_drain_stdout())

    # Red de seguridad independiente: si el cliente se desconecta de un
    # stream EN VIVO y nada del lado async llega a enterarse (visto en
    # pruebas: ni is_disconnected() ni una tarea de asyncio con su propio
    # timer bastan — algo en la cadena Starlette/anyio puede dejar hasta
    # tareas de asyncio "independientes" sin correr), esto necesita ser
    # un hilo de sistema operativo real, no una Task de asyncio: un
    # threading.Thread con time.sleep() no depende del event loop para
    # nada, no puede ser cancelado por ningún cancel scope, y es la única
    # garantía real de que ffmpeg no queda corriendo para siempre.
    def _watchdog_thread(pid: int):
        time.sleep(MAX_SESSION_SECONDS)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # ya había terminado solo

    watchdog = threading.Thread(target=_watchdog_thread, args=(proc.pid,), daemon=True)
    watchdog.start()

    buf = bytearray()
    chunk_index = 0
    prev_text = ""
    start_real = time.monotonic()

    try:
        while True:
            if time.monotonic() - start_real > MAX_SESSION_SECONDS:
                yield {"status": "Sesión cortada por límite de tiempo máximo."}
                break

            data = await audio_q.get()
            if not data:
                break  # ffmpeg cerró stdout: terminó o murió

            buf.extend(data)

            while len(buf) >= WINDOW_BYTES:
                window = bytes(buf[:WINDOW_BYTES])
                elapsed = start_seconds + chunk_index * CHUNK_SECONDS
                ts = f"{elapsed // 60:02d}:{elapsed % 60:02d}"

                if _is_silence(window):
                    # Sin señal real: NO llamamos a Whisper. Si le mandás
                    # silencio igual te devuelve texto (alucina despedidas,
                    # relleno, frases repetidas) — mejor no decir nada.
                    text = ""
                else:
                    try:
                        text = await loop.run_in_executor(None, _transcribe_pcm, window, api_key)
                    except Exception as exc:
                        # Una key inválida no se arregla sola: seguir capturando
                        # repite el mismo 401 cada 10 s y el usuario ve un
                        # volcado del SDK en vez de saber qué le falta.
                        if _es_error_de_key(exc):
                            yield {"error": (
                                "La GROQ_API_KEY no es válida o falta. La transcripción "
                                "en vivo usa Whisper de Groq siempre, aunque el chat esté "
                                "configurado con otro proveedor. Completá GROQ_API_KEY en "
                                "el archivo .env y reiniciá la app."
                            )}
                            break
                        yield {"error": f"Error al transcribir el tramo {ts}: {exc}"}
                        text = ""

                if text:
                    visible = _merge_overlap(prev_text, text)
                    prev_text = text
                    if visible.strip():
                        yield {"timestamp": ts, "text": visible, "elapsed": elapsed}

                # Avanza CHUNK_SECONDS y conserva OVERLAP_SECONDS como cola
                # para la próxima ventana.
                del buf[:ADVANCE_BYTES]
                chunk_index += 1

        # Salimos del while por EOF (sin cancelación) — chequea si fue un error real.
        await proc.wait()
        if proc.returncode not in (0, None):
            stderr_out = b""
            try:
                stderr_out = await asyncio.wait_for(proc.stderr.read(), timeout=2)
            except Exception:
                pass
            yield {"error": f"ffmpeg terminó con error (código {proc.returncode}). {stderr_out.decode(errors='replace')[:200]}"}

    except asyncio.CancelledError:
        pass
    finally:
        # El watchdog es un hilo daemon con time.sleep(): no hace falta (ni
        # se puede) cancelarlo — si ffmpeg ya está muerto para cuando
        # despierte, os.kill() sobre un pid inexistente simplemente no hace
        # nada (ProcessLookupError, ya capturado ahí mismo).
        if not reader_task.done():
            reader_task.cancel()
            try:
                await reader_task
            except BaseException:
                pass
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception:
                    pass
