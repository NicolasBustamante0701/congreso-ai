"""
OCR de páginas escaneadas.

Muchos proyectos de ley llegan a SPLEY como PDF escaneado: PyMuPDF no saca
texto y Diana respondía "no se puede leer la fórmula legal" (pasó con el
14864/2025-CR, 7 páginas de imagen).

Motor principal: el OCR de macOS (framework Vision, vía PyObjC). Es local,
sin cuota ni red, lee español y tarda 1.5-7 s por página (medido en esta
Mac Intel con macOS 12). Respaldo: Gemini con visión, por el mismo endpoint
compatible con OpenAI del chat — el 13/10/2026 el tier gratis daba 503 y 429
en la mitad de los pedidos, por eso no es el principal.

Lo transcrito puede tener errores en algún número o nombre (sellos, firmas
manuscritas). Quien lo use tiene que decirlo (ver fetch_formula_legal).
"""
import asyncio
import base64
import hashlib
import json
import threading

from config import DATA_DIR, PROVIDERS, logger
from services import llm

# Tope de páginas por documento: una fórmula legal está en las primeras; la
# exposición de motivos de un proyecto largo puede pasar de 50 páginas.
MAX_PAGINAS = 12
PAGINAS_POR_PEDIDO = 2
ZOOM = 2.0
# Gemini gratis falla seguido con 503: una segunda vuelta para las páginas
# que no salieron, tras una pausa corta.
PAUSA_REINTENTO = 4.0
# Lo ya transcrito se guarda: un PDF oficial no cambia y cada OCR tarda y
# gasta cuota.
CACHE_DIR = DATA_DIR / "ocr"

PROMPT = (
    "Transcribe literalmente el texto de estas páginas escaneadas de un proyecto "
    "de ley del Congreso del Perú. Respeta artículos, numerales y saltos de "
    "párrafo. No resumas, no corrijas y no agregues nada. Si una palabra no se "
    "lee, escribe [ilegible]. Empieza cada página con su marca exacta, por "
    "ejemplo «[Página 3]»."
)


def _vision_disponible() -> bool:
    try:
        import Quartz  # noqa: F401
        import Vision  # noqa: F401
        return True
    except ImportError:
        return False


def disponible() -> bool:
    return _vision_disponible() or bool(PROVIDERS.get("gemini", {}).get("api_key"))


# PyObjC carga los símbolos del framework de forma perezosa y no es seguro
# hacerlo desde dos hilos a la vez: con lotes en paralelo fallaba con
# AttributeError: 'CGImageSourceCreateWithData'. Vision ya usa todos los
# núcleos por dentro, así que en serie no se pierde velocidad.
_vision_lock = threading.Lock()


def _ocr_vision(png_b64: str) -> str:
    """Texto de una imagen con el OCR de macOS, línea por línea."""
    with _vision_lock:
        return _ocr_vision_sin_lock(png_b64)


def _ocr_vision_sin_lock(png_b64: str) -> str:
    import Quartz
    import Vision
    from Foundation import NSData

    png = base64.b64decode(png_b64)
    fuente = Quartz.CGImageSourceCreateWithData(NSData.dataWithBytes_length_(png, len(png)), None)
    imagen = Quartz.CGImageSourceCreateImageAtIndex(fuente, 0, None)
    pedido = Vision.VNRecognizeTextRequest.alloc().init()
    pedido.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    pedido.setRecognitionLanguages_(["es-ES"])
    pedido.setUsesLanguageCorrection_(True)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(imagen, None)
    ok, error = handler.performRequests_error_([pedido], None)
    if not ok:
        raise RuntimeError(f"Vision: {error}")
    return "\n".join(o.topCandidates_(1)[0].string() for o in (pedido.results() or []))


async def _transcribir_vision(imagenes: list[tuple[int, str]]) -> str:
    partes = []
    for n, b64 in imagenes:
        texto = await asyncio.to_thread(_ocr_vision, b64)
        if texto.strip():
            partes.append(f"[Página {n}]\n{texto}")
    if not partes:
        raise RuntimeError("Vision no encontró texto")
    return "\n\n".join(partes)


def _renderizar(data: bytes, paginas: list[int]) -> list[tuple[int, str]]:
    """(número de página, PNG en base64) de cada página pedida (1 = primera)."""
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    out = []
    for n in paginas:
        if 1 <= n <= len(doc):
            pix = doc[n - 1].get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM), alpha=False)
            out.append((n, base64.b64encode(pix.tobytes("png")).decode()))
    return out


