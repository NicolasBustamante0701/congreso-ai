"""
Extracción de texto y miniaturas de PDFs.

PyMuPDF (fitz) se importa dentro de cada función: es pesado y solo hace falta
cuando el usuario abre o sube un documento. Las funciones de fitz son
síncronas y con un expediente grande tardan segundos: quien las llama desde
un endpoint async las corre con asyncio.to_thread para no frenar el server.
"""
import httpx

from config import logger

MAX_TEXT_CHARS = 40000
MAX_PDF_BYTES = 50 * 1024 * 1024


class UrlNoPermitida(ValueError):
    """La URL no es de congreso.gob.pe."""


async def download(url: str, timeout: int = 30) -> httpx.Response:
    """
    Baja un PDF, solo de dominios del Congreso.

    Antes usaba un httpx.AsyncClient pelado: /pdf-thumbnail y /load-pdf-url
    descargaban cualquier URL que llegara (incluida la red local). Ahora va
    por el cliente del scraper, cuyo hook corta cualquier request — también
    una redirección — que salga de congreso.gob.pe.
    """
    import scraper

    if not scraper.url_permitida(url):
        raise UrlNoPermitida("Solo se pueden abrir PDFs de congreso.gob.pe")
    async with scraper._client() as c:
        c.timeout = httpx.Timeout(timeout)
        return await c.get(url)


def extract_text(data: bytes) -> tuple[str, int]:
    """
    Devuelve (texto, número de páginas), recortado a MAX_TEXT_CHARS.

    Cada página va precedida de «[Página N]» (N = página física del PDF, no
    la numeración impresa) para que las respuestas puedan citar de dónde sale
    cada dato. Las páginas sin texto extraíble (escaneadas) se listan al final
    en vez de pasar en silencio.
    """
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    pages = len(doc)
    partes, vacias = [], []
    for i, page in enumerate(doc, start=1):
        t = page.get_text().strip()
        if t:
            partes.append(f"[Página {i}]\n{t}")
        else:
            vacias.append(i)
    text = "\n\n".join(partes)
    if len(text) > MAX_TEXT_CHARS:
        corte = text[:MAX_TEXT_CHARS]
        ultima = corte.rfind("[Página ")
        hasta = corte[ultima:].split("]")[0].replace("[Página ", "") if ultima >= 0 else "?"
        text = corte + (f"\n\n[Texto recortado: se leyó hasta la página {hasta} "
                        f"de {pages}. Lo que sigue no está en este texto.]")
    if vacias:
        lista = ", ".join(map(str, vacias[:30])) + ("…" if len(vacias) > 30 else "")
        text += (f"\n\n[Páginas sin texto extraíble (probablemente escaneadas): "
                 f"{lista}. Su contenido no está disponible.]")
    return text, pages


def render_thumbnail(data: bytes, zoom: float = 1.8) -> bytes:
    """PNG de la primera página."""
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png")


def looks_like_pdf(resp: httpx.Response) -> bool:
    """Heurística: algunos servidores del Congreso no mandan el content-type."""
    if resp.status_code != 200:
        return False
    ct = resp.headers.get("content-type", "application/pdf").lower()
    return "html" not in ct


def safe_extract_text(data: bytes) -> str:
    """extract_text tolerante a fallos — devuelve '' si el PDF no se puede leer."""
    try:
        text, _ = extract_text(data)
        return text
    except Exception as e:
        logger.debug("extract_text falló: %s", e)
        return ""
