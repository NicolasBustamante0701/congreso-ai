"""
"Mirar" el video: capturas cada 30 s y lectura de la placa de quien habla.

Las transmisiones del Congreso (Senado y Diputados) no ponen el nombre de
quien habla en el cartel de abajo: el cartel dice solo la comisión. Lo que sí
sale es la PLACA del escritorio cuando la cámara enfoca a quien tiene la
palabra ("CÉSAR ALFONSO LUNA VICTORIA / Superintendente Nacional de Aduanas…",
visto el 26/10/2026 en la Comisión de Economía del Senado).

Cómo:
  1. yt-dlp da la lista de segmentos de video (~5 s cada uno, DASH; el mismo
     formato que ya baja la transcripción para el audio).
  2. Cada `CADA_S` segundos se baja UN segmento y ffmpeg saca un cuadro,
     recortando el cartel fijo de abajo (que tapa placas y confunde al OCR).
  3. OCR de macOS (Vision, local y gratis) con la posición de cada texto.
     La placa es una línea en MAYÚSCULAS de 2-6 palabras; de varias, gana la
     de letra más grande y más al centro (la de quien está en primer plano).
     La línea de abajo en minúsculas, si hay, es el cargo.

Esto NO reconoce caras: solo lee texto impreso en pantalla.

Las lecturas van al índice de oradores (services/oradores) como una pista
más, junto con el texto: la cámara a veces muestra a quien escucha.
Solo funciona con videos grabados (las transmisiones en vivo, al terminar,
pasan a ser videos grabados) y en macOS.
"""
import base64
import concurrent.futures as cf
import difflib
import re
import subprocess
import time
import unicodedata

from config import logger

CADA_S = 30
# Parte de arriba del cuadro que se lee: el cartel de la transmisión ocupa
# desde ~el 78 % hacia abajo.
ALTO_UTIL = 0.78
_CACHE_FORMATO: dict[str, tuple[float, dict]] = {}
_TTL_FORMATO = 60 * 60   # las URLs de YouTube vencen a las ~6 h

# Textos fijos de la transmisión y del mobiliario que no son placas.
_FIJOS = re.compile(r"CONGRESO|TV DIGITAL|EN VIVO|\bVIVO\b|SENADO|C[AÁ]MARA|DIPUTADOS|"
                    r"REP[UÚ]BLICA|SAN MATEO|SAN LUIS|CIELO|MANANTIAL|COMISI[OÓ]N|SALA ",
                    re.IGNORECASE)


def disponible() -> bool:
    from services import ocr
    return ocr._vision_disponible()


# ── Elegir la placa entre lo que leyó el OCR ─────────────────────────────────

def _es_nombre(texto: str) -> bool:
    t = texto.strip()
    if not 8 <= len(t) <= 48 or _FIJOS.search(t):
        return False
    letras = [c for c in t if c.isalpha()]
    if len(letras) < 7 or sum(c.isupper() for c in letras) / len(letras) < 0.9:
        return False
    if re.search(r"\d", t):
        return False
    palabras = [p for p in re.split(r"[\s.]+", t) if p]
    # Al menos dos palabras "de nombre" (4+ letras): "DE MANANT" (la etiqueta
    # de una botella de agua) no pasa; "MARÍA DE LA CRUZ" sí.
    return (2 <= len(palabras) <= 6 and all(len(p) >= 2 for p in palabras)
            and sum(len(p) >= 4 for p in palabras) >= 2)


def _es_cargo(texto: str) -> bool:
    t = texto.strip()
    letras = [c for c in t if c.isalpha()]
    return (len(letras) >= 6 and not _FIJOS.search(t)
            and sum(c.islower() for c in letras) / len(letras) > 0.4)


def elegir_placa(observaciones: list[dict]) -> dict | None:
    """
    observaciones: [{"texto", "conf", "x", "y", "w", "h"}] en coordenadas
    normalizadas de Vision (origen abajo a la izquierda).
    Devuelve {"nombre", "cargo"} o None.
    """
    nombres = [o for o in observaciones if o["conf"] >= 0.5 and _es_nombre(o["texto"])]
    if not nombres:
        return None

    def peso(o):   # letra grande y cerca del centro horizontal
        centro = o["x"] + o["w"] / 2
        return o["h"] * (1.2 - abs(centro - 0.5))
    placa = max(nombres, key=peso)
    cargo = ""
    debajo = [o for o in observaciones
              if o is not placa and _es_cargo(o["texto"])
              and o["y"] < placa["y"] and placa["y"] - o["y"] < placa["h"] * 3
              and o["x"] < placa["x"] + placa["w"] and o["x"] + o["w"] > placa["x"]]
    if debajo:
        cargo = " ".join(o["texto"].strip() for o in sorted(debajo, key=lambda o: -o["y"])[:2])
    return {"nombre": " ".join(placa["texto"].split()), "cargo": cargo[:120]}


