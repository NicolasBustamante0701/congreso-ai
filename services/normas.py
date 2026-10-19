"""
Constitución y Reglamentos oficiales, por artículo, para responder preguntas
de procedimiento ("¿qué pasa si censuran a un ministro?", "¿cuántas firmas
pide una interpelación?") con el texto de la norma y no con la memoria del
modelo.

Visto el 18/10/2026: con el modelo de respaldo, Diana inventaba "renunciar en
48 horas (art. 120)", "lo inhabilita 5 años", "el Senado ratifica la
censura". El conocimiento de los modelos es anterior al Congreso bicameral de
2026, que cambió la Constitución y los Reglamentos.

Fuentes (verificadas el 18/10/2026 en congreso.gob.pe):
  - Constitución, texto de diciembre de 2024: ya trae el régimen bicameral
    (Art. 132: la censura la hace la Cámara de Diputados). La "Constitución
    web 2026" publicada no tiene texto extraíble, por eso no se usa.
  - Reglamentos de la Cámara de Diputados, del Senado y del Congreso, de
    julio de 2026. El "Reglamento del Congreso" de setiembre de 2025 que
    sigue publicado es el unicameral: no se usa.

Los PDF se descargan una vez y se guardan, ya separados por artículo, en
DATA_DIR/normas. Se vuelven a bajar cada 30 días por si los actualizan.
"""
import asyncio
import json
import math
import re
import time
from collections import Counter

from config import DATA_DIR, logger

NORMAS = [
    {"clave": "constitucion", "nombre": "Constitución Política del Perú",
     "url": "https://www3.congreso.gob.pe/Docs/files/constitucion/constitucion-12-2024.pdf"},
    {"clave": "diputados", "nombre": "Reglamento de la Cámara de Diputados",
     "url": "https://www.congreso.gob.pe/wp-content/uploads/2026/07/Reglamento-de-la-Camara-de-Diputados.pdf"},
    {"clave": "senado", "nombre": "Reglamento del Senado",
     "url": "https://www.congreso.gob.pe/wp-content/uploads/2026/07/Reglamento-del-Senado-de-la-Republica.pdf"},
    {"clave": "congreso", "nombre": "Reglamento del Congreso de la República",
     "url": "https://www.congreso.gob.pe/wp-content/uploads/2026/07/Reglamento-del-Congreso-de-la-Republica-1.pdf"},
]
DIR = DATA_DIR / "normas"
REFRESCAR_CADA_S = 30 * 24 * 3600
MAX_ARTICULOS = 6
MAX_CHARS_ARTICULO = 1800

# Encabezado de artículo al comienzo de un renglón ("Artículo 132.", "Artículo
# 64-A.", "Artículo 5°"). Las menciones dentro del texto ("según el artículo
# 132") no empiezan renglón. Puede ir entre comillas: así viene el texto
# nuevo de un artículo modificado ("“Artículo 132. La Cámara de Diputados…").
_ART_RE = re.compile(r"(?m)^[ \t“\"«]*Art[íi]culo\s+(\d{1,3})(-?[A-Z])?\s*[.°º:–-]")
# Nota de modificación de la Constitución compilada: "(*) Párrafos 1 y 2
# modificados por … cuyos textos son los siguientes:".
_NOTA_RE = re.compile(r"(?m)^\s*\(\*\)")
_VACIAS = set("""de la el los las del al que en y o a un una por para con se su sus es son lo
como más mas sin sobre entre este esta estos estas ese esa cual cuales cuando donde quien
qué que cómo como cuál cuáles cuándo dónde quién pasa hace hacen puede pueden debe deben
si no le les ha han ser está están tiene tienen hay explica explicame dime dame
""".split())

_cache: dict | None = None
_bajando: asyncio.Task | None = None


def _sin_acentos(t: str) -> str:
    return t.translate(str.maketrans("áéíóúüÁÉÍÓÚÜñÑ", "aeiouuAEIOUUnN"))