async def _transcribir(imagenes: list[tuple[int, str]]) -> str:
    if _vision_disponible():
        try:
            return await _transcribir_vision(imagenes)
        except Exception as e:
            logger.warning("OCR con Vision falló, pruebo con Gemini: %s", e)
    if not PROVIDERS.get("gemini", {}).get("api_key"):
        raise RuntimeError("OCR: no hay Vision ni Gemini")
    contenido = [{"type": "text", "text": PROMPT + " Páginas: "
                  + ", ".join(str(n) for n, _ in imagenes) + "."}]
    for _, b64 in imagenes:
        contenido.append({"type": "image_url",
                          "image_url": {"url": f"data:image/png;base64,{b64}"}})
    cfg = PROVIDERS["gemini"]
    modelos = [cfg["main_model"]] + list(cfg.get("modelos_respaldo", []))
    ultimo_error = None
    for model in modelos:
        try:
            resp = await llm.get_client("gemini").chat.completions.create(
                model=model, messages=[{"role": "user", "content": contenido}],
                max_tokens=8000, temperature=0, timeout=60,
                **llm._extra_params("gemini"),
            )
            texto = (resp.choices[0].message.content or "").strip()
            if texto:
                return texto
        except Exception as e:
            ultimo_error = e
            logger.warning("OCR con %s falló: %s", model, str(e)[:200])
    raise RuntimeError(f"OCR sin respuesta: {ultimo_error}")


def _cache_path(data: bytes):
    return CACHE_DIR / f"{hashlib.sha256(data).hexdigest()[:32]}.json"


def _leer_cache(data: bytes) -> dict[int, str]:
    try:
        return {int(k): v for k, v in json.loads(_cache_path(data).read_text()).items()}
    except (OSError, ValueError):
        return {}


def _guardar_cache(data: bytes, paginas: dict[int, str]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(data).write_text(json.dumps(paginas, ensure_ascii=False))
    except OSError as e:
        logger.warning("OCR: no se pudo guardar la caché: %s", e)


async def _vuelta(imagenes: list[tuple[int, str]]) -> dict[int, str]:
    """Una pasada en lotes paralelos; devuelve {lote_inicial: texto} de los que salieron."""
    lotes = [imagenes[i:i + PAGINAS_POR_PEDIDO]
             for i in range(0, len(imagenes), PAGINAS_POR_PEDIDO)]
    resultados = await asyncio.gather(*[_transcribir(lote) for lote in lotes],
                                      return_exceptions=True)
    return {lote[0][0]: r for lote, r in zip(lotes, resultados)
            if not isinstance(r, Exception)}, lotes


async def transcribir_paginas(data: bytes, paginas: list[int]) -> tuple[str, list[int]]:
    """
    Texto de las páginas pedidas (hasta MAX_PAGINAS) y las que quedaron sin leer.
    Nunca lanza: si Gemini no responde, devuelve lo que haya.
    """
    pedidas = paginas[:MAX_PAGINAS]
    hechas = {n: t for n, t in _leer_cache(data).items() if n in pedidas}
    faltan = [n for n in pedidas if n not in hechas]
    if faltan:
        imagenes = dict(await asyncio.to_thread(_renderizar, data, faltan))
        for vuelta in range(2):
            pendientes = [(n, imagenes[n]) for n in faltan if n in imagenes and n not in hechas]
            if not pendientes:
                break
            if vuelta:
                await asyncio.sleep(PAUSA_REINTENTO)
            ok, lotes = await _vuelta(pendientes)
            # El texto de un lote se guarda bajo su primera página; las demás
            # del lote quedan marcadas como incluidas ahí.
            for lote in lotes:
                if lote[0][0] in ok:
                    hechas[lote[0][0]] = ok[lote[0][0]]
                    for n, _ in lote[1:]:
                        hechas[n] = ""
        if hechas:
            _guardar_cache(data, {**_leer_cache(data), **hechas})
    texto = "\n\n".join(hechas[n] for n in sorted(hechas) if hechas[n])
    sin_leer = sorted([n for n in pedidas if n not in hechas]
                      + [n for n in paginas if n not in pedidas])
    return texto, sin_leer