def _ocr_con_cajas(png: bytes) -> list[dict]:
    from services import ocr
    with ocr._vision_lock:
        import Quartz
        import Vision
        from Foundation import NSData
        fuente = Quartz.CGImageSourceCreateWithData(NSData.dataWithBytes_length_(png, len(png)), None)
        imagen = Quartz.CGImageSourceCreateImageAtIndex(fuente, 0, None)
        pedido = Vision.VNRecognizeTextRequest.alloc().init()
        pedido.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        pedido.setRecognitionLanguages_(["es-ES"])
        pedido.setUsesLanguageCorrection_(False)   # son nombres propios
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(imagen, None)
        ok, error = handler.performRequests_error_([pedido], None)
        if not ok:
            raise RuntimeError(f"Vision: {error}")
        salida = []
        for o in pedido.results() or []:
            c = o.topCandidates_(1)[0]
            caja = o.boundingBox()
            salida.append({"texto": c.string(), "conf": float(c.confidence()),
                           "x": caja.origin.x, "y": caja.origin.y,
                           "w": caja.size.width, "h": caja.size.height})
        return salida


# ── Video ────────────────────────────────────────────────────────────────────

def _formato_video(video_id: str) -> dict | None:
    """Segmentos de video de 480/720p. None si es una transmisión en curso o no hay."""
    hit = _CACHE_FORMATO.get(video_id)
    if hit and time.time() - hit[0] < _TTL_FORMATO:
        return hit[1]
    import yt_dlp

    from scraper import _ydl_cookie_opts
    opts = {"quiet": True, "no_warnings": True,
            "format": "136/135/bestvideo[height<=720][vcodec^=avc1]", **_ydl_cookie_opts()}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    if info.get("is_live"):
        return None
    f = (info.get("requested_formats") or [info])[0]
    frs = f.get("fragments") or []
    if f.get("protocol") != "http_dash_segments" or not frs:
        return None
    dur = f.get("target_duration") or frs[0].get("duration") or 5
    formato = {"urls": [fr["url"] for fr in frs if fr.get("url")], "seg": float(dur),
               "headers": f.get("http_headers") or info.get("http_headers") or {}}
    _CACHE_FORMATO[video_id] = (time.time(), formato)
    return formato


def _cuadro(formato: dict, segundo: int) -> bytes:
    import httpx

    from live_transcriber import ffmpeg_exe
    i = int(segundo / formato["seg"])
    if i >= len(formato["urls"]):
        return b""
    with httpx.Client(headers=formato["headers"], timeout=30, follow_redirects=True) as c:
        r = c.get(formato["urls"][i])
        r.raise_for_status()
    p = subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-i", "pipe:0", "-frames:v", "1",
                        "-vf", f"crop=iw:ih*{ALTO_UTIL}:0:0", "-f", "image2", "-c:v", "png", "pipe:1"],
                       input=r.content, capture_output=True, timeout=60)
    return p.stdout


def capturar(video_id: str, desde_s: int, hasta_s: int) -> list[dict]:
    """
    Placas leídas entre desde_s y hasta_s, una captura cada CADA_S segundos.
    [{"start_s", "nombre", "cargo"}], sin repetir la misma placa seguida.
    Bloqueante (red + ffmpeg + OCR): llamarlo con asyncio.to_thread.
    """
    if not disponible():
        return []
    formato = _formato_video(video_id)
    if not formato:
        return []
    segundos = list(range(int(desde_s) - int(desde_s) % CADA_S + CADA_S // 2, int(hasta_s), CADA_S))

    def _uno(s):
        try:
            return s, _cuadro(formato, s)
        except Exception as e:
            logger.info("placas %s %ss: %s", video_id, s, e)
            return s, b""
    with cf.ThreadPoolExecutor(6) as ex:
        cuadros = list(ex.map(_uno, segundos))

    lecturas, anterior = [], None
    for s, png in cuadros:
        if not png:
            continue
        try:
            placa = elegir_placa(_ocr_con_cajas(png))
        except Exception as e:
            logger.info("placas OCR %s %ss: %s", video_id, s, e)
            continue
        if not placa:
            anterior = None
            continue
        clave = _clave(placa["nombre"])
        # La misma placa leída con una letra distinta ("VICTORA"), o vista de
        # nuevo tras un plano general de pocos minutos, no es un orador nuevo.
        ultima = lecturas[-1] if lecturas else None
        igual = (ultima is not None and s - ultima["visto_s"] <= 180
                 and difflib.SequenceMatcher(None, clave, _clave(ultima["nombre"])).ratio() >= 0.85)
        if not igual:
            lecturas.append({"start_s": s, "visto_s": s, **placa})
        else:
            ultima["visto_s"] = s
            if len(placa["cargo"]) > len(ultima["cargo"]):
                ultima["cargo"] = placa["cargo"]
            if len(placa["nombre"]) > len(ultima["nombre"]):
                ultima["nombre"] = placa["nombre"]
        anterior = clave
    return [{k: v for k, v in x.items() if k != "visto_s"} for x in lecturas]


def _clave(nombre: str) -> str:
    t = unicodedata.normalize("NFKD", nombre.lower())
    return "".join(c for c in t if c.isalpha())