def _raices(texto: str) -> list[str]:
    """Palabras normalizadas y recortadas a 6 letras: "interpelación" e
    "interpelar" caen en la misma raíz ("interp"), "censura" y "censurar"
    en "censur"."""
    palabras = re.findall(r"[a-z0-9]+", _sin_acentos(texto.lower()))
    return [p[:6] for p in palabras if len(p) >= 3 and p not in _VACIAS]


# Algunos títulos de los Reglamentos de 2026 usan una fuente sin tabla de
# caracteres: el texto sale corrido 29 posiciones ("Artículo 43. 'H¾QLFLyQ"
# es "Artículo 43. Definición") y sin decodificar esos artículos no se
# encontraban. Las tildes y la ligadura "fi" tienen glifos propios.
_CORRIDOS = {"¾": "fi", "¿": "fi", "y": "ó", "~": "ú", "t": "í", "i": "á", "p": "é",
             "x": "ñ", "²": "“", "³": "”"}
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _decodificar_palabra(p: str) -> str:
    salida = []
    for c in p:
        if c in _CORRIDOS:
            salida.append(_CORRIDOS[c])
        elif ord(c) < 98:
            salida.append(chr(ord(c) + 29))
        else:
            salida.append(c)
    return "".join(salida)


def _decodificar(texto: str) -> str:
    """Decodifica, palabra por palabra, los renglones con texto corrido."""
    if "\x03" not in texto:
        return texto
    lineas = []
    for linea in texto.split("\n"):
        if "\x03" not in linea:
            lineas.append(linea)
            continue
        prefijo = re.match(r"\s*Art[íi]culo\s+\d+[A-Z]?", linea)
        cabeza = prefijo.group(0) if prefijo else ""
        partes = re.split(r"([ \t]+)", linea[len(cabeza):])
        for i, p in enumerate(partes):
            if not p or p.isspace():
                continue
            minusculas = sum(c.islower() and c.isascii() for c in p)
            corrida = bool(_CONTROL_RE.search(p)) or minusculas / len(p) < 0.3
            if corrida:
                partes[i] = " ".join(_decodificar_palabra(t) for t in re.split(r"[\x03]", p))
        lineas.append(cabeza + "".join(partes))
    return re.sub(r" {2,}", " ", "\n".join(lineas))


def _sin_encabezados(paginas: list[str]) -> list[str]:
    """Quita los renglones que se repiten en muchas páginas (encabezados y
    pies: "CONSTITUCIÓN POLÍTICA DEL PERÚ", números de página)."""
    cuenta = Counter()
    for p in paginas:
        cuenta.update({l.strip() for l in p.splitlines() if l.strip()})
    umbral = max(3, len(paginas) // 5)
    repetidos = {l for l, n in cuenta.items() if n >= umbral}

    def limpia(p):
        return "\n".join(l for l in p.splitlines()
                         if l.strip() and l.strip() not in repetidos and not re.fullmatch(r"\d{1,3}", l.strip()))
    return [limpia(p) for p in paginas]


def separar_articulos(paginas: list[str]) -> list[dict]:
    """
    [{"numero": "132", "pagina": 105, "texto": "..."}].

    Un encabezado con número mayor que el anterior abre un artículo nuevo. Si
    el número se repite, es el texto nuevo de un artículo modificado (la
    Constitución compilada trae el texto original, una nota "(*) Párrafos 1 y
    2 modificados por la Ley 31988…" y el texto nuevo entre comillas): se
    descarta lo reemplazado y queda la nota, el texto nuevo y los párrafos
    que siguen vigentes. Sin esto, el Art. 132 decía "El Congreso hace
    efectiva…" (unicameral) en vez de "La Cámara de Diputados…".
    """
    limpias = _sin_encabezados([_decodificar(p) for p in paginas])
    texto, inicios = "", []
    for p in limpias:
        inicios.append(len(texto))
        texto += p + "\n"

    def pagina(pos):
        n = 1
        for i, ini in enumerate(inicios, start=1):
            if ini <= pos:
                n = i
        return n

    articulos: list[dict] = []
    ultimo = 0
    for m in _ART_RE.finditer(texto):
        n = int(m.group(1))
        if n > ultimo:
            articulos.append({"numero": f"{n}{m.group(2) or ''}".replace("-", ""),
                              "pagina": pagina(m.start()), "inicio": m.start()})
            ultimo = n
        elif n == ultimo and articulos:
            a = articulos[-1]
            notas = [x.start() for x in _NOTA_RE.finditer(texto, a["inicio"], m.start())]
            a["inicio"] = notas[-1] if notas else m.start()
    for a, sig in zip(articulos, articulos[1:] + [None]):
        fin = sig["inicio"] if sig else len(texto)
        a["texto"] = re.sub(r"[ \t]+", " ", texto[a.pop("inicio"):fin]).strip()
    return [a for a in articulos if len(a["texto"]) > 20]


def _leer_pdf(data: bytes) -> list[str]:
    import fitz
    doc = fitz.open(stream=data, filetype="pdf")
    return [p.get_text() for p in doc]


async def _bajar(norma: dict) -> list[dict]:
    from scraper import _client
    async with _client() as c:
        r = await c.get(norma["url"], timeout=600)
        r.raise_for_status()
    paginas = await asyncio.to_thread(_leer_pdf, r.content)
    articulos = separar_articulos(paginas)
    if len(articulos) < 50:
        raise ValueError(f"{norma['nombre']}: solo {len(articulos)} artículos, el PDF no se leyó bien")
    return articulos


async def _cargar() -> dict:
    """{clave: [artículos]} desde el disco; baja lo que falta o venció."""
    global _cache
    DIR.mkdir(parents=True, exist_ok=True)
    datos = {}
    for norma in NORMAS:
        archivo = DIR / f"{norma['clave']}.json"
        vigente = archivo.exists() and time.time() - archivo.stat().st_mtime < REFRESCAR_CADA_S
        if not vigente:
            try:
                articulos = await _bajar(norma)
                archivo.write_text(json.dumps(articulos, ensure_ascii=False))
                logger.info("normas: %s guardada (%d artículos)", norma["nombre"], len(articulos))
            except Exception as e:
                logger.warning("normas: no se pudo bajar %s: %s", norma["nombre"], e)
        if archivo.exists():
            datos[norma["clave"]] = json.loads(archivo.read_text())
    _cache = _indexar(datos)
    return _cache


def _indexar(datos: dict) -> dict:
    """Índice BM25 en memoria: unos 800 artículos en total, alcanza y sobra."""
    docs = []
    for norma in NORMAS:
        for a in datos.get(norma["clave"], []):
            # El comienzo (número y título del artículo) pesa doble.
            raices = _raices(a["texto"]) + _raices(a["texto"][:160])
            docs.append({**a, "norma": norma["clave"], "tf": Counter(raices), "largo": len(raices)})
    df = Counter()
    for d in docs:
        df.update(d["tf"].keys())
    promedio = sum(d["largo"] for d in docs) / len(docs) if docs else 1
    return {"docs": docs, "df": df, "n": len(docs), "promedio": promedio,
            "normas": {k: len(v) for k, v in datos.items()}}


async def preparar():
    """Baja e indexa las normas en segundo plano al arrancar el servidor."""
    global _bajando
    if _cache is None and (_bajando is None or _bajando.done()):
        _bajando = asyncio.create_task(_cargar())
    try:
        await _bajando
    except Exception as e:
        logger.warning("normas: preparación falló: %s", e)


async def _indice(espera_s: float = 25) -> dict | None:
    if _cache is not None:
        return _cache
    global _bajando
    if _bajando is None or _bajando.done():
        _bajando = asyncio.create_task(_cargar())
    # asyncio.wait no cancela la descarga al vencer la espera ni deja el
    # "exception in shielded future" de Python 3.14 si después falla.
    # _cargar() deja el índice en _cache.
    await asyncio.wait({_bajando}, timeout=espera_s)
    if _bajando.done() and not _bajando.cancelled() and _bajando.exception():
        logger.warning("normas: no se pudo cargar el índice: %s", _bajando.exception())
    return _cache


_REF_RE = re.compile(r"\bart(?:[íi]culo|\.)?\s*(\d{1,3})\b", re.IGNORECASE)


def _norma_nombrada(pregunta: str) -> str | None:
    t = _sin_acentos(pregunta.lower())
    if "constituci" in t:
        return "constitucion"
    if "reglamento del senado" in t:
        return "senado"
    if "reglamento de la camara" in t or "reglamento de diputados" in t:
        return "diputados"
    if "reglamento del congreso" in t:
        return "congreso"
    return None


def _formato(d: dict) -> dict:
    nombre = next(n["nombre"] for n in NORMAS if n["clave"] == d["norma"])
    url = next(n["url"] for n in NORMAS if n["clave"] == d["norma"])
    texto = d["texto"]
    if len(texto) > MAX_CHARS_ARTICULO:
        texto = texto[:MAX_CHARS_ARTICULO].rsplit(" ", 1)[0] + " […]"
    return {"norma": nombre, "articulo": d["numero"], "pagina": d["pagina"], "texto": texto, "enlace": url}


async def buscar(consulta: str, norma: str | None = None, limite: int = MAX_ARTICULOS) -> dict:
    """Artículos que corresponden a la consulta, de la Constitución y los Reglamentos."""
    idx = await _indice()
    if not idx or not idx["docs"]:
        return {"sin_datos": True,
                "mensaje": "No se pudieron cargar la Constitución ni los Reglamentos oficiales en este momento."}
    norma = norma if norma in {n["clave"] for n in NORMAS} else _norma_nombrada(consulta)
    docs = [d for d in idx["docs"] if not norma or d["norma"] == norma]

    # Referencia exacta: "artículo 132 de la Constitución".
    exactos = []
    for m in _REF_RE.finditer(consulta):
        exactos += [d for d in docs if d["numero"] == m.group(1)][:2]
    if exactos:
        return {"fuente": "Constitución y Reglamentos oficiales (congreso.gob.pe)",
                "articulos": [_formato(d) for d in exactos[:limite]]}

    consulta_raices = set(_raices(_REF_RE.sub(" ", consulta)))
    if not consulta_raices:
        return {"sin_datos": True, "mensaje": "La consulta no tiene términos para buscar en las normas."}
    t = _sin_acentos(consulta.lower())
    k1, b = 1.4, 0.75
    puntajes = []
    for d in docs:
        s = 0.0
        for r in consulta_raices:
            f = d["tf"].get(r, 0)
            if not f:
                continue
            idf = math.log(1 + (idx["n"] - idx["df"][r] + 0.5) / (idx["df"][r] + 0.5))
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * d["largo"] / idx["promedio"]))
        # La cámara que nombra el usuario pesa más.
        if ("senad" in t and d["norma"] == "senado") or ("diputad" in t and d["norma"] == "diputados"):
            s *= 1.3
        if s > 0:
            puntajes.append((s, d))
    puntajes.sort(key=lambda x: -x[0])
    elegidos, por_norma = [], Counter()
    for s, d in puntajes:
        if por_norma[d["norma"]] >= 3:
            continue
        elegidos.append(d)
        por_norma[d["norma"]] += 1
        if len(elegidos) >= limite:
            break
    if not elegidos:
        return {"sin_datos": True, "mensaje": "No encontré artículos sobre eso en la Constitución ni en los Reglamentos."}
    return {"fuente": "Constitución y Reglamentos oficiales (congreso.gob.pe)",
            "articulos": [_formato(d) for d in elegidos]}
