"""
Scrapers for Congreso de la República del Perú.
- Proyectos de ley: SPLEY API (api.congreso.gob.pe/spley-portal-service)
- Sesiones / Agenda / Destacados: portales de Congreso/Senado/Diputados
- Noticias: sala de prensa oficial (comunicaciones.congreso.gob.pe)

Toda la información sale de dominios del Congreso. No se consultan
buscadores ni medios de prensa: si el Congreso no lo publicó, la
herramienta devuelve vacío en vez de rellenar con otra fuente.
"""
import asyncio
import base64
import logging
import math
import os
import re
import time
import unicodedata
import urllib.parse

import httpx

logger = logging.getLogger("congreso-ai.scraper")
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

from config import ahora_lima

TIMEOUT = 25
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "es-PE,es;q=0.9",
    "Referer": "https://wb2server.congreso.gob.pe/spley-portal/",
}

SPLEY_API  = "https://api.congreso.gob.pe/spley-portal-service"
CONGRESO   = "https://www.congreso.gob.pe"
# bicameral.congreso.gob.pe todavía no existe del lado del Congreso (404 ->
# wp-signup.php, típico de un subdominio de WordPress Multisite reservado
# pero no creado) — mientras tanto CONGRESO sigue siendo el sitio anterior,
# que está vivo. SENADO y DIPUTADOS sí están publicados y usan el mismo
# WordPress/plugin que CONGRESO (mismas clases widget_wc_widget_*).
SENADO     = "https://senado.congreso.gob.pe"
DIPUTADOS  = "https://diputados.congreso.gob.pe"
# Periodos parlamentarios de respaldo, del más nuevo al más viejo. Solo se usan
# si /periodo-parlamentario no responde; la lista real se resuelve en runtime.
PER_PAR_FALLBACK = [2026, 2021]
PER_PAR_ID = PER_PAR_FALLBACK[-1]   # periodo 2021-2026, base de los enlaces viejos

# codTipoParl es el discriminador de cámara de SPLEY. Sin mandarlo, la API
# responde solo con las proposiciones del Congreso (C) — al 19/08/2026 eso era
# 1 de 70: Diputados tenía 63 y Senado 6. Va en el body; como query param se
# ignora, igual que strBusqueda.
CAMARAS = {"C": "Congreso", "D": "Diputados", "S": "Senado"}
CAMARA_RUTA = {"C": "congreso", "D": "diputados", "S": "senado"}
# El bicameralismo arranca con el periodo 2026-2031; antes solo existía "C", así
# que para periodos viejos consultar D y S es tráfico tirado a la basura.
PER_PAR_BICAMERAL = 2026

# Ventana de "noticia reciente". Coincide con la del resumen semanal
# (services.orchestrator.RESUMEN_DIAS).
DIAS_NOTICIAS_RECIENTES = 7


# ── Helpers ────────────────────────────────────────────────────

def _client():
    # verify=False: los servidores del Congreso (api.congreso.gob.pe, wb2server, comunicaciones)
    # presentan certs autofirmados o caducados — sin esto fallan todas las llamadas.
    #
    # El hook de request es el candado de dominios a nivel de cliente: corre
    # en CADA request, incluidas las redirecciones que sigue httpx, así que
    # una URL oficial que redirige afuera también se corta.
    return httpx.AsyncClient(timeout=TIMEOUT, verify=False,
                             follow_redirects=True, headers=HEADERS,
                             event_hooks={"request": [_solo_congreso]})


# ── Lista blanca de dominios ─────────────────────────────────────────────────
# Casi todos los pedidos van a una URL constante de acá arriba, pero no todos:
# el adjunto de un expediente, las síntesis de agenda y la agenda del Pleno se
# piden a enlaces sacados del HTML/JSON que devuelve el propio Congreso. Si una
# de esas páginas trae un enlace externo — un banner, un acortador, un dominio
# que cambió de manos — el scraper lo seguiría sin preguntar y el modelo
# terminaría resumiendo contenido de un tercero como si fuera oficial.
#
# Vale la pena el candado porque _client() va con verify=False (certs
# autofirmados del Congreso): fuera de esos dominios, sin validar el
# certificado, no hay ninguna garantía de con quién se está hablando.
DOMINIO_OFICIAL = "congreso.gob.pe"


class DominioNoPermitido(httpx.RequestError):
    """Se intentó salir del dominio del Congreso."""


async def _solo_congreso(request: httpx.Request) -> None:
    if not url_permitida(str(request.url)):
        logger.warning("Request bloqueado (no es del Congreso): %s", request.url)
        raise DominioNoPermitido(f"dominio no permitido: {request.url.host}", request=request)


def url_permitida(url: str) -> bool:
    """True solo si la URL es del dominio del Congreso o un subdominio suyo."""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    # El chequeo es por sufijo con el punto: "congreso.gob.pe.evil.com" no pasa,
    # y "notcongreso.gob.pe" tampoco.
    return host == DOMINIO_OFICIAL or host.endswith("." + DOMINIO_OFICIAL)


_url_permitida = url_permitida   # nombre viejo, lo usan los tests


async def _get_oficial(c, url, **kw):
    """
    GET que solo sale hacia el dominio del Congreso.

    Devuelve None si la URL apunta a otro lado, para que quien llama lo saltee
    y siga con el resto en vez de romper la respuesta entera.
    """
    if not url_permitida(url):
        logger.warning("Enlace externo descartado (no es del Congreso): %s", url)
        return None
    return await c.get(url, **kw)

def _fmt_date(s):
    """
    Fecha a dd/mm/aaaa, el formato que usa el portal del Congreso.

    OJO con el recorte: SPLEY devuelve '2026-08-21T00:00:00.000-05:00', que
    son 29 caracteres. El código cortaba en [:25] y partía el huso horario a
    la mitad ('...000-05'), así que NINGÚN formato casaba y todo caía al
    fallback [:10] — o sea que las fechas de SPLEY salían en ISO
    ('2026-08-21') en las tablas y en los expedientes, en vez de 21/08/2026.
    fromisoformat se come el string completo sin recortar nada.
    """
    if not s:
        return ""
    txt = str(s).strip()
    try:
        return datetime.fromisoformat(txt).strftime("%d/%m/%Y")
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(txt[:26], fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    logger.debug("_fmt_date no pudo parsear %r", txt[:40])
    return txt[:10]


_MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def _fecha_desde_titulo_es(titulo: str):
    """
    Extrae una fecha de un título tipo 'Agenda del Pleno de la sesión del
    martes 23 de junio de 2026.' — devuelve un date() o None si no matchea.
    """
    m = re.search(r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", titulo or "", re.IGNORECASE)
    if not m:
        return None
    dia, mes_txt, anio = m.groups()
    mes = _MESES_ES.get(mes_txt.lower())
    if not mes:
        return None
    try:
        return date(int(anio), mes, int(dia))
    except ValueError:
        return None


def _antiguedad_dias(pubdate_raw: str):
    """
    Días transcurridos desde una fecha de publicación.

    Acepta dos formatos, porque conviven dos fuentes de fecha:
      - "2026-08-21 18:15[:03]" — el post_date de la API de noticias del
        Congreso, que es la fuente actual.
      - "Mon, 23 Jun 2026 10:00:00 GMT" — RFC-822, el pubDate de RSS.
        Se mantiene por si alguna fuente del Congreso vuelve a servir RSS.

    Devuelve None si la fecha no se puede parsear — quien filtre decide qué
    hacer con eso. OJO: sin el formato ISO acá, el filtro `dias` descartaba
    TODAS las noticias oficiales (parsedate_to_datetime no lo entiende y
    devolvía None), o sea que el resumen semanal se quedaba sin noticias.
    """
    from email.utils import parsedate_to_datetime

    if not pubdate_raw:
        return None

    dt = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(pubdate_raw.strip()[:19], fmt)
            break
        except ValueError:
            continue
    if dt is None:
        try:
            dt = parsedate_to_datetime(pubdate_raw)
        except (TypeError, ValueError):
            return None

    ahora = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return (ahora - dt).days


# Sala de prensa oficial del Congreso (WP REST API). Es lo que alimenta el
# widget de noticias de las portadas de congreso/senado/diputados, que en el
# HTML llega vacío con "Loading..." porque lo rellena JavaScript.
NOTICIAS_API = "https://comunicaciones.congreso.gob.pe/wp-json/noticias/v1"

# `destacado=` acepta una cámara, en singular para Diputados: "diputado" es
# lo que pide la portada de diputados.congreso.gob.pe ("diputados" da 404).
# Un 404 con code "no_destacados" es "vacío", no un fallo.
_NOTICIAS_CAMARAS = ("congreso", "senado", "diputado")


async def _noticias_oficiales() -> list[dict]:
    """
    Todas las noticias publicadas por el Congreso: las últimas más las
    destacadas de cada cámara, deduplicadas por enlace.

    Sustituye a la búsqueda en Google News. La diferencia de fondo: esta API
    NO busca por palabra clave, devuelve lo último publicado — así que el
    filtrado por tema se hace acá, sobre el texto que ya vino. Es el mismo
    criterio que _fetch_spley_por_materia usa con SPLEY, que tampoco filtra.
    """
    async def _traer(c, url):
        try:
            r = await c.get(url)
            if r.status_code != 200:      # 404 = "no hay destacadas", no error
                return []
            return r.json().get("noticias") or []
        except Exception as _e:
            logger.debug("noticias oficiales (%s): %s", url, _e)
            return []

    urls = [f"{NOTICIAS_API}/ultimos/"]
    urls += [f"{NOTICIAS_API}/destacados?destacado={cam}&cantidad=10"
             for cam in _NOTICIAS_CAMARAS]

    async with _client() as c:
        tandas = await asyncio.gather(*(_traer(c, u) for u in urls))

    vistas, salida = set(), []
    for tanda in tandas:
        for n in tanda:
            enlace = (n.get("post_link") or "").strip()
            titulo = (n.get("post_title") or "").strip()
            if not titulo or enlace in vistas:
                continue
            vistas.add(enlace)
            resumen = re.sub(r"<[^>]+>", "", n.get("post_content") or "").strip()
            salida.append({
                "titulo":  titulo,
                "fecha":   (n.get("post_date") or "")[:16],
                "fuente":  "Congreso de la República",
                "resumen": resumen[:300],
                "enlace":  enlace,
            })
    return salida


async def _noticias_congreso(query: str, max_results: int = 15,
                             dias: int | None = None):
    """
    Noticias oficiales del Congreso que coinciden con `query`.

    Reemplazo directo de la vieja _google_news: misma firma y mismos campos de
    salida, pero la fuente es la sala de prensa del Congreso en vez del índice
    de Google. Consecuencia esperada y buscada: devuelve menos, porque solo
    dice lo que el Congreso publicó. Antes se rellenaba con notas de prensa de
    medios y el informe las citaba como si fueran fuente parlamentaria.

    `dias` limita a lo publicado en los últimos N días; una noticia sin fecha
    parseable se descarta cuando hay filtro, igual que antes: no se puede
    afirmar que esté dentro de la ventana.
    """
    noticias = await _noticias_oficiales()

    # Palabras de 4+ letras: las cortas ("de", "el", "ley") matchean todo.
    claves = [p for p in re.findall(r"\w+", query.lower()) if len(p) > 3]

    resultados, descartadas = [], 0
    for n in noticias:
        if dias is not None:
            antiguedad = _antiguedad_dias(n["fecha"])
            if antiguedad is None or antiguedad > dias:
                descartadas += 1
                continue
        if claves:
            texto = f"{n['titulo']} {n['resumen']}".lower()
            if not any(k in texto for k in claves):
                continue
        resultados.append(n)
        if len(resultados) >= max_results:
            break

    if descartadas:
        logger.info("_noticias_congreso(%r): %d descartadas por antigüedad (>%d días)",
                    query, descartadas, dias)
    return resultados


# ── Proyectos de ley ───────────────────────────────────────────

_periodos_cache: list[int] | None = None
_periodos_lock = asyncio.Lock()


async def _periodos() -> list[int]:
    """
    perParId de los periodos parlamentarios, del más reciente al más antiguo.

    No puede quedar hardcodeado: el 27/07/2026 arrancó el periodo 2026-2031 y
    todo lo que seguía pidiendo perParId=2021 dejó de ver proyectos nuevos
    (el último que devolvía era del 22/07/2026). Se resuelve contra la API y se
    cachea por proceso — la lista cambia una vez cada cinco años.
    """
    global _periodos_cache
    if _periodos_cache is not None:
        return _periodos_cache

    async with _periodos_lock:
        if _periodos_cache is not None:
            return _periodos_cache
        try:
            async with _client() as c:
                r = await c.get(f"{SPLEY_API}/periodo-parlamentario")
                if r.status_code == 200:
                    ids = [p["perParId"] for p in r.json().get("data", [])
                           if p.get("perParId")]
                    if ids:
                        _periodos_cache = sorted(set(ids), reverse=True)
                        logger.info("periodos parlamentarios: %s", _periodos_cache)
                        return _periodos_cache
        except Exception as _e:
            logger.debug("periodo-parlamentario falló, uso fallback: %s", _e)

        _periodos_cache = list(PER_PAR_FALLBACK)
        return _periodos_cache


def _num_proyecto(numero) -> str:
    """
    Número de proyecto normalizado para comparar.

    Conviven dos formatos: '14864/2025-CR' (periodo 2021-2026) y
    '00001-2026-2031-CR' (2026-2031). El nuevo lleva ceros a la izquierda, así
    que se toman los dígitos iniciales sin ceros: '14864' y '1'.
    """
    m = re.match(r"\s*0*(\d+)", str(numero or ""))
    return m.group(1) if m else str(numero or "").strip()


def _periodo_desde_numero(numero) -> str:
    """
    Periodo parlamentario que viene dentro del número: '00006-2026-2031-S' ->
    '2026-2031'. Devuelve '' si el número es del formato viejo ('14864/2025-CR',
    que no lo lleva) — mejor vacío que afirmar un periodo equivocado.
    """
    m = re.search(r"-(\d{4}-\d{4})-", str(numero or ""))
    return m.group(1) if m else ""


def _periodo_desde_perpar(per_par) -> str:
    """
    Periodo a partir del perParId de SPLEY: 2021 -> '2021-2026'. Se usa para
    los números del formato viejo ('14864/2025-CR'), que no lo llevan dentro.
    Los periodos parlamentarios duran 5 años.
    """
    try:
        ini = int(per_par)
    except (TypeError, ValueError):
        return ""
    return f"{ini}-{ini + 5}"


def _camara_de_numero(numero) -> str | None:
    """
    Cámara deducida del sufijo del número: '00040-2026-2031-CD' → 'D'.

    En el periodo bicameral la numeración reinicia en cada cámara, así que
    '00001' existe tres veces y el sufijo es lo único que las separa. En
    2021-2026 el sufijo era el tipo de proponente (PE, GR, GL...), no una
    cámara; por eso solo se aceptan los tres códigos válidos y cualquier otro
    devuelve None, o sea "sin restricción".
    """
    m = re.search(r"-(CR|CD|S)\s*$", str(numero or "").upper())
    return {"CR": "C", "CD": "D", "S": "S"}[m.group(1)] if m else None


def _periodos_de_numero(numero) -> list[int] | None:
    """
    Periodo donde hay que buscar un número de proyecto, o None si no se sabe.

    '14864/2025-CR' es del formato viejo, que solo existe en 2021-2026;
    '00040-2026-2031-CD' lleva el periodo adentro. Sin esto la búsqueda por
    número recorría los periodos del más nuevo al más viejo y se conformaba
    con los 300 del 2026-2031 (ya son más de 300), así que todo proyecto del
    periodo anterior salía como "no encontrado".
    """
    texto = str(numero or "")
    m = re.search(r"-(\d{4})-\d{4}-", texto)
    if m:
        return [int(m.group(1))]
    if "/" in texto:
        return [PER_PAR_ID]
    return None


async def _spley_proyectos(c, payload: dict, min_items: int = 1,
                           periodos: list[int] | None = None) -> list[dict]:
    """
    Corre lista-con-filtro sobre los periodos parlamentarios, del más nuevo al
    más viejo, y devuelve los proyectos anotados con su periodo en `_perPar`.
    `periodos` restringe la búsqueda (ver _periodos_de_numero).

    Sigue al periodo anterior en vez de cortar en el primero que responde:
    2026-2031 abrió con un único proyecto, así que quedarse ahí dejaría la app
    casi vacía durante semanas. Ordena por fecha de presentación descendente
    para que la mezcla de periodos salga coherente.
    """
    out: list[dict] = []
    for per in periodos or await _periodos():
        camaras = list(CAMARAS) if per >= PER_PAR_BICAMERAL else ["C"]
        for cod in camaras:
            try:
                r = await c.post(f"{SPLEY_API}/proyecto-ley/lista-con-filtro",
                                 json={**payload, "perParId": per,
                                       "codTipoParl": cod})
                if r.status_code != 200:
                    continue
                for p in r.json().get("data", {}).get("proyectos", []):
                    p["_perPar"] = per
                    p["_camara"] = cod
                    out.append(p)
            except Exception as _e:
                logger.debug("spley periodo %s cámara %s: %s", per, cod, _e)
        if len(out) >= min_items:
            break

    out.sort(key=lambda p: str(p.get("fecPresentacion") or ""), reverse=True)
    return out


# Palabras que no dicen nada del tema: con ellas como palabra clave, "ley de
# salud" encontraba cualquier proyecto (todos dicen "LEY").
_STOPWORDS_MATERIA = {
    "LOS", "LAS", "DEL", "PARA", "POR", "CON", "SIN", "QUE", "UNA", "UNO", "UNOS",
    "UNAS", "SOBRE", "ENTRE", "HACIA", "DESDE", "COMO", "MAS", "SUS", "ESTE", "ESTA",
    "LEY", "LEYES", "PROYECTO", "PROYECTOS", "PROPOSICION", "PROPOSICIONES",
    "LEGISLATIVA", "LEGISLATIVAS", "TEMA", "TEMAS", "MATERIA", "RELACIONADO",
    "RELACIONADOS", "REFERIDO", "REFERIDOS", "CONGRESO", "PERU",
}


def _sin_acentos(texto: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", texto)
                   if unicodedata.category(ch) != "Mn").upper()


def _keywords_materia(materia: str) -> list[str]:
    palabras = re.findall(r"\w+", _sin_acentos(materia or ""))
    return [w for w in palabras if len(w) > 2 and w not in _STOPWORDS_MATERIA]


def _coincide_materia(titulo: str, keywords: list[str]) -> bool:
    """
    True si TODAS las palabras clave aparecen como inicio de alguna palabra
    del título ("MENTAL" encuentra "MENTALES" pero no "INSTRUMENTALIZACIÓN").

    Antes era subcadena con ANY: "salud mental" traía el proyecto de crimen
    organizado por "instruMENTALización", el de rocas "ornaMENTALes" y
    cualquier proyecto con "SALUD" — y el modelo los presentaba como
    proyectos de salud mental.
    """
    if not keywords:
        return False
    palabras = re.findall(r"\w+", _sin_acentos(titulo or ""))
    return all(any(w.startswith(kw) for w in palabras) for kw in keywords)


async def _fetch_spley_por_materia(materia: str, limit: int = 20, camara: str = None):
    """
    SPLEY ignora strBusqueda (devuelve los recientes igual), así que se traen
    los ~300 proyectos más recientes y se filtra por título de este lado.
    """
    keywords = _keywords_materia(materia)
    if not keywords:
        return None

    # min_items=300 fuerza a barrer todos los periodos: el actual todavía tiene
    # muy pocos proyectos y una búsqueda por materia sin el periodo anterior no
    # encontraría nada.
    try:
        async with _client() as c:
            all_items = await _spley_proyectos(c, {"page": 0, "size": 300},
                                               min_items=300)
        if not all_items:
            return None
    except Exception as _e:
        logger.warning("Búsqueda por materia %r falló: %s", materia, _e)
        return None

    # "Educación en el Senado": sin esto la cámara se ignoraba y salían
    # proyectos de Diputados.
    cod_camara = _codigo_camara(camara)
    if cod_camara:
        all_items = [p for p in all_items if p.get("_camara") == cod_camara]
    matches = [p for p in all_items if _coincide_materia(p.get("titulo"), keywords)]
    criterio = (f"Títulos que contienen TODAS estas palabras: {', '.join(keywords)}. "
                f"Revisados los {len(all_items)} proyectos más recientes de SPLEY; "
                f"los más antiguos no se revisaron.")
    # Una lista de sinónimos ("hospitales medicamentos medicos...") casi nunca
    # aparece entera en un título. Ahí, y solo ahí, se acepta cualquiera de
    # las palabras, avisándolo. Con una o dos palabras ("salud mental") no:
    # traería todo lo de "salud" presentado como salud mental.
    if not matches and len(keywords) >= 3:
        matches = [p for p in all_items
                   if any(_coincide_materia(p.get("titulo"), [kw]) for kw in keywords)]
        criterio = (f"COINCIDENCIA PARCIAL: ningún título contiene todas las palabras; "
                    f"se muestran los que contienen AL MENOS UNA de: {', '.join(keywords)}. "
                    f"Revisados los {len(all_items)} proyectos más recientes de SPLEY. "
                    f"Aclarale al usuario que es una coincidencia parcial.")
    logger.info("Materia %r: %d de %d títulos coinciden", materia, len(matches), len(all_items))

    if not matches:
        return {"sin_datos": True,
                "criterio": criterio,
                "mensaje": (f"Ningún título de los {len(all_items)} proyectos más recientes "
                            f"trata sobre '{materia}'. No inventes proyectos: decile al "
                            f"usuario que no se encontraron.")}

    resultado = _format_proyectos(matches[:limit], total_disponible=len(matches))
    return {"criterio": criterio, **resultado}


async def fetch_proyectos(autor=None, comision=None, numero=None, materia=None,
                          legislatura="2021-2026", limit=None, dias=None, camara=None):
    from datetime import timedelta

    # `dias` es una ventana temporal cerrada ("los últimos 15 días"), no una
    # muestra: con el default de 20 una consulta legítima devolvía 20 de 89 y
    # marcaba truncado=true, así que el modelo no podía armar el cuadro por
    # temas y terminaba preguntándole al usuario si reintentaba. Pedir un
    # rango de fechas es pedir todo lo que cae adentro.
    if limit is None:
        limit = 100 if dias else 20

    async with _client() as c:

        # Materia: SPLEY ignores strBusqueda for topic searches — use client-side filter
        if materia and not dias:
            result = await _fetch_spley_por_materia(materia, limit, camara=camara)
            if result is not None:
                return result

        # Con dias: traer bastante más de lo pedido, porque el filtro por
        # fecha descarta del lado del cliente. El techo era 100, el mismo
        # número que ahora puede pedir `limit`, así que una ventana con más
        # de 100 proyectos quedaba recortada por el fetch antes de llegar
        # siquiera al filtro.
        fetch_size = 300 if dias else limit
        payload: dict = {"page": 0, "size": fetch_size}

        # SPLEY ignora strBusqueda por completo (hasta un texto inexistente
        # devuelve el catálogo entero), así que número y autor se filtran del
        # lado del cliente igual que materia. Mandarlo como filtro devolvía los
        # proyectos más recientes disfrazados de resultado de la búsqueda.
        filtro_local = None
        if numero:
            objetivo = _num_proyecto(numero)
            # El número solo es único dentro de su periodo: "00001" existe tanto
            # en 2021-2026 como en 2026-2031. Si el texto trae el periodo
            # ("00001-2026-2031-CR") se usa para desambiguar.
            m_per = re.search(r"(20\d{2})\s*-\s*20\d{2}", str(numero))
            per_pedido = int(m_per.group(1)) if m_per else None
            cam_pedida = _camara_de_numero(numero)

            def filtro_local(p):
                if _num_proyecto(p.get("proyectoLey") or p.get("pleyNum")) != objetivo:
                    return False
                if per_pedido is not None and p.get("_perPar") != per_pedido:
                    return False
                return cam_pedida is None or p.get("_camara") == cam_pedida
        elif materia and _keywords_materia(materia):
            # Solo llega acá con `dias` (materia sola sale arriba). Antes el
            # tema se descartaba en silencio y "salud de los últimos 15 días"
            # devolvía los 15 días enteros: el modelo presentaba un proyecto
            # de CTS como si fuera de salud.
            kws = _keywords_materia(materia)

            def filtro_local(p):
                return _coincide_materia(p.get("titulo"), kws)
        elif autor:
            tokens = [t for t in re.split(r"\s+", autor.upper()) if len(t) > 2]

            def filtro_local(p):
                # `autores` viene como "Luque Ibarra, Ruth" (apellidos primero),
                # así que comparar contra el crudo hacía fallar "Ruth Luque".
                # Se normaliza al orden natural y se exige que estén todas las
                # palabras del nombre buscado, en cualquier orden.
                texto = (f"{_normalizar_lista_autores(p.get('autores'))} "
                         f"{p.get('desProponente') or ''}").upper()
                return bool(tokens) and all(t in texto for t in tokens)

        if filtro_local:
            # El filtro corre sobre el catálogo completo, no sobre `limit`.
            payload["size"] = 300
            fetch_size = 300
        elif comision:
            try:
                rc = await c.get(f"{SPLEY_API}/comisiones")
                if rc.status_code == 200:
                    comisiones = rc.json().get("data", [])
                    match = next(
                        (x for x in comisiones
                         if comision.lower() in x.get("nombreComision", "").lower()),
                        None
                    )
                    if match:
                        payload["comisionId"] = match["comisionId"]
                    else:
                        payload["strBusqueda"] = comision
            except Exception as _e:
                logger.debug("comision lookup failed, fallback to text: %s", _e)
                payload["strBusqueda"] = comision

        try:
            # Por autor se recorren todos los periodos: con el corte en
            # fetch_size se quedaba en 2026-2031 (ya pasa de 300) y a Ruth
            # Luque le salían 2 proyectos, sin ninguno de su periodo anterior.
            items = await _spley_proyectos(
                c, payload, min_items=10**9 if autor and not numero else fetch_size,
                periodos=_periodos_de_numero(numero) if numero else None)
            if items:
                if filtro_local:
                    items = [p for p in items if filtro_local(p)]

                # "Novedades de Diputados": en el periodo bicameral cada
                # proyecto trae su cámara (_camara), y sin filtro se mezclaban.
                cod_camara = _codigo_camara(camara)
                if cod_camara:
                    items = [p for p in items if p.get("_camara") == cod_camara]

                # Filtrar por fecha si se pidió
                if dias:
                    # Normalizado a medianoche: las fechas de SPLEY vienen a
                    # medianoche (sin hora real), así que comparar contra
                    # "ahora mismo" (con hora) excluía de forma inconsistente
                    # ítems del propio día límite según a qué hora se
                    # corriera la consulta — un proyecto de "hoy 00:00" podía
                    # quedar afuera de "últimos 15 días" si ya eran las 11pm.
                    hoy_medianoche = ahora_lima().replace(hour=0, minute=0, second=0, microsecond=0)
                    cutoff = hoy_medianoche - timedelta(days=int(dias))
                    def _parse_raw(s):
                        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                            try:
                                return datetime.strptime(str(s)[:19], fmt)
                            except ValueError:
                                continue   # probar el siguiente formato
                        return None
                    items = [p for p in items
                             if _parse_raw(p.get("fecPresentacion") or "") is not None
                             and _parse_raw(p.get("fecPresentacion")) >= cutoff]

                if not items:
                    # Distinguir "no hay nada que cumpla el filtro" de un fallo
                    # de red: devolver una lista vacía dejaba al modelo sin nada
                    # que decir y respondía en vago.
                    return {"sin_datos": True,
                            "mensaje": _mensaje_sin_proyectos(
                                autor=autor, comision=comision, numero=numero,
                                materia=materia, dias=dias)}
                return _format_proyectos(items[:limit], total_disponible=len(items))
        except Exception as _e:
            logger.warning("fetch_proyectos: %s: %s", type(_e).__name__, _e)

        return {
            "error": "No se pudo conectar con el sistema SPLEY del Congreso. "
                     "Consulta https://wb2server.congreso.gob.pe/spley-portal/"
        }


def _codigo_camara(camara) -> str | None:
    """'diputados' → 'D', 'Senado' → 'S', 'congreso' → 'C'; None si no se reconoce."""
    t = _sin_acentos(camara or "")
    if "DIPUTAD" in t:
        return "D"
    if "SENAD" in t:
        return "S"
    if "CONGRES" in t:
        return "C"
    return None


def _mensaje_sin_proyectos(autor=None, comision=None, numero=None,
                           materia=None, dias=None) -> str:
    """Explica por qué la búsqueda quedó vacía, con el criterio que la vació."""
    if numero:
        return f"No se encontró el proyecto de ley {numero}."
    criterios = []
    if autor:
        criterios.append(f"del autor '{autor}'")
    if comision:
        criterios.append(f"en la comisión '{comision}'")
    if materia:
        criterios.append(f"sobre '{materia}'")
    if dias:
        criterios.append(f"presentados en los últimos {dias} días")
    detalle = " ".join(criterios) if criterios else "con esos criterios"
    return (f"No se encontraron proyectos de ley {detalle}. "
            "El periodo parlamentario 2026-2031 recién comenzó el 27/07/2026, "
            "así que todavía hay muy pocos proyectos presentados.")


SPLEY_PORTAL_BASE = "https://wb2server.congreso.gob.pe/spley-portal/#"
SPLEY_PORTAL = f"{SPLEY_PORTAL_BASE}/expediente"


def _enlace_expediente(p: dict) -> str:
    """
    Enlace al expediente en el portal SPLEY.

    El portal enruta por cámara (#/diputados/expediente/..., #/senado/...,
    #/congreso/...) y el periodo va en la URL, así que ambos salen del propio
    proyecto (`_camara` y `_perPar`, puestos por _spley_proyectos). Con valores
    fijos el enlace apuntaba a un expediente inexistente.
    """
    num = p.get("pleyNum") or ""
    if not num:
        return ""
    ruta = CAMARA_RUTA.get(p.get("_camara") or "C", "congreso")
    return f"{SPLEY_PORTAL_BASE}/{ruta}/expediente/{p.get('_perPar') or PER_PAR_ID}/{num}"


def _format_proyectos(items, total_disponible: int | None = None):
    out = []
    for p in items:
        num = p.get("pleyNum") or ""
        titulo = p.get("titulo") or ""
        item = {
            "numero":              p.get("proyectoLey") or num or "",
            "fecha_presentacion":  _fmt_date(p.get("fecPresentacion") or ""),
            "estado":              p.get("desEstado") or "",
            "titulo":              titulo,
            "sumilla":             p.get("sumilla") or titulo,
            "proponente":          p.get("desProponente") or "",
            "autor":               _normalizar_lista_autores(p.get("autores")) or p.get("desProponente") or "",
            "comision":            p.get("desComision") or "",
            "grupo_parlamentario": p.get("desGpar") or "",
            "legislatura":         p.get("desLegis") or "",
            "camara":              CAMARAS.get(p.get("_camara") or "C", "Congreso"),
            "enlace":              _enlace_expediente(p) or f"{SPLEY_PORTAL}/search",
        }

        # El listado de SPLEY no trae sumilla propia (solo el expediente la
        # tiene), así que la línea de arriba caía al título y cada ítem viajaba
        # con el mismo texto largo dos veces. En una consulta de 15 días eran
        # 15 mil caracteres de duplicado exacto, suficiente para empujar el
        # JSON por encima del tope y que al modelo le llegara cortado.
        # `comision`, `grupo_parlamentario` y `legislatura` vienen siempre
        # vacíos en este endpoint: la clave sola tampoco es gratis.
        if item["sumilla"] == titulo:
            del item["sumilla"]
        for k in ("comision", "grupo_parlamentario", "legislatura", "estado", "proponente"):
            if not item.get(k):
                item.pop(k, None)
        # Suele venir repetido del proponente cuando el proyecto no es de un
        # congresista ("Otros Poderes del Estado" en los dos campos).
        if item.get("proponente") and item["proponente"] == item.get("autor"):
            del item["proponente"]

        # El formato de salida ya solo muestra tres autores y "y N más" (ver
        # workflow_proyectos.md), así que mandar la lista entera es peso que el
        # modelo descarta igual: era el campo más pesado del listado, 224
        # caracteres por ítem contra 173 del título.
        # OJO: el separador es ";", no "," — lo pone _normalizar_lista_autores.
        # Los nombres propios no traen ";", así que cortar por ahí es seguro;
        # cortar por coma no lo era y además no encontraba nada.
        autores = [a.strip() for a in re.split(r"\s*;\s*", item.get("autor") or "") if a.strip()]
        if len(autores) > 3:
            item["autor"] = "; ".join(autores[:3])
            item["autores_restantes"] = len(autores) - 3

        out.append(item)
    # `total` es lo que se devuelve; `total_disponible` lo que había antes de
    # cortar por `limit`. Sin la distinción el modelo leía el total truncado y
    # afirmaba "van 20 en total" cuando eran 40, y sacaba conclusiones sobre las
    # cámaras a partir de la porción que le tocó ver.
    #
    # El orden importa: orchestrator.MAX_TOOL_RESULT_CHARS recorta el JSON
    # serializado, así que los metadatos van ANTES de `items` — puestos después
    # quedaban fuera del corte y el modelo nunca los leía.
    resumen: dict = {"fuente": "SPLEY — api.congreso.gob.pe", "total": len(out)}
    if total_disponible is not None and total_disponible > len(out):
        resumen["total_disponible"] = total_disponible
        resumen["truncado"] = True
        resumen["aviso"] = (
            f"Se muestran {len(out)} de {total_disponible} resultados que "
            f"cumplen el filtro. No afirmes que son todos: pide más con `limit` "
            f"si necesitas el total real o el desglose por cámara."
        )
    resumen["items"] = out
    return resumen


# ── Sesiones ───────────────────────────────────────────────────

async def fetch_sesiones(comision=None, fecha=None, limit=20):
    # Build a focused search query
    query = "sesiones comisiones congreso perú"
    if comision:
        query = f"sesión comisión {comision} congreso perú"
    if fecha:
        query += f" {fecha}"

    noticias = await _noticias_congreso(query, max_results=limit)

    if noticias:
        return {
            "fuente": "Noticias recientes del Congreso",
            "total": len(noticias),
            "items": noticias,
        }

    # Fallback: try comisiones2020 page HTML
    try:
        async with _client() as c:
            r = await c.get(f"{CONGRESO}/comisiones2020/")
            soup = BeautifulSoup(r.text, "html.parser")
            items = []
            for a in soup.select("a[href]")[:limit]:
                txt = a.get_text(strip=True)
                href = a.get("href", "")
                if len(txt) > 15:
                    items.append({"comision": txt, "enlace": href})
            if items:
                return {"fuente": "congreso.gob.pe/comisiones2020 (HTML)",
                        "total": len(items), "items": items}
    except Exception as _e:
        logger.warning("fetch_sesiones: %s: %s", type(_e).__name__, _e)

    return {
        "error": "No se pudo obtener información de sesiones. "
                 "Consulta https://wb2server.congreso.gob.pe/visor-sesiones/"
    }


# ── Agenda ─────────────────────────────────────────────────────

# ── Destacados ─────────────────────────────────────────────────

def _extract_widget_items(soup, widget_class):
    items = []
    widget = soup.find("div", class_=widget_class)
    if not widget:
        return items
    for a in widget.find_all("a", href=True):
        titulo = a.get_text(strip=True)
        enlace = a["href"].strip()
        if titulo and enlace:
            items.append({"titulo": titulo, "enlace": enlace})
    return items


async def _scrape_destacados_camara(url: str):
    """
    Scrapea la portada de una cámara (Congreso/Senado/Diputados) — todas
    corren el mismo WordPress y las mismas clases widget_wc_widget_feature_article
    / widget_wc_widget_citation_article, así que un solo parser sirve para las 3.

    `url` ya viene con la ruta correcta incluida: a diferencia de
    www.congreso.gob.pe (que sirve la portada en /home/ y también en /),
    senado.congreso.gob.pe y diputados.congreso.gob.pe solo responden 200 en
    la raíz — /home/ ahí da 404.
    """
    async with _client() as c:
        r = await c.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
        })
        if r.status_code != 200:
            raise Exception(f"HTTP {r.status_code}")

    soup = BeautifulSoup(r.text, "html.parser")
    destacados = _extract_widget_items(soup, "widget_wc_widget_feature_article")
    citaciones = _extract_widget_items(soup, "widget_wc_widget_citation_article")

    # Distinguir "esta cámara no publicó nada" de "el scraping se rompió". Los
    # widgets se renderizan en el servidor con el literal "No hay publicaciones
    # para mostrar" cuando están vacíos — antes eso se trataba como fallo y se
    # devolvían notas de prensa de medios como si fueran documentos oficiales.
    # Desde oct-2026 Diputados ya no imprime ese literal: deja el <ul> vacío.
    # Si los dos widgets están en la página pero sin links, también es vacío;
    # solo es "HTML cambió" cuando los widgets desaparecen.
    vacio_declarado = "No hay publicaciones para mostrar" in r.text
    widgets_presentes = all(
        soup.find("div", class_=cls)
        for cls in ("widget_wc_widget_feature_article", "widget_wc_widget_citation_article")
    )
    if not destacados and not citaciones and not (vacio_declarado or widgets_presentes):
        raise Exception("sin items y sin aviso de vacío — el HTML cambió")

    return {"fuente": url, "destacados": destacados, "citaciones": citaciones}


async def fetch_destacados():
    """
    Scrapea /home de Congreso, Senado y Diputados — secciones DESTACADO y
    CITACIONES con links de descarga de cada cámara.

    bicameral.congreso.gob.pe todavía no existe (ver nota junto a la
    constante CONGRESO) — se usa www.congreso.gob.pe como fuente "Congreso"
    hasta que el sitio nuevo esté publicado.
    """
    import asyncio

    fuentes = {
        "Congreso": f"{CONGRESO}/home/",
        "Senado": f"{SENADO}/",
        "Diputados": f"{DIPUTADOS}/",
    }
    resultados = await asyncio.gather(
        *(_scrape_destacados_camara(url) for url in fuentes.values()),
        return_exceptions=True,
    )

    camaras = {}
    fallidas = []
    for nombre, resultado in zip(fuentes.keys(), resultados):
        if isinstance(resultado, Exception):
            fallidas.append(nombre)
            logger.warning("%s: scrape de destacados falló: %s", nombre, resultado)
            continue
        camaras[nombre] = resultado
        if not resultado["destacados"] and not resultado["citaciones"]:
            camaras[nombre]["estado_fuente"] = (
                f"{nombre} no tiene publicaciones en DESTACADO ni en CITACIONES "
                "en este momento (sus widgets muestran 'No hay publicaciones "
                "para mostrar'). No es un fallo de conexión."
            )

    if not camaras:
        # Las 3 cámaras fallaron: fallback a la sala de prensa oficial del
        # Congreso (comunicaciones.congreso.gob.pe), solo de la última semana
        # para que notas viejas no se citen como actividad de esta semana.
        logger.warning("Scrape de destacados falló en las 3 cámaras: %s", fallidas)
        noticias = await _noticias_congreso(
            "congreso perú noticias destacados sesión pleno ley",
            max_results=10,
            dias=DIAS_NOTICIAS_RECIENTES,
        )
        resultado = {
            "sin_datos": True,
            "mensaje": "No se pudo leer DESTACADO ni CITACIONES en Congreso, Senado ni Diputados.",
            "documentos_disponibles": await _documentos_disponibles(),
            "noticias_relacionadas": noticias,
        }
        if not noticias:
            resultado["estado_noticias"] = (
                f"No hay noticias de prensa sobre el Congreso en los últimos "
                f"{DIAS_NOTICIAS_RECIENTES} días. Los documentos listados en "
                "documentos_disponibles son material histórico descargable, NO "
                "actividad de esta semana."
            )
        return resultado

    resultado = {"camaras": camaras}
    if fallidas:
        resultado["camaras_no_disponibles"] = fallidas
    if all(not c["destacados"] and not c["citaciones"] for c in camaras.values()):
        resultado["documentos_disponibles"] = await _documentos_disponibles()
    return resultado


async def _documentos_disponibles():
    """
    Documentos oficiales descargables que sí están publicados hoy.

    Se usa cuando DESTACADO/CITACIONES están vacíos, para poder ofrecer
    descargas reales en vez de una negativa.
    """
    docs = []
    try:
        agenda = await fetch_agenda_pleno()
        if agenda.get("enlace"):
            docs.append({
                "titulo": agenda.get("titulo") or "Agenda del Pleno vigente",
                "enlace": agenda["enlace"],
                "tipo": "Agenda del Pleno (PDF)",
            })
        # La última agenda de cada una de las otras cámaras.
        for otra in agenda.get("otras_agendas") or []:
            if otra.get("enlace"):
                docs.append({
                    "titulo": otra.get("titulo") or "Agenda del Pleno",
                    "enlace": otra["enlace"],
                    "tipo": f"Agenda del Pleno — {otra.get('camara', 'Congreso')} (PDF)",
                })
        for prev in (agenda.get("agendas_anteriores") or [])[:5]:
            if prev.get("enlace"):
                docs.append({
                    "titulo": prev.get("titulo") or "Agenda del Pleno anterior",
                    "enlace": prev["enlace"],
                    "tipo": "Agenda del Pleno anterior (PDF)",
                })
    except Exception as e:
        logger.warning("_documentos_disponibles: agenda del Pleno falló: %s", e)

    docs.extend([
        {"titulo": "Reglamento del Congreso de la República (setiembre 2025)",
         "enlace": "https://www3.congreso.gob.pe/Docs/constitucion/reglamento/reglamento%20setiembre-2025.pdf",
         "tipo": "Norma de referencia (PDF)"},
        {"titulo": "Constitución Política del Perú (dic. 2024)",
         "enlace": "https://www3.congreso.gob.pe/Docs/files/constitucion/constitucion-12-2024.pdf",
         "tipo": "Norma de referencia (PDF)"},
    ])
    return docs


# ── Congresista ────────────────────────────────────────────────

async def fetch_congresista(nombre: str):
    """Proyectos presentados + noticias recientes de un congresista."""
    import asyncio

    # Proyectos en SPLEY
    proyectos_task = fetch_proyectos(autor=nombre, limit=30)
    # Noticias de la sala de prensa del Congreso
    noticias_task  = _noticias_congreso(f"{nombre} congresista peru", max_results=10)

    proyectos, noticias = await asyncio.gather(proyectos_task, noticias_task)

    # Agrupar proyectos por estado
    resumen_estados: dict = {}
    if "items" in proyectos:
        for p in proyectos["items"]:
            estado = p.get("estado") or "Sin estado"
            resumen_estados[estado] = resumen_estados.get(estado, 0) + 1

    return {
        "congresista": nombre,
        "proyectos": proyectos,
        "resumen_estados": resumen_estados,
        "noticias_recientes": noticias,
        "perfil_url": f"https://wb2server.congreso.gob.pe/spley-portal/#/busqueda?autor={urllib.parse.quote(nombre)}",
    }


# ── Rastrear proyecto específico ───────────────────────────────

async def fetch_estado_proyecto(numero: str):
    """Estado detallado de un proyecto de ley por número."""
    # Número normalizado: "14860/2025-CR" → "14860", "00001-2026-2031-CR" → "1"
    num_clean = _num_proyecto(numero)
    async with _client() as c:
        try:
            items = await _spley_proyectos(c, {"page": 0, "size": 300},
                                           min_items=300,
                                           periodos=_periodos_de_numero(numero))
            if items:
                # Match exacto por número. Antes era `num_clean in proyectoLey`,
                # que con números cortos matcheaba cualquier proyecto que los
                # contuviera como substring y devolvía uno equivocado.
                exact = next(
                    (p for p in items
                     if _num_proyecto(p.get("proyectoLey") or p.get("pleyNum")) == num_clean),
                    None
                )
                if exact:
                    p   = exact
                    num = p.get("pleyNum") or ""
                    return {
                        "numero":        p.get("proyectoLey") or num,
                        "titulo":        p.get("titulo") or "",
                        "estado":        p.get("desEstado") or "",
                        "fecha_ingreso": _fmt_date(p.get("fecPresentacion") or ""),
                        "autor":         _normalizar_lista_autores(p.get("autores")) or p.get("desProponente") or "",
                        "comision":      p.get("desComision") or "",
                        "sumilla":       p.get("sumilla") or p.get("titulo") or "",
                        "enlace":        _enlace_expediente(p),
                        "fuente":        "SPLEY — api.congreso.gob.pe",
                    }
        except Exception as _e:
            # Un fallo de red NO es "no existe": antes las dos cosas salían con
            # el mismo "No se encontró el proyecto" y el modelo afirmaba que el
            # número era incorrecto cuando SPLEY simplemente no respondió.
            logger.warning("fetch_estado_proyecto: %s: %s", type(_e).__name__, _e)
            return {"error": "No se pudo consultar SPLEY en este momento "
                             f"({type(_e).__name__}). No se sabe si el proyecto '{numero}' existe."}

    return {"sin_datos": True,
            "mensaje": (f"El proyecto '{numero}' no está entre los 300 más recientes de SPLEY. "
                        "Puede ser más antiguo o el número puede estar mal: prueba con "
                        "fetch_expediente, que consulta el expediente directo.")}


# ── Videos YouTube del Congreso ────────────────────────────────

YT_CHANNEL = "https://www.youtube.com/@congresodelarepublicaperu/streams"

async def fetch_videos_youtube(limit=20):
    """Lista los videos más recientes del canal oficial del Congreso."""
    import asyncio

    import yt_dlp

    def _extract():
        opts = {
            "quiet":        True,
            "no_warnings":  True,
            "extract_flat": True,
            "playlist_items": f"1-{limit}",
            **_ydl_cookie_opts(),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(YT_CHANNEL, download=False)
            entries = info.get("entries") or []
            videos = []
            for e in entries:
                vid_id      = e.get("id") or ""
                title       = e.get("title") or ""
                duration    = e.get("duration")
                timestamp   = e.get("timestamp") or e.get("release_timestamp")
                live_status = e.get("live_status") or "not_live"
                # "is_upcoming" es una transmisión programada que todavía no
                # empezó: contarla como en vivo ponía "En vivo ahora" en el
                # panel antes de tiempo.
                is_live     = live_status == "is_live"
                programado  = live_status == "is_upcoming"
                was_live    = live_status in ("was_live", "post_live")
                fecha = ""
                if timestamp:
                    fecha = datetime.utcfromtimestamp(timestamp).strftime("%d/%m/%Y")
                dur_str = ""
                try:
                    if duration:
                        total_s = int(duration)
                        h, rem = divmod(total_s, 3600)
                        m, s   = divmod(rem, 60)
                        dur_str = f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"
                except (TypeError, ValueError):
                    pass
                videos.append({
                    "id":        vid_id,
                    "titulo":    title,
                    "fecha":     fecha,
                    "duracion":  dur_str,
                    "en_vivo":   is_live,
                    "fue_live":  was_live,
                    "programado": programado,
                    "url":       f"https://www.youtube.com/watch?v={vid_id}" if vid_id else "",
                    "thumb":     f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg" if vid_id else "",
                })
            return videos

    loop = asyncio.get_event_loop()
    try:
        videos = await loop.run_in_executor(None, _extract)
        return {"ok": True, "videos": videos}
    except Exception as e:
        return {"ok": False, "error": str(e)}


import sys

COOKIE_PATHS = [
    os.path.expanduser("~/youtube.cookies"),
    os.path.expanduser("~/youtube_cookies.txt"),
    os.path.expanduser("~/Downloads/youtube.cookies"),
]

# On macOS, yt-dlp can read browser cookies directly from the OS keychain
_MAC_BROWSERS = ["chrome", "safari", "firefox", "chromium"]
_cookie_opts_cache = None


def _get_cookie_path():
    for p in COOKIE_PATHS:
        if os.path.exists(p):
            return p
    return None


def _ydl_cookie_opts():
    """Returns cookie options for yt-dlp. Cached after first call."""
    global _cookie_opts_cache
    if _cookie_opts_cache is not None:
        return _cookie_opts_cache

    # Cookie file always wins
    path = _get_cookie_path()
    if path:
        _cookie_opts_cache = {"cookiefile": path}
        return _cookie_opts_cache

    # On macOS, probe which browser is available and cache it
    if sys.platform == "darwin":
        import yt_dlp
        for browser in _MAC_BROWSERS:
            try:
                from yt_dlp.cookies import extract_cookies_from_browser

                class _SilentLogger:
                    def debug(self, *a): pass
                    def warning(self, *a): pass
                    def error(self, *a): pass

                jar = extract_cookies_from_browser(browser, None, _SilentLogger())
                if jar is not None:
                    _cookie_opts_cache = {"cookiesfrombrowser": (browser,)}
                    return _cookie_opts_cache
            except Exception as _e:
                logger.debug("cookie extract failed for browser: %s", _e)
                continue

    _cookie_opts_cache = {}
    return _cookie_opts_cache


def _parse_vtt(content: str) -> str:
    """Extract plain text from a VTT subtitle file."""
    lines, seen = [], set()
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("WEBVTT") or "-->" in line:
            continue
        # Remove HTML tags and timestamps
        clean = re.sub(r"<[^>]+>", "", line).strip()
        if clean and clean not in seen:
            seen.add(clean)
            lines.append(clean)
    return " ".join(lines)


def _resolve_yt_info(video_id: str) -> dict:
    """Extract video info (URL, subtitles) without downloading anything."""
    import yt_dlp
    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 20,
        "format": "bestaudio[protocol=m3u8_native]/bestaudio[protocol=m3u8]/bestaudio/best",
        **_ydl_cookie_opts(),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def get_yt_captions(video_id: str):
    """
    Obtiene subtítulos de YouTube via extract_info (no descarga archivos).
    Retorna dict con ok/text/source, o None si no hay subtítulos.
    """
    import httpx

    try:
        info = _resolve_yt_info(video_id)
    except Exception as _e:
        logger.debug("scraper → None: %s", _e)
        return None

    # Buscar en subtítulos manuales primero, luego automáticos
    for sub_dict in [info.get("subtitles", {}), info.get("automatic_captions", {})]:
        for lang in ["es", "es-419", "es-US", "es-MX"]:
            tracks = sub_dict.get(lang, [])
            # Preferir vtt, luego cualquier formato
            vtt_url = next((t["url"] for t in tracks if t.get("ext") == "vtt"), None)
            if not vtt_url and tracks:
                vtt_url = tracks[0].get("url")
            if not vtt_url:
                continue
            try:
                resp = httpx.get(vtt_url, timeout=20, follow_redirects=True)
                text = _parse_vtt(resp.text)
                if text.strip():
                    return {"ok": True, "text": text[:40000], "source": "subtitulos"}
            except Exception as _e:
                logger.debug("VTT fetch failed: %s", _e)
                continue

    return None


def transcribe_with_whisper(video_id: str, api_key: str, minutes: int = 10,
                            en_vivo: bool = False, cancelado=None):
    """
    Captura hasta `minutes` minutos de audio via ffmpeg+HLS y transcribe con Groq Whisper.
    Usa el mismo enfoque que el live transcriber: resolve URL con yt-dlp, captura con ffmpeg.

    En un stream en vivo ffmpeg entra por el borde en vivo: lo capturado son
    los PRÓXIMOS `minutes` minutos, no los últimos (antes el aviso decía
    "últimos 5 min"). `cancelado` (threading.Event) corta la captura si el
    usuario se fue: corre en un hilo y si no, ffmpeg seguía hasta 10 minutos
    para nadie.
    """
    import subprocess
    import tempfile

    from groq import Groq

    # Import diferido: live_transcriber importa de este módulo (ver
    # get_stream_url), así que a nivel de módulo sería circular.
    from live_transcriber import ffmpeg_exe, resolver_audio

    seconds = minutes * 60

    # Resolver URL de audio sin descargar
    try:
        fuente = resolver_audio(video_id)
    except Exception as e:
        err = str(e)
        return {"ok": False, "error": f"No se pudo obtener el video de YouTube: {err[:200]}"}
    stream_url = fuente["url"]

    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "audio.wav")
        entrada = stream_url
        if fuente["fragmentos"]:
            # VOD que fue transmisión en vivo: solo DASH por segmentos. La URL
            # del formato da 5 s de audio (ver resolver_audio); se bajan los
            # segmentos que cubren `minutes` a un archivo y ffmpeg lee de ahí.
            n = max(1, math.ceil(seconds / (fuente["seg"] or 5)))
            entrada = os.path.join(tmp, "audio.m4a")
            try:
                with httpx.Client(headers=fuente["headers"], timeout=30, follow_redirects=True) as hc, \
                     open(entrada, "wb") as fh:
                    for u in fuente["fragmentos"][:n]:
                        if cancelado is not None and cancelado.is_set():
                            return {"ok": False, "error": "Cancelado."}
                        r = hc.get(u)
                        r.raise_for_status()
                        fh.write(r.content)
            except Exception as e:
                return {"ok": False, "error": f"No se pudo bajar el audio del video: {str(e)[:200]}"}
        cmd = [
            ffmpeg_exe(), "-y",
            "-i", entrada,
            "-vn",
            "-ar", "16000",
            "-ac", "1",
            "-t", str(seconds),
            out_path,
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except FileNotFoundError:
            return {"ok": False, "error": "ffmpeg no está instalado. Instálalo con: pip install imageio-ffmpeg"}
        limite = time.monotonic() + seconds + 60
        try:
            while proc.poll() is None:
                if cancelado is not None and cancelado.is_set():
                    proc.kill()
                    proc.wait(5)
                    return {"ok": False, "error": "Cancelado."}
                if time.monotonic() > limite:
                    proc.kill()
                    proc.wait(5)
                    return {"ok": False, "error": "Tiempo de espera agotado al capturar el audio."}
                time.sleep(0.5)
        finally:
            if proc.poll() is None:
                proc.kill()
        stderr_txt = (proc.stderr.read() if proc.stderr else b"").decode(errors="ignore")

        if not os.path.exists(out_path) or os.path.getsize(out_path) < 4096:
            stderr = stderr_txt[-300:]
            return {"ok": False, "error": f"No se pudo capturar el audio del video. {stderr}"}

        size_mb = os.path.getsize(out_path) / 1_000_000
        from config import WHISPER_TIMEOUT
        from services.observability import timed

        # Sin timeout el SDK espera 600s × 3 intentos: un Groq colgado dejaba
        # el resumen de la sesión trabado media hora.
        client = Groq(api_key=api_key, timeout=WHISPER_TIMEOUT, max_retries=1)
        with open(out_path, "rb") as f, timed("Whisper", video=video_id, mb=f"{size_mb:.1f}"):
            tr = client.audio.transcriptions.create(
                file=(os.path.basename(out_path), f.read()),
                model="whisper-large-v3-turbo",
                language="es",
                response_format="text",
            )
        text = tr if isinstance(tr, str) else tr.text
        return {
            "ok": True,
            "text": text[:40000],
            "source": "whisper",
            "nota": (f"Transcripción de {minutes} min desde que empezó la captura en vivo"
                     if en_vivo else f"Transcripción de los primeros {minutes} min")
                    + f" ({size_mb:.1f} MB de audio)",
        }


async def fetch_transcript_youtube(video_id: str, api_key: str = ""):
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_yt_captions, video_id)


# ── AES encryption for SPLEY expediente API ───────────────────

_SPLEY_KEY = "ProdALg5ZrAsxBMD"

def _spley_encrypt(value: str) -> str:
    import base64

    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    key = _SPLEY_KEY.encode("utf-8")
    cipher = AES.new(key, AES.MODE_ECB)
    encrypted = cipher.encrypt(pad(value.encode("utf-8"), AES.block_size))
    b64 = base64.b64encode(encrypted).decode("utf-8")
    return b64.replace("+", "-").replace("/", "_").replace("=", "")


# ── Expediente completo de un proyecto ────────────────────────

def _nombre_a_formato_natural(nombre_crudo: str) -> str:
    """
    SPLEY devuelve los firmantes de un expediente como 'Apellidos, Nombres'
    (ej. 'Paredes Piqué, Susel Ana María'), mientras que la búsqueda de
    proyectos (fetch_proyectos) devuelve el mismo tipo de dato ya en
    'Nombres Apellidos'. Sin normalizar, un mismo congresista aparece con dos
    formatos distintos según qué herramienta lo trajo — se unifica acá al
    formato natural para que el informe sea consistente.
    """
    nombre_crudo = (nombre_crudo or "").strip()
    if "," not in nombre_crudo:
        return nombre_crudo
    apellidos, _, nombres = nombre_crudo.partition(",")
    return f"{nombres.strip()} {apellidos.strip()}".strip()


def _normalizar_lista_autores(autores_crudo: str) -> str:
    """
    Igual que _nombre_a_formato_natural pero para el campo 'autores' de SPLEY,
    que trae varios firmantes juntos separados por '; ' (ej. 'Luque Ibarra,
    Ruth; Bazán Narro, Sigrid Tesoro'). Deja intacto cualquier valor que no
    sea una lista de personas (ej. 'Poder Ejecutivo').
    """
    autores_crudo = (autores_crudo or "").strip()
    if not autores_crudo or "," not in autores_crudo:
        return autores_crudo
    partes = [_nombre_a_formato_natural(p) for p in autores_crudo.split(";")]
    return "; ".join(p for p in partes if p)


async def fetch_expediente(numero: str):
    """
    Obtiene el expediente completo de un proyecto: comisiones asignadas,
    seguimiento cronológico de actos, predictamen/dictamen y grupo parlamentario.
    """
    num_clean = _num_proyecto(numero)

    # Resolve pleyNum from list if needed
    async with _client() as c:
        items = await _spley_proyectos(c, {"page": 0, "size": 300},
                                       min_items=300,
                                       periodos=_periodos_de_numero(numero))
        if not items:
            return {"error": f"No se pudo buscar el proyecto {numero}."}
        cam_pedida = _camara_de_numero(numero)
        exact = next(
            (p for p in items
             if _num_proyecto(p.get("proyectoLey") or p.get("pleyNum")) == num_clean
             and (cam_pedida is None or p.get("_camara") == cam_pedida)),
            None,
        )
        if not exact:
            return {"error": f"Proyecto {numero} no encontrado."}
        pley_num = str(exact["pleyNum"])
        # El expediente vive dentro de su periodo y su cámara: con valores fijos
        # devolvía 404 para todo lo del periodo 2026-2031.
        per_par = exact.get("_perPar") or PER_PAR_ID
        cod_parl = exact.get("_camara") or "C"

    enc_per = _spley_encrypt(str(per_par))
    enc_ple = _spley_encrypt(pley_num)
    import asyncio as _asyncio

    base_url = f"{SPLEY_API}/expediente/{enc_per}/{enc_ple}"

    async def _get(c, path):
        try:
            # codTipoParl acá sí va como query param (así lo llama el portal).
            r = await c.get(f"{base_url}{path}", params={"codTipoParl": cod_parl})
            if r.status_code == 200:
                return r.json().get("data", {})
        except Exception as _e:
            logger.warning("fetch_expediente: %s: %s", type(_e).__name__, _e)
        return {}

    async with _client() as c:
        results = await _asyncio.gather(
            _get(c, ""),
            _get(c, "/acumulados"),
            _get(c, "/secciones"),
            _get(c, "/opinion-ciudadana"),
            _get(c, "/documentacion"),
            return_exceptions=True,
        )

    data               = results[0] if isinstance(results[0], dict) else {}
    data_acumulados    = results[1] if isinstance(results[1], dict) else {}
    data_secciones     = results[2] if isinstance(results[2], dict) else {}
    data_opinion       = results[3] if isinstance(results[3], dict) else {}
    data_documentacion = results[4] if isinstance(results[4], dict) else {}

    if not data:
        return {"error": f"No se pudo obtener el expediente del proyecto {numero}."}

    general      = data.get("general", {})
    comisiones   = data.get("comisiones", [])
    seguimientos = data.get("seguimientos", [])
    firmantes    = data.get("firmantes", [])

    # tipoFirmanteId 1 = autor principal, 2+ = coautores
    _autores_principales = [_nombre_a_formato_natural(f["nombre"]) for f in firmantes if f.get("tipoFirmanteId") == 1]
    _coautores           = [_nombre_a_formato_natural(f["nombre"]) for f in firmantes if f.get("tipoFirmanteId") != 1]

    # OJO: el patrón anterior (expediente/archivo/{nombreArchivo}) nunca
    # funcionó — el servidor real siempre devolvía 400 "parámetro con formato
    # incorrecto". Se encontró el formato real inspeccionando el tráfico de
    # red del portal SPLEY real (wb2server.congreso.gob.pe/spley-portal):
    # el link que arma el frontend es
    # spley-portal-service/archivo/{base64(proyectoArchivoId)}/pdf — SIN
    # "expediente/" en el path, con el ID en base64 plano (no la encriptación
    # AES de _spley_encrypt, que es para otra cosa). Verificado con una
    # descarga real: 200 OK, PDF válido.
    def _archivo_url(a):
        archivo_id = a.get("proyectoArchivoId")
        if archivo_id is None:
            return ""
        b64_id = base64.b64encode(str(archivo_id).encode()).decode()
        return f"https://api.congreso.gob.pe/spley-portal-service/archivo/{b64_id}/pdf"

    def _fmt_archivo(a):
        return {
            "nombre":      a.get("nombreArchivo") or "",
            "descripcion": a.get("descripcion") or a.get("desArchivo") or a.get("nombreArchivo") or "",
            "url":         _archivo_url(a),
            "tipo":        a.get("tipoArchivo") or "pdf",
        }

    # Pestaña 1: Seguimiento (orden cronológico ascendente — el más antiguo arriba)
    seguimiento = []
    todos_archivos = []
    for s in reversed(seguimientos):
        archivos_acto = [_fmt_archivo(a) for a in s.get("archivos", []) if a.get("activo") or a.get("nombreArchivo")]
        todos_archivos.extend(archivos_acto)
        seguimiento.append({
            "fecha":     _fmt_date(s.get("fecha", "")),
            "estado":    (s.get("desEstado") or "").upper(),
            "comision":  s.get("desComisiones") or "",
            "detalle":   (s.get("detalle") or "").upper(),
            "adjuntos":  archivos_acto,
        })

    # Detect predictamen / dictamen
    dictamen = next(
        (a for s in seguimientos for a in s.get("archivos", [])
         if "dictamen" in (a.get("descripcion") or "").lower()
         or "dictamen" in (a.get("nombreArchivo") or "").lower()),
        None,
    )

    # Pestaña 2: Proyectos acumulados
    pley_acumulados = []
    for p in (data_acumulados if isinstance(data_acumulados, list) else data_acumulados.get("proyectos", [])):
        pley_acumulados.append({
            "numero":             p.get("proyectoLey") or p.get("pleyNum") or "",
            "titulo":             p.get("titulo") or "",
            "fecha_presentacion": _fmt_date(p.get("fecPresentacion") or ""),
            "autor":              _normalizar_lista_autores(p.get("autores")) or p.get("desProponente") or "",
            "proponente":         p.get("desProponente") or "",
            "estado":             p.get("desEstado") or "",
            "enlace":             _enlace_expediente({**p, "_perPar": per_par,
                                                      "_camara": cod_parl}),
        })

    # Pestaña 3: Documentación Anexa (oficios, opiniones de ministerios, informes)
    documentacion_anexa = []
    _raw_docs = data_documentacion
    if isinstance(_raw_docs, dict):
        _raw_docs = (_raw_docs.get("documentos") or _raw_docs.get("documentacion")
                     or _raw_docs.get("items") or [])
    for d in (_raw_docs if isinstance(_raw_docs, list) else []):
        documentacion_anexa.append({
            "fecha":       _fmt_date(d.get("fecha") or ""),
            "tipo":        d.get("tipo") or d.get("tipoDocumento") or "",
            "descripcion": d.get("descripcion") or d.get("remitente") or d.get("nombre") or "",
            "adjuntos":    [_fmt_archivo(a) for a in (d.get("archivos") or [])] if d.get("archivos") else [],
        })

    # Pestaña 4: Secciones (texto articulado del proyecto, fórmula legal, dictámenes, autógrafas)
    secciones = []
    for s in (data_secciones if isinstance(data_secciones, list) else data_secciones.get("secciones", [])):
        sec_adjuntos = [_fmt_archivo(a) for a in (s.get("archivos") or []) if a.get("nombreArchivo") or a.get("rutaArchivo")]
        secciones.append({
            "titulo":    s.get("titulo") or s.get("nombre") or "",
            "texto":     (s.get("texto") or s.get("contenido") or "")[:3000],
            "adjuntos":  sec_adjuntos,
        })

    # Opinión ciudadana. La ficha oficial muestra CUATRO contadores: a favor,
    # en contra, propuesta alternativa y total — faltaba el tercero, así que el
    # asistente nunca podía mencionarlo aunque la página sí lo muestra.
    opinion = {}
    if isinstance(data_opinion, dict):
        opinion = {
            "total_opiniones": data_opinion.get("total") or data_opinion.get("totalOpiniones") or 0,
            "a_favor":         data_opinion.get("aFavor") or data_opinion.get("favor") or 0,
            "en_contra":       data_opinion.get("enContra") or data_opinion.get("contra") or 0,
            "propuesta_alternativa": (data_opinion.get("propuestaAlternativa")
                                      or data_opinion.get("alternativa")
                                      or data_opinion.get("propAlternativa") or 0),
            "comentarios":     len(data_opinion.get("comentarios") or data_opinion.get("opiniones") or []),
        }

    # OJO: general.get("pleyNum") puede venir vacío en esta sub-respuesta del
    # expediente (visto en producción) — usamos el pley_num ya resuelto más
    # arriba (de la búsqueda inicial), que siempre está disponible acá, en vez
    # de depender de un campo que a veces no viene.
    pley_num_str = pley_num or str(general.get("pleyNum", ""))
    return {
        "numero":                general.get("proyectoLey", numero),
        "titulo":                general.get("titulo", ""),
        "sumilla":               general.get("sumilla") or general.get("titulo", ""),
        "estado":                general.get("desEstado", ""),
        "fecha_presentacion":    _fmt_date(general.get("fecPresentacion") or ""),
        # El fallback era el literal "2021-2026": cuando la API no manda
        # desPerPar (pasa seguido), la ficha afirmaba el periodo ANTERIOR. Un
        # 00006-2026-2031-S salía como "2021-2026", contradiciendo su propio
        # número y la ficha oficial. El periodo va dentro del número, así que
        # se saca de ahí antes de inventarlo.
        "periodo_parlamentario": (general.get("desPerPar")
                                  or _periodo_desde_numero(general.get("proyectoLey") or numero)
                                  or _periodo_desde_perpar(per_par)),
        "legislatura":           general.get("desLegis", ""),
        "proponente":            general.get("desProponente", ""),
        "autor_principal":       ", ".join(_autores_principales) if _autores_principales else (general.get("autores") or general.get("desProponente") or ""),
        "coautores":             ", ".join(_coautores) if _coautores else (general.get("coAutores") or ""),
        "adherentes":            general.get("adherentes") or "",
        "grupo_parlamentario":   general.get("desGpar", ""),
        "comisiones":            [
            {
                "nombre": c.get("nombre") or c.get("desComision") or "",
                "id":     c.get("comisionId") or c.get("id") or "",
                "fecha_derivacion": _fmt_date(c.get("fecha") or ""),
                # OJO: antes armaba un link a comision2011.nsf/ComisionesVirtual
                # — esa plantilla ya no existe en el servidor (404: "Couldn't
                # find design note"), verificado con un comisionId real. No hay
                # una URL de reemplazo por-comisión conocida, así que se deja
                # vacío en vez de mostrar un link muerto (el workflow del
                # expediente ya omite el link si "enlace" viene vacío).
                "enlace": "",
            }
            for c in comisiones
        ] or _comisiones_desde_seguimiento(seguimiento),
        "fases":                 [f["fase"] for f in data.get("fases", []) if f.get("tipo") in (1, 2)],
        # Pestaña 1
        "seguimiento":           seguimiento,
        # Pestaña 2
        "proyectos_acumulados":  pley_acumulados,
        # Pestaña 3
        "documentacion_anexa":   documentacion_anexa,
        # Pestaña 4
        "secciones":             secciones,
        # Pestaña 5
        "opinion_ciudadana":     opinion,
        "todos_los_adjuntos":    todos_archivos,
        "predictamen":           {
            "fecha":   _fmt_date(dictamen.get("fecha", "")) if dictamen else None,
            "nombre":  dictamen.get("nombreArchivo") if dictamen else None,
            "url":     _archivo_url(dictamen) if dictamen else None,
        } if dictamen else None,
        "enlace_expediente":     _enlace_expediente({"pleyNum": pley_num_str,
                                                     "_perPar": per_par,
                                                     "_camara": cod_parl}),
        "fuente":                f"SPLEY expediente — {SPLEY_API}",
    }


# ── Texto real del proyecto (fórmula legal) ─────────────────────

def _comisiones_desde_seguimiento(seguimiento: list) -> list:
    """
    Comisiones a las que pasó el proyecto, sacadas de su seguimiento.

    En los proyectos del periodo 2026-2031 la pestaña de comisiones de SPLEY
    suele venir vacía aunque el seguimiento diga "EN COMISIÓN — Desarrollo
    Agrario; Defensa del Consumidor..." (visto en 00011-2026-2031-CD), y
    Diana respondía que no estaba en ninguna comisión.
    """
    vistas, out = set(), []
    for paso in seguimiento or []:
        for nombre in (paso.get("comision") or "").split(";"):
            nombre = nombre.strip()
            if nombre and nombre not in vistas:
                vistas.add(nombre)
                out.append({"nombre": nombre, "id": "", "enlace": "",
                            "fecha_derivacion": paso.get("fecha") or "",
                            "fuente": "seguimiento del expediente"})
    return out


async def fetch_formula_legal(numero_proyecto: str):
    """
    Devuelve el texto real de un proyecto de ley (fórmula legal, exposición
    de motivos) para poder resumirlo o analizarlo con precisión.

    Se llama SOLA (sin depender de una llamada previa a fetch_expediente —
    el router de Fase 1 elige herramientas de una sola vez, sin ver
    resultados intermedios, así que esta tiene que resolver todo internamente):
    1. Si el expediente tiene la pestaña "secciones" con texto → la usa.
    2. Si viene vacía (pasa seguido en proyectos recién presentados, donde
       SPLEY todavía no publicó el texto estructurado) → descarga el PDF
       real del proyecto desde "todos_los_adjuntos" y le extrae el texto.
    """
    from services import pdf as pdf_service

    exp = await fetch_expediente(numero_proyecto)
    if isinstance(exp, dict) and exp.get("error"):
        return exp

    secciones = exp.get("secciones") or []
    texto_secciones = "\n\n".join(
        f"### {s.get('titulo', '')}\n{s.get('texto', '')}" for s in secciones if s.get("texto")
    )
    if texto_secciones.strip():
        return {
            "numero": exp.get("numero", numero_proyecto),
            "fuente": "secciones estructuradas del expediente (SPLEY)",
            "texto": texto_secciones,
        }

    # Sin secciones: buscamos el PDF del proyecto entre los adjuntos.
    adjuntos = exp.get("todos_los_adjuntos") or []
    pdf_adjunto = next((a for a in adjuntos if (a.get("tipo") or "").lower() == "pdf"), None)
    if not pdf_adjunto or not pdf_adjunto.get("url"):
        return {
            "sin_datos": True,
            "mensaje": (
                "SPLEY no tiene el texto estructurado de este proyecto y tampoco hay "
                "un PDF del proyecto entre sus adjuntos. No hay forma de leer la "
                "fórmula legal real todavía."
            ),
        }

    # Los servidores del Congreso rechazan pedidos sin sus headers/cookies
    # esperados (Referer, User-Agent) y con certs autofirmados — por eso se
    # usa el cliente del scraper (_client(), verify=False).
    try:
        async with _client() as c:
            resp = await _get_oficial(c, pdf_adjunto["url"])
    except Exception as e:
        return {"error": f"No se pudo descargar el PDF del proyecto: {e}"}

    if resp is None:
        return {"error": "El adjunto del proyecto apunta fuera del Congreso; no se descargó."}

    if not pdf_service.looks_like_pdf(resp):
        return {"error": f"El adjunto no es un PDF válido (código {resp.status_code})."}

    from services import ocr

    try:
        paginas = await asyncio.to_thread(pdf_service.extract_pages, resp.content)
    except Exception as _e:
        logger.warning("fórmula legal: el PDF no se pudo abrir: %s", _e)
        return {"error": "No se pudo abrir el PDF del proyecto."}

    partes = [f"[Página {i}]\n{t}" for i, t in enumerate(paginas, start=1) if t]
    escaneadas = [i for i, t in enumerate(paginas, start=1) if not t]
    resultado = {
        "numero": exp.get("numero", numero_proyecto),
        "fuente": f"texto extraído del PDF del proyecto ({pdf_adjunto['url']})",
    }
    # Páginas escaneadas: antes quedaban como "sin texto" y Diana no podía
    # decir nada de la fórmula legal (pasó con el 14864/2025-CR).
    if escaneadas and ocr.disponible():
        texto_ocr, sin_leer = await ocr.transcribir_paginas(resp.content, escaneadas)
        if texto_ocr:
            partes.append(texto_ocr)
            resultado["fuente"] += "; páginas escaneadas leídas con OCR"
            resultado["aviso_ocr"] = (
                "Parte del texto viene de OCR sobre páginas escaneadas: lo transcribió "
                "un modelo de IA y puede tener errores en números o nombres. Dilo en la "
                "respuesta y recomienda verificar en el PDF oficial lo que sea decisivo."
            )
        escaneadas = sin_leer
    if escaneadas:
        partes.append("[Páginas escaneadas que no se pudieron leer: "
                      + ", ".join(map(str, escaneadas)) + ". Su contenido no está disponible.]")

    texto = "\n\n".join(partes)
    if not texto.strip() or (not any(paginas) and "OCR" not in resultado["fuente"]):
        return {"error": "No se pudo extraer texto del PDF del proyecto: es una imagen "
                         "escaneada y el OCR no respondió. Revísalo en el PDF oficial: "
                         + pdf_adjunto["url"]}
    resultado["texto"] = texto[:pdf_service.MAX_TEXT_CHARS]
    return resultado


# ── Agenda de comisiones (próximos días) ──────────────────────

SINTESIS_URL = (
    "https://www2.congreso.gob.pe/Sicr/ApoyComisiones/comision2011.nsf/"
    "new_04pa_sintagenNS?OpenForm&Start=1&Count=1000&ExpandView"
)
CONGRESO2 = "https://www2.congreso.gob.pe"


async def fetch_agenda_comisiones(dias: int = 2, comision: str = None):
    """
    Obtiene la agenda de sesiones de comisiones para los próximos días desde
    las Síntesis de Agendas del Departamento de Comisiones (sistema Lotus Notes,
    la misma fuente que muestra el iframe de congreso.gob.pe/agendas-del-dia).
    Cada síntesis detalla comisión, sesión, hora, lugar y plataforma.
    `comision` filtra opcionalmente por nombre (o parte del nombre).
    """
    from datetime import timedelta

    today = ahora_lima().date()
    limite = today + timedelta(days=max(dias, 1))

    async with _client() as c:
        r = await c.get(SINTESIS_URL)
        if r.status_code != 200:
            return {"error": "No se pudo obtener la lista de síntesis de agendas."}
        soup = BeautifulSoup(r.text, "html.parser")

        # Entradas con formato "dd/mm/yyyy, Síntesis de Agendas"
        entradas = []
        for a in soup.select("a[href]"):
            text = a.get_text(strip=True)
            m = re.match(r"(\d{2}/\d{2}/\d{4})", text)
            if m and "OpenDocument" in a.get("href", ""):
                try:
                    fecha = datetime.strptime(m.group(1), "%d/%m/%Y").date()
                except ValueError:
                    continue
                href = a["href"]
                enlace = href if href.startswith("http") else CONGRESO2 + href
                entradas.append({"fecha": fecha, "enlace": enlace})

        # Solo síntesis dentro del rango [hoy, hoy+dias] — sin fallback a datos viejos
        en_rango = [e for e in entradas if today <= e["fecha"] <= limite]
        vigentes = en_rango
        nota = None

        sintesis = []
        for e in vigentes[:3]:
            try:
                rd = await _get_oficial(c, e["enlace"])
                if rd is None or rd.status_code != 200:
                    continue
                dsoup = BeautifulSoup(rd.text, "html.parser")
                texto = dsoup.get_text(separator="\n", strip=True)
                lineas = [l for l in texto.split("\n") if len(l.strip()) > 2]
                contenido = "\n".join(lineas)[:4000]
                if comision and comision.lower() not in contenido.lower():
                    continue
                sintesis.append({
                    "fecha_sintesis": e["fecha"].strftime("%d/%m/%Y"),
                    "enlace": e["enlace"],
                    "contenido": contenido,
                })
            except Exception as _e:
                logger.debug("sintesis item failed: %s", _e)
                continue

    # Desde la bicameralidad las Síntesis de Agendas dejaron de publicarse a
    # diario y las sesiones de comisiones salen en la agenda de
    # comunicaciones. Solo con las síntesis Diana decía "no hay sesiones" un
    # día con seis (verificado el 13/10/2026).
    agenda = await fetch_agenda_camaras(dias=max(dias, 1), camara="comision")
    sesiones = agenda.get("sesiones") or []
    if comision:
        buscado = _sin_acentos(comision)
        sesiones = [x for x in sesiones
                    if buscado in _sin_acentos(x["tema"] + " " + x["organiza"])]

    if not sintesis and not sesiones:
        if agenda.get("error"):
            return agenda
        return {"sin_datos": True,
                "mensaje": "No se encontraron sesiones de comisiones programadas"
                           + (f" para '{comision}'" if comision else "")
                           + " en los próximos días."}

    resultado = {
        "fuente": ("Agenda del Congreso (comunicaciones.congreso.gob.pe/agenda), Visor de "
                   "Sesiones de Comisiones de Diputados y Senado y Síntesis de Agendas "
                   "del Departamento de Comisiones"),
        "dias_consultados": dias,
        "sesiones": sesiones,
        "total_sesiones": len(sesiones),
        **({"aviso_fuentes": agenda["aviso_fuentes"]} if agenda.get("aviso_fuentes") else {}),
        "instruccion": ("Lista TODAS las sesiones de \"sesiones\" (cada una trae su "
                        "\"enlace\" a la agenda del día) y, si hay \"sintesis\", las "
                        "que estas no repitan. Extrae SOLO las de los días consultados."),
    }
    if sintesis:
        resultado["sintesis"] = sintesis
    if nota:
        resultado["nota"] = nota
    return resultado


# ── Agenda del Pleno ──────────────────────────────────────────

AGENDA_PLENO_URL = (
    "https://www2.congreso.gob.pe/Sicr/RelatAgenda/PlenoComiPerm20112016.nsf/"
    "new_agendapleno?OpenForm&Start=1&Count=1000&ExpandView"
)


# Desde la bicameralidad cada cámara publica su Agenda del Pleno en su propio
# portal; la Relatoría de arriba quedó congelada en junio de 2026 (la última
# del Congreso unicameral) y Diana presentaba esa como "la agenda del Pleno".
AGENDAS_PLENO_CAMARAS = {
    "Cámara de Diputados": f"{DIPUTADOS}/sesiones-del-pleno/",
    "Senado": f"{SENADO}/sesiones_del_pleno/",
}


async def _agendas_de_camara(c, url: str) -> list[tuple[date, str]]:
    """(fecha, enlace) de las agendas del Pleno publicadas, la más nueva primero."""
    r = await c.get(url, headers=HEADERS)
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    out, vistos = [], set()
    for a in soup.select("a[href]"):
        href = a["href"]
        if not href.lower().endswith(".pdf") or "AGENDA" not in href.upper() or href in vistos:
            continue
        fila = a.find_parent(["tr", "li"])
        m = re.search(r"(\d{2})/(\d{2})/(\d{4})", fila.get_text(" ", strip=True) if fila else "")
        if not m:
            continue
        try:
            fecha = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            continue
        vistos.add(href)
        out.append((fecha, href))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def _leer_agenda_pdf(data: bytes):
    import fitz
    doc = fitz.open(stream=data, filetype="pdf")
    return ("\n".join(page.get_text() for page in doc).strip(),
            _parse_indice_agenda_pleno(doc))


def _contar_items_agenda(texto: str, estructura: list) -> None:
    """
    Agrega a cada sección de `estructura` cuántos asuntos numerados (1., 2.,
    ...) tiene en el cuerpo. Solo cuenta numeraciones correlativas que
    arrancan en 1 después del título de la sección, así que el pie de página
    o un "1." suelto no suman.

    La Relatoría solo numera cuando hay dos asuntos o más: una sección sin
    subsecciones que aparece en el cuerpo con texto pero sin numeración tiene
    un asunto (agenda de Diputados del 06/10/2026: "Dictamen" con el PL 98,
    "Moción de interpelación" con la 424). Antes quedaban sin el campo y la
    respuesta a "¿cuántos dictámenes?" era "—".
    """
    nombres = {}
    for e in estructura:
        nombres[_sin_acentos(e["seccion"]).strip()] = (e, None)
        for sub in e.get("subsecciones", []):
            nombres[_sin_acentos(sub["seccion"]).strip()] = (sub, e)
    con_texto = set()   # id() de las secciones con cuerpo en el documento
    actual = padre = None
    ultimo = 0
    for linea in texto.split("\n"):
        limpia = _sin_acentos(linea).strip()
        if limpia in nombres:
            # La numeración reinicia en cada sección y subsección.
            (actual, padre), ultimo = nombres[limpia], 0
            continue
        if actual is not None and len(limpia) > 20:
            con_texto.add(id(actual))
        m = re.match(r"^(\d{1,3})\.(\s|$)", limpia)
        if actual is not None and m and int(m.group(1)) == ultimo + 1:
            ultimo += 1
            actual["asuntos"] = ultimo

    for e in estructura:
        subs = e.get("subsecciones", [])
        for sec in subs or [e]:
            if "asuntos" not in sec and id(sec) in con_texto:
                sec["asuntos"] = 1
        if subs and any("asuntos" in sub for sub in subs):
            e["asuntos"] = sum(sub.get("asuntos", 0) for sub in subs)


def _armar_agenda(fuente: str, titulo: str, enlace: str, texto: str,
                  estructura: list, fecha_doc) -> dict:
    resultado = {"fuente": fuente, "titulo": titulo, "enlace": enlace}
    if fecha_doc:
        resultado["fecha"] = fecha_doc.strftime("%d/%m/%Y")
        # Puede llevar semanas sin actualizarse: sin este aviso, el modelo
        # presenta una agenda de hace semanas como si fuera la de "hoy".
        dias = (ahora_lima().date() - fecha_doc).days
        if dias > 14:
            resultado["advertencia_desactualizado"] = (
                f"Esta es la Agenda del Pleno MÁS RECIENTE publicada, pero es del "
                f"{fecha_doc.strftime('%d/%m/%Y')} — hace {dias} días. NO la presentes "
                f"como la agenda 'actual' o 'de esta semana' sin aclarar la fecha real "
                f"y que no hay una más nueva publicada."
            )
    if estructura:
        _contar_items_agenda(texto, estructura)
        resultado["estructura"] = estructura
        resultado["nota"] = (
            "\"estructura\" viene del ÍNDICE real del documento (sección → rango de "
            "páginas). \"asuntos\" es la cantidad de asuntos numerados (1., 2., ...) "
            "de esa sección en el cuerpo del documento; si una sección no lo trae, no "
            "tiene numeración y no hay un conteo confiable. NO cuentes menciones de "
            "palabras en \"texto\": una palabra como \"dictamen\" aparece varias "
            "veces por asunto y contarla da un número inflado y falso."
        )
    else:
        resultado["nota"] = (
            "No se pudo extraer el índice estructurado de este documento. NO "
            "inventes ni calcules conteos de dictámenes/mociones/etc a partir del "
            "texto — decile al usuario que no hay un conteo confiable disponible "
            "para esta agenda y dale el link para revisarla directamente."
        )
    # Al final: si el orquestador recorta por largo, se pierde texto y no la
    # estructura (pasó: el conteo de asuntos quedaba fuera de los 7000 chars).
    resultado["texto"] = texto[:8000]
    return resultado


async def _agenda_pleno_relatoria(c):
    """Última agenda de la Relatoría del Congreso (la del iframe de agenda-del-pleno)."""
    r = await c.get(AGENDA_PLENO_URL)
    if r.status_code != 200:
        return {"error": "No se pudo obtener la lista de agendas del Pleno."}
    soup = BeautifulSoup(r.text, "html.parser")
    docs = [(a.get_text(strip=True), a["href"]) for a in soup.select("a[href]")
            if "/Apleno/" in a.get("href", "") and a.get_text(strip=True)]
    if not docs:
        return {"error": "No se encontraron agendas del Pleno publicadas."}

    titulo, enlace = docs[0]  # la más reciente
    rd = await _get_oficial(c, enlace)
    if rd is None:
        return {"error": "La agenda del Pleno apunta fuera del Congreso; no se descargó."}
    if rd.status_code != 200:
        return {"error": "No se pudo descargar la agenda del Pleno."}
    try:
        # fitz es síncrono: en un hilo para no frenar el event loop.
        texto, estructura = await asyncio.to_thread(_leer_agenda_pdf, rd.content)
    except Exception as _e:
        logger.warning("agenda del Pleno: el PDF no se pudo leer, uso el texto HTML: %s", _e)
        texto = BeautifulSoup(rd.text, "html.parser").get_text(separator="\n", strip=True)
        estructura = []
    resultado = _armar_agenda("Relatoría del Congreso — Agenda del Pleno", titulo, enlace,
                              texto, estructura, _fecha_desde_titulo_es(titulo))
    resultado = {"camara": "Congreso", **resultado}
    resultado["agendas_anteriores"] = [{"titulo": t, "enlace": h} for t, h in docs[1:4]]
    return resultado


async def _agenda_pleno_camara(c, camara: str, url: str):
    try:
        agendas = await _agendas_de_camara(c, url)
        if not agendas:
            return None
        fecha, enlace = agendas[0]
        rd = await _get_oficial(c, enlace)
        if rd is None or rd.status_code != 200:
            return None
        texto, estructura = await asyncio.to_thread(_leer_agenda_pdf, rd.content)
    except Exception as _e:
        logger.warning("agenda del Pleno de %s: %s: %s", camara, type(_e).__name__, _e)
        return None
    resultado = _armar_agenda(f"{camara} — Sesiones del Pleno ({url})",
                              f"Agenda del Pleno de {camara} — sesión del {fecha.strftime('%d/%m/%Y')}",
                              enlace, texto, estructura, fecha)
    resultado = {"camara": camara, **resultado}
    resultado["agendas_anteriores"] = [
        {"titulo": f"Agenda del Pleno de {camara} — {f.strftime('%d/%m/%Y')}", "enlace": h}
        for f, h in agendas[1:4]
    ]
    return resultado


async def fetch_agenda_pleno(camara: str = None):
    """
    Agenda del Pleno más reciente: la de la Cámara de Diputados, la del
    Senado y la de la Relatoría del Congreso. Devuelve la más nueva arriba
    (mismo formato de siempre) y las de las otras cámaras en
    "otras_agendas". Los PDFs se leen con PyMuPDF y el índice da la
    estructura por sección.
    """
    async with _client() as c:
        resultados = await asyncio.gather(
            _agenda_pleno_relatoria(c),
            *[_agenda_pleno_camara(c, cam, url) for cam, url in AGENDAS_PLENO_CAMARAS.items()],
            return_exceptions=True,
        )
    agendas = [r for r in resultados if isinstance(r, dict) and r.get("titulo")]
    if not agendas:
        primero = resultados[0]
        return primero if isinstance(primero, dict) else {
            "error": "No se pudo obtener la Agenda del Pleno."}

    def _fecha(a):
        try:
            return datetime.strptime(a.get("fecha", ""), "%d/%m/%Y").date()
        except ValueError:
            return date.min

    agendas.sort(key=_fecha, reverse=True)
    # "La última agenda del Pleno del Senado": esa va arriba aunque la de
    # Diputados sea más nueva.
    pedida = {"D": "Cámara de Diputados", "S": "Senado", "C": "Congreso"}.get(
        _codigo_camara(camara) or "")
    if pedida:
        agendas.sort(key=lambda a: a.get("camara") != pedida)
    principal, otras = agendas[0], agendas[1:]
    texto = principal.pop("texto", "")
    if otras:
        # Solo lo necesario para mencionarlas: el texto completo de tres
        # agendas no entra en el contexto del modelo.
        principal["otras_agendas"] = [
            {k: a[k] for k in ("camara", "titulo", "fecha", "enlace", "estructura",
                               "advertencia_desactualizado") if k in a}
            for a in otras
        ]
    principal["texto"] = texto  # siempre último, ver _armar_agenda
    return principal


# Nombres de sección tal como aparecen en el cuerpo del documento (en
# MAYÚSCULAS, sin tilde) — se usan para ubicar el inicio real de cada
# sección, distinto de su mención en el índice.
_SECCIONES_AGENDA_PLENO = [
    "DICTAMENES", "REFORMAS CONSTITUCIONALES", "INSISTENCIAS", "ALLANAMIENTO",
    "AUTOGRAFAS OBSERVADAS", "PROYECTOS DE LEY", "PENDIENTES DE SEGUNDA VOTACION",
    "RECONSIDERACIONES", "MOCIONES DE ORDEN DEL DIA", "INFORMES FINALES",
]


def _parse_indice_agenda_pleno(doc) -> list:
    """
    Extrae la tabla real del ÍNDICE (página 2 típicamente): nombre de sección
    y página donde empieza. A partir de eso calcula cuántas páginas ocupa
    cada sección — un dato 100% verificable contra el propio documento, a
    diferencia de contar menciones de palabras en el texto (que sobreestima
    mucho: "dictamen" aparece varias veces por cada asunto, no una vez).

    Devuelve una lista de dicts: [{"seccion", "pagina_inicio", "paginas"}].
    Vacía si no logra parsear un índice reconocible (el llamador debe avisar
    que no hay conteo confiable en vez de inventar uno).
    """
    import re
    import unicodedata

    def _sin_tildes(s: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")

    # Busca la página del índice: la que contiene la palabra "ÍNDICE".
    indice_pagina = None
    for i in range(min(5, len(doc))):
        if "INDICE" in _sin_tildes(doc[i].get_text()).upper():
            indice_pagina = i
            break
    if indice_pagina is None:
        return []

    lineas = [l.strip() for l in doc[indice_pagina].get_text().split("\n") if l.strip()]

    # Reconstruye entradas "nombre → página": cada entrada del índice termina
    # en una línea que es puramente un número de página; el nombre son las
    # líneas previas que no son numeral romano ni encabezado de la tabla.
    entradas = []
    for i, linea in enumerate(lineas):
        if not re.match(r"^\d{1,4}$", linea):
            continue
        j = i - 1
        partes = []
        while j >= 0 and not re.match(r"^\d{1,4}$", lineas[j]):
            partes.insert(0, lineas[j])
            j -= 1
        nombre = " ".join(partes).strip()
        # Las secciones llevan numeral romano ("I. Dictamen"); las líneas sin
        # numeral que siguen son subsecciones ("Moción de interpelación"). Las
        # agendas de Diputados y del Senado (2026) usan secciones que no
        # existían en las del Congreso unicameral, así que el numeral manda y
        # la lista fija solo cubre índices viejos sin numeral.
        es_seccion = bool(re.match(r"^[IVX]+\.\s*", nombre))
        nombre = re.sub(r"^[IVX]+\.\s*", "", nombre)  # saca el numeral romano pegado
        nombre_norm = _sin_tildes(nombre).upper()
        if es_seccion or nombre_norm in _SECCIONES_AGENDA_PLENO:
            entradas.append((nombre, int(linea), []))
        elif entradas and nombre:
            entradas[-1][2].append({"seccion": nombre, "pagina_inicio": int(linea)})

    if not entradas:
        return []

    # Página final = inicio de la siguiente sección menos 1 (o fin del doc).
    estructura = []
    for idx, (nombre, inicio, subsecciones) in enumerate(entradas):
        fin = entradas[idx + 1][1] - 1 if idx + 1 < len(entradas) else len(doc)
        seccion = {
            "seccion": nombre,
            "pagina_inicio": inicio,
            "paginas": max(1, fin - inicio + 1),
        }
        if subsecciones:
            seccion["subsecciones"] = subsecciones
        estructura.append(seccion)
    return estructura


# ── Mesa Directiva ────────────────────────────────────────────

MESA_DIRECTIVA_URLS = {
    "Senado": f"{SENADO}/mesa-directiva/",
    "Cámara de Diputados": f"{DIPUTADOS}/mesa-directiva/",
}
_CARGOS_MESA = ("Presidente", "Presidenta", "Primer Vicepresidente", "Primera Vicepresidenta",
                "Segundo Vicepresidente", "Segunda Vicepresidenta",
                "Tercer Vicepresidente", "Tercera Vicepresidenta")


def _mesa_desde_html(html: str) -> list[dict]:
    """[{cargo, nombre}] de una página de Mesa Directiva: el nombre va justo antes del cargo."""
    lineas = BeautifulSoup(html, "html.parser").get_text("\n", strip=True).split("\n")
    return [{"cargo": l, "nombre": lineas[i - 1]}
            for i, l in enumerate(lineas) if l in _CARGOS_MESA and i > 0]


def _preside_desde_agenda(texto: str) -> str:
    m = re.search(r"PRESIDENCIA DE(?:L SEÑOR|\s+LA SEÑORA)\s+([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ .]+)", texto or "")
    return " ".join(m.group(1).split()).title() if m else ""


async def fetch_mesa_directiva(camara: str = None):
    """
    Presidente y vicepresidentes de cada cámara.

    Senado: su página oficial de Mesa Directiva (nombre y cargo en texto).
    Diputados: su página solo publica una foto sin nombres (verificado: el
    OCR no saca nada), así que el presidente sale de la portada de la última
    Agenda del Pleno ("PRESIDENCIA DEL SEÑOR ..."), y se dice de dónde sale.
    """
    pedida = _codigo_camara(camara)
    out = {}
    async with _client() as c:
        for nombre_camara, url in MESA_DIRECTIVA_URLS.items():
            if pedida and nombre_camara[0] != {"D": "C", "S": "S"}.get(pedida, nombre_camara[0]):
                continue
            try:
                r = await c.get(url, headers=HEADERS)
                miembros = _mesa_desde_html(r.text) if r.status_code == 200 else []
            except Exception as _e:
                logger.warning("mesa directiva de %s: %s", nombre_camara, _e)
                miembros = []
            out[nombre_camara] = {"miembros": miembros, "fuente": url}
    if "Cámara de Diputados" in out and not out["Cámara de Diputados"]["miembros"]:
        try:
            agenda = await fetch_agenda_pleno(camara="diputados")
            if agenda.get("camara") == "Cámara de Diputados":
                preside = _preside_desde_agenda(agenda.get("texto", ""))
                if preside:
                    out["Cámara de Diputados"] = {
                        "miembros": [{"cargo": "Presidencia de la sesión del Pleno", "nombre": preside}],
                        "fuente": agenda.get("enlace"),
                        "nota": ("La página de Mesa Directiva de Diputados solo publica una foto sin "
                                 "nombres. El dato sale de la portada de la Agenda del Pleno del "
                                 f"{agenda.get('fecha')}, que indica quién preside la sesión; dilo así."),
                    }
        except Exception as _e:
            logger.warning("mesa directiva de Diputados desde la agenda: %s", _e)
    if not any(v["miembros"] for v in out.values()):
        return {"sin_datos": True, "mensaje": "No se pudo leer la Mesa Directiva en los portales oficiales."}
    return {"mesas_directivas": out}


# ── Mociones de interpelación ─────────────────────────────────

# ── Mociones de Orden del Día ──────────────────────────────────
#
# Sistema oficial de mociones, enlazado desde el menú de senado. y
# diputados.congreso.gob.pe ("Sistema de Mociones de Orden del Día"). Vive en
# el mismo api.congreso.gob.pe que SPLEY y tampoco pide autenticación.
#
# Las interpelaciones son mociones, no proposiciones legislativas: por eso
# antes había que rastrearlas con un LIKE '%INTERPELAC%' sobre los títulos de
# SPLEY —donde no viven— y completar el hueco con notas de prensa. Acá vienen
# tipificadas, con su estado real de trámite.
SMOCIONES_API = "https://api.congreso.gob.pe/smociones-portal-service"

# tipoMocionId del catálogo oficial (general/info-filtros-lista-mociones).
TIPO_MOCION = {
    "interpelacion": 19,   # Pedidos de Interpelación al Consejo de Ministros
    "censura":       27,   # Censura al Consejo de Ministros o a Ministros
    "vacancia":      30,   # Pedido de Vacancia Presidencial
    "investigacion": 17,   # Conformación de Comisiones de Investigación
}

SMOCIONES_PORTAL = "https://wb2server.congreso.gob.pe/smociones-portal/#/expediente"


def _autores_mocion(crudo: str) -> str:
    """
    El sistema de Mociones trae "(P)Vidaurre Floridas, César Manuel (Fuerza
    Popular); ...": con el normalizador de SPLEY salía "César Manuel (Fuerza
    Popular) (P)Vidaurre Floridas". (P) marca al proponente principal.
    """
    salida = []
    for parte in (crudo or "").split(";"):
        parte = re.sub(r"^\s*\(P\)\s*", "", parte).strip()
        m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", parte)
        nombre, bancada = (m.group(1), m.group(2)) if m else (parte, "")
        nombre = _nombre_a_formato_natural(nombre) if "," in nombre else nombre
        if nombre:
            salida.append(f"{nombre} ({bancada})" if bancada else nombre)
    return "; ".join(salida)


async def fetch_mociones(tipo: str = None, camara: str = None,
                         texto: str = None, limit: int = 20):
    """
    Mociones de Orden del Día de ambas cámaras.

    `tipo` es una clave de TIPO_MOCION ("interpelacion", "censura",
    "vacancia"...). Sin tipo trae todas.

    OJO con `size`: la API lo acepta pero lo ignora y devuelve el periodo
    completo (153 mociones en Diputados 2026, medido) — igual que SPLEY con su
    propio `size`. Por eso el recorte a `limit` se hace acá.
    """
    codigos = {"diputados": ["D"], "senado": ["S"]}.get(
        (camara or "").lower(), ["D", "S"])
    tipo_id = TIPO_MOCION.get((tipo or "").lower())

    async def _una_camara(c, cod):
        try:
            per = await c.get(f"{SMOCIONES_API}/general/periodo-parlamentario/{cod}")
            periodos = per.json().get("data") or []
            if not periodos:
                return []
            per_id = periodos[0].get("perParId")
            r = await c.post(f"{SMOCIONES_API}/mocion/lista-con-filtros", json={
                "codTipoParl": cod, "perParId": per_id, "perLegId": None,
                "mocionNum": None, "tipoMocionId": tipo_id, "finalidadId": None,
                "estadoMocionId": None, "congresistaId": None, "grupoParlId": None,
            })
            if r.status_code != 200:
                return []
            return [dict(m, _camara=cod)
                    for m in (r.json().get("data") or {}).get("mociones") or []]
        except Exception as _e:
            logger.debug("smociones cámara %s: %s", cod, _e)
            return []

    async with _client() as c:
        tandas = await asyncio.gather(*(_una_camara(c, cod) for cod in codigos))

    filtro = (texto or "").upper()
    out = []
    for tanda in tandas:
        for m in tanda:
            sumilla = m.get("sumilla") or ""
            if filtro and filtro not in sumilla.upper() and filtro not in str(m.get("autores") or "").upper():
                continue
            out.append({
                "numero":     m.get("mocion") or m.get("mocionNum") or "",
                "tipo":       m.get("desTipoMocion") or "",
                "estado":     m.get("desEstadoMocion") or "",
                "fecha":      _fmt_date(m.get("fecPresentacion") or ""),
                "sumilla":    sumilla,
                "grupo_parlamentario": m.get("desGpar") or "",
                "autores":    _autores_mocion(m.get("autores")) or (m.get("autores") or ""),
                "camara":     CAMARAS.get(m.get("_camara") or "D", ""),
                "enlace":     f"{SMOCIONES_PORTAL}/{m.get('_camara')}/search",
            })

    out.sort(key=lambda m: m["fecha"][-4:] + m["fecha"][3:5] + m["fecha"][:2],
             reverse=True)
    return out[:limit], len(out)


def _tipo_coincide(tipo_pedido: str, tipo_mocion: str) -> bool:
    """"comisión especial" casa con "Conformación de Comisiones Especiales":
    cada palabra del pedido (sus primeras 6 letras) tiene que estar."""
    palabras = [p[:6] for p in re.findall(r"\w{4,}", _sin_acentos(tipo_pedido))]
    tipo = _sin_acentos(tipo_mocion)
    return bool(palabras) and all(p in tipo for p in palabras)


async def buscar_mociones(tipo: str = None, camara: str = None, texto: str = None,
                          dias: int = None, limit: int = 20):
    """
    Mociones de Orden del Día de cualquier tipo, con filtros.

    Las de saludo son ~86% del registro (726 de 839 al 19/10/2026): si no se
    piden, se dejan afuera y solo se cuentan, porque taparían a las demás.
    """
    todas, _ = await fetch_mociones(camara=camara, texto=texto, limit=100_000)
    if dias:
        desde = (ahora_lima().date() - timedelta(days=int(dias))).strftime("%Y%m%d")
        todas = [m for m in todas if m["fecha"][-4:] + m["fecha"][3:5] + m["fecha"][:2] >= desde]
    if tipo:
        elegidas = [m for m in todas if _tipo_coincide(tipo, m["tipo"])]
        saludos = 0
    else:
        elegidas = [m for m in todas if "SALUDO" not in _sin_acentos(m["tipo"])]
        saludos = len(todas) - len(elegidas)

    if not elegidas:
        partes = [f"de tipo «{tipo}»" if tipo else "", f"que mencionen «{texto}»" if texto else "",
                  f"en los últimos {dias} días" if dias else ""]
        return {"sin_datos": True,
                "mensaje": "El sistema de Mociones no tiene mociones "
                           + " ".join(p for p in partes if p) + " en el periodo actual.",
                "mociones_de_saludo": saludos}

    for m in elegidas:
        autores = [a.strip() for a in (m.get("autores") or "").split(";") if a.strip()]
        if len(autores) > 3:
            m["autores"] = "; ".join(autores[:3]) + f"; y {len(autores) - 3} más"
    resultado = {
        "fuente": "Sistema de Mociones de Orden del Día — api.congreso.gob.pe",
        "mociones": elegidas[:limit],
        "total": len(elegidas),
    }
    if saludos:
        resultado["mociones_de_saludo"] = saludos
        resultado["nota"] = (f"Además hay {saludos} mociones de saludo en el mismo rango, que no se "
                             "listan: menciónalo en una línea.")
    return resultado


async def fetch_interpelaciones(ministro: str = None):
    """
    Mociones de interpelación a ministros, del sistema oficial de mociones.

    Antes esto salía de un LIKE sobre los títulos de SPLEY (donde las mociones
    no viven) más notas de prensa de medios. Ahora sale del registro oficial,
    filtrado por el tipo 19 del catálogo del propio Congreso, con el estado
    real de cada una — admitida, en Junta de Portavoces, con ministro citado.
    """
    # La censura es el paso siguiente a una interpelación: el 15/10/2026 la
    # del ministro del Interior (Moción 445) estaba en la agenda del Pleno y
    # Diana no la mencionaba porque solo pedía el tipo "interpelación".
    (mociones, total), (censuras, _) = await asyncio.gather(
        fetch_mociones(tipo="interpelacion", texto=ministro, limit=30),
        fetch_mociones(tipo="censura", texto=ministro, limit=30))
    if not mociones and not censuras:
        detalle = f" que mencionen a «{ministro}»" if ministro else ""
        return {
            "sin_datos": True,
            "mensaje": (f"El Congreso no tiene mociones de interpelación ni de censura "
                        f"registradas{detalle} en el periodo actual."),
        }

    # Cada moción trae todos sus firmantes (hasta 30 nombres con su bancada):
    # con 4 mociones el resultado pasaba el tope del orquestador, se cortaba
    # la última y el modelo decía que "no aparece en el sistema".
    for m in mociones + censuras:
        autores = [a.strip() for a in (m.get("autores") or "").split(";") if a.strip()]
        if len(autores) > 3:
            m["autores"] = "; ".join(autores[:3]) + f"; y {len(autores) - 3} más"
            m["cantidad_firmantes"] = len(autores)

    resultado = {
        "fuente": "Sistema de Mociones de Orden del Día — api.congreso.gob.pe",
        "mociones_interpelacion": mociones,
        "total": total,
    }
    if censuras:
        resultado["mociones_censura"] = censuras
    return resultado


# ── Agenda bicameral (Senado + Diputados + Pleno) ─────────────

AGENDA_COMUNICACIONES = "https://comunicaciones.congreso.gob.pe/agenda"

# Visor de Sesiones de Comisiones (wb2server.congreso.gob.pe/visor-sesiones/#/d
# y #/s). Las comisiones registran ahí sus convocatorias apenas las emiten, y
# muchas nunca llegan a la agenda de comunicaciones: el 27/10/2026 la agenda
# traía 2 sesiones de comisión y el visor 7+ solo en Diputados. Es la misma
# consulta pública que hace la página del visor, con el header Basic fijo que
# viene en su código público (autorizado por el usuario el 26/10/2026).
VISOR_SESIONES_API = "https://wb2server.congreso.gob.pe/service-portal-publico-ext/sesiones/busqueda"
VISOR_SESIONES_WEB = "https://wb2server.congreso.gob.pe/visor-sesiones/#/"
_VISOR_AUTH = {"Authorization": "Basic " + base64.b64encode(b"userNamePortal:passwordPortal").decode()}
_VISOR_CAMARAS = {"D": "Cámara de Diputados", "S": "Senado"}
_PALABRAS_VACIAS = {"COMISION", "DE", "DEL", "LA", "LAS", "LOS", "Y", "EN", "E",
                    "ASUNTOS", "PARA", "EL", "SOBRE"}


def _hora_visor(hora: str) -> str:
    """'3:00PM' → '3:00 PM', el formato de la agenda de comunicaciones."""
    m = re.match(r"^\s*(\d{1,2}:\d{2})\s*([AP]M)\s*$", (hora or "").upper())
    return f"{m.group(1)} {m.group(2)}" if m else (hora or "").strip()


def _sesion_visor(item: dict, codigo: str) -> dict | None:
    fecha = (item.get("fecha") or "").strip()
    comision = (item.get("nombreComision") or "").strip()
    if not fecha or not comision:
        return None
    tema = (item.get("nombreSesion") or "").strip()
    extras = (item.get("caracteristicas") or "").strip()
    if extras:
        tema = f"{tema} ({extras})"
    return {
        "fecha": fecha,
        "hora": _hora_visor(item.get("horaInicio", "")),
        "tema": tema,
        "organiza": f"Comisión {comision}",
        "lugar": "",
        "camara": _VISOR_CAMARAS[codigo],
        "es_comision": True,
        "estado": (item.get("estado") or "").strip(),
        "enlace": VISOR_SESIONES_WEB + codigo.lower(),
        "fuente": "Visor de Sesiones de Comisiones",
    }


async def _sesiones_visor(c, desde: date, hasta: date) -> tuple[list[dict], list[str]]:
    """Sesiones de comisión de Diputados y Senado en [desde, hasta], y las cámaras que fallaron."""
    async def _camara(codigo):
        params = {"codigoTipoParlamento": codigo, "periodoParlamentario": "",
                  "periodoLegislativo": "", "tipoComision": "", "comision": "",
                  "sesion": "", "descentralizada": "", "conjunta": "", "continuada": "",
                  "fechaInicio": desde.isoformat(), "fechaFin": hasta.isoformat(), "fecha": ""}
        try:
            r = await c.get(VISOR_SESIONES_API, params=params, headers={**HEADERS, **_VISOR_AUTH})
            r.raise_for_status()
            items = r.json().get("result") or []
        except Exception as _e:
            logger.warning("visor sesiones %s: %s: %s", codigo, type(_e).__name__, _e)
            return codigo, None
        return codigo, [s for s in (_sesion_visor(i, codigo) for i in items) if s]

    sesiones, fallas = [], []
    for codigo, lista in await asyncio.gather(*[_camara(k) for k in _VISOR_CAMARAS]):
        if lista is None:
            fallas.append(_VISOR_CAMARAS[codigo])
        else:
            sesiones.extend(lista)
    return sesiones, fallas


def _misma_sesion(visor: dict, agenda: dict) -> bool:
    """La sesión del visor ya está en la agenda de comunicaciones (mismo día, cámara y comisión)."""
    if visor["fecha"] != agenda["fecha"]:
        return False
    if agenda["camara"] in ("Senado", "Cámara de Diputados") and agenda["camara"] != visor["camara"]:
        return False
    claves = [p for p in re.findall(r"[A-Z]+", _sin_acentos(visor["organiza"]))
              if p not in _PALABRAS_VACIAS and len(p) > 2]
    texto = _sin_acentos(agenda["tema"] + " " + agenda["organiza"])
    return bool(claves) and all(p in texto for p in claves)


def _unir_visor(agenda: list[dict], visor: list[dict]) -> list[dict]:
    """Suma a la agenda las convocatorias del visor que no traía. Ante un duplicado
    queda la de la agenda (trae lugar y PDF), con el estado del visor."""
    nuevas = []
    for v in visor:
        igual = next((a for a in agenda if a["es_comision"] and _misma_sesion(v, a)), None)
        if igual is not None:
            if v.get("estado"):
                igual.setdefault("estado", v["estado"])
        else:
            nuevas.append(v)

    def _orden(s):
        try:
            f = datetime.strptime(s["fecha"], "%d/%m/%Y")
        except ValueError:
            return (datetime.max, 0)
        try:
            h = datetime.strptime(s["hora"].upper().replace(" ", ""), "%I:%M%p")
            return (f, h.hour * 60 + h.minute)
        except ValueError:
            return (f, 24 * 60)
    return sorted(agenda + nuevas, key=_orden)


async def fetch_agenda_camaras(dias: int = 2, camara: str = None):
    """
    Obtiene la agenda parlamentaria bicameral desde comunicaciones.congreso.gob.pe/agenda.
    Cubre Senado, Cámara de Diputados, Pleno del Congreso y Comisiones.
    `camara` filtra opcionalmente: 'senado', 'diputados', 'pleno', 'comision'.

    Un request por día, en paralelo (antes era secuencial: con dias=14 tardaba
    ~22s porque cada día es un GET aparte a un WordPress que no tiene endpoint
    de rango — verificado en vivo al armar el dashboard de bienvenida, que
    necesita una ventana ancha para encontrar la próxima sesión).
    """
    from datetime import timedelta

    today = ahora_lima().date()

    async def _un_dia(c, dia):
        url = f"{AGENDA_COMUNICACIONES}/{dia.year}/{dia.month}/{dia.day}/"
        try:
            r = await c.get(url, headers=HEADERS)
            # WordPress puede devolver 404 con contenido válido — no saltarse
            if not r.text or len(r.text) < 500:
                return [], []
            return _parsear_agenda_dia(r.text, dia, url)
        except Exception as _e:
            logger.debug("sesiones day parse failed: %s", _e)
            return [], []

    async with _client() as c:
        resultados, (visor, fallas_visor) = await asyncio.gather(
            asyncio.gather(*[_un_dia(c, today + timedelta(days=delta)) for delta in range(dias)]),
            _sesiones_visor(c, today, today + timedelta(days=max(dias, 1) - 1)),
        )
    sesiones_total = [s for sesiones_dia, _ in resultados for s in sesiones_dia]
    avisos = [a for _, avisos_dia in resultados for a in avisos_dia]
    # No todo se sube a la agenda: el visor de cada cámara trae convocatorias
    # que comunicaciones nunca publica.
    sesiones_total = _unir_visor(sesiones_total, visor)

    if camara:
        # Sin acentos: "comision" no matcheaba "Comisión" y el filtro de
        # comisiones devolvía vacío.
        pedido = _sin_acentos(camara)
        if pedido.startswith("COMISI"):
            sesiones_total = [s for s in sesiones_total if s["es_comision"]]
        else:
            # El tema solo cuenta si la cámara no se pudo detectar: la
            # Comisión de Constitución del Senado debatía una propuesta
            # "remitida por la Cámara de Diputados" y salía en Diputados.
            sesiones_total = [s for s in sesiones_total
                              if pedido in _sin_acentos(s["camara"])
                              or (s["camara"] not in ("Senado", "Cámara de Diputados")
                                  and pedido in _sin_acentos(s["tema"]))]
            # La Bicameral es de las dos cámaras. Con su etiqueta propia,
            # Mistral la dejaba fuera de "comisiones de Diputados": se la
            # rotula con la cámara pedida.
            nombre_pedido = {"SENADO": "Senado", "DIPUTADOS": "Cámara de Diputados"}.get(
                pedido.replace("CAMARA DE ", "").strip())
            if nombre_pedido:
                sesiones_total = [{**s, "camara": f"{nombre_pedido} (Comisión Bicameral)"}
                                  if s["camara"].startswith("Bicameral") else s
                                  for s in sesiones_total]

    if not sesiones_total:
        vacio = {
            "sin_datos": True,
            "mensaje": f"No hay sesiones programadas en los próximos {dias} días"
                       + (f" para '{camara}'" if camara else "") + ".",
        }
        if avisos:
            vacio["avisos"] = avisos
        return vacio

    resultado = {
        "fuente": (f"Agenda del Congreso (comunicaciones.congreso.gob.pe/agenda) y Visor de "
                   f"Sesiones de Comisiones de Diputados y Senado — {dias} días"),
        "sesiones": sesiones_total,
        "total": len(sesiones_total),
    }
    if fallas_visor:
        resultado["aviso_fuentes"] = ("No respondió el Visor de Sesiones de: "
                                      + ", ".join(fallas_visor)
                                      + ". Puede haber convocatorias que no aparecen aquí.")
    comisiones = sum(1 for x in sesiones_total if x["es_comision"])
    if comisiones:
        # Mistral dejaba afuera la sesión de la Bicameral cuando se pedía una
        # cámara, aunque el prompt la incluye: dicho en los datos, la respeta.
        resultado["instruccion"] = (
            f"Hay {comisiones} sesiones con es_comision=true: todas van en la tabla de "
            "sesiones de comisión, incluidas las de cámara 'Bicameral (Senado y Diputados)', "
            "que son de las dos cámaras.")
    if avisos:
        resultado["avisos"] = avisos
    return resultado


# Encabezados de cada actividad en la agenda de comunicaciones.
_AGENDA_HEADERS = {"HORA", "TEMA", "ORGANIZA", "LUGAR"}
# "Descargar" es el botón con el PDF de la actividad (la agenda de la sesión
# de comisión, el oficio). El resto es navegación del pie de página, que solo
# aparece pegada al lugar de la ÚLTIMA actividad del día.
_AGENDA_RUIDO = {"Descargar", "Portal", "Inicio", "Noticias"}
# Líneas que puede ocupar cada campo. El tema llega a 3 (título, "(Continuación
# de la Primera Sesión No Ordinaria)" y el asunto a votar, visto el
# 15/10/2026): con el tope fijo de 2 el parser quedaba parado en la tercera,
# no encontraba ORGANIZA ni LUGAR y la sesión de Constitución del Senado salía
# sin cámara, sin lugar y fuera del filtro "senado". El lugar sí se corta en
# 2 porque en la última actividad del día lo sigue el pie de página.
# La Bicameral de Presupuesto lista a sus invitados dentro del tema: 15
# líneas el 16/10/2026. El tope del tema solo frena un HTML roto.
_AGENDA_TOPE = {"TEMA": 40, "ORGANIZA": 4, "LUGAR": 2}
_HORA_RE = re.compile(r"^\d{1,2}:\d{2}$")


def _parsear_agenda_dia(html: str, dia: date, url: str) -> tuple[list[dict], list[dict]]:
    """
    Actividades de un día de comunicaciones.congreso.gob.pe/agenda.

    Estructura de la página: HORA / hh:mm / AM|PM / TEMA / 1-3 líneas /
    [Descargar] / ORGANIZA / 1-2 líneas / LUGAR / 1-2 líneas. Cada campo se
    recolecta hasta el próximo encabezado. Los bloques sin hora ("Semana de
    Representación — del lunes 12 al viernes 16") son avisos, no sesiones:
    explican por qué un día no tiene sesiones de comisión.

    Devuelve (sesiones, avisos).
    """
    soup = BeautifulSoup(html, "html.parser")
    # Los PDFs de "Descargar", en el mismo orden en que aparece el texto.
    descargas = iter([a.get("href") for a in soup.find_all("a")
                      if a.get_text(strip=True) == "Descargar"])
    texto = soup.get_text(separator="\n", strip=True)
    lineas = [l.strip() for l in texto.split("\n") if l.strip()]

    def _recolectar(j, campo):
        partes, documento, vistas = [], None, 0
        while (j < len(lineas) and lineas[j] not in _AGENDA_HEADERS
               and not _HORA_RE.match(lineas[j]) and vistas < _AGENDA_TOPE[campo]):
            if lineas[j] == "Descargar":
                documento = next(descargas, None)
            elif lineas[j] not in _AGENDA_RUIDO:
                partes.append(lineas[j])
            vistas += 1
            j += 1
        while j < len(lineas) and lineas[j] in _AGENDA_RUIDO:
            if lineas[j] == "Descargar":
                documento = next(descargas, None)
            j += 1
        return " ".join(partes), documento, j

    sesiones, avisos = [], []
    i = 0
    while i < len(lineas):
        hora = ""
        if _HORA_RE.match(lineas[i]):
            hora = lineas[i]
            j = i + 1
            if j < len(lineas) and lineas[j].upper() in ("AM", "PM"):
                hora = f"{hora} {lineas[j]}"
                j += 1
        elif lineas[i] == "HORA" and i + 1 < len(lineas) and lineas[i + 1] == "TEMA":
            j = i + 1   # bloque sin hora: aviso
        else:
            i += 1
            continue

        campos = {"TEMA": "", "ORGANIZA": "", "LUGAR": ""}
        documento = None
        for campo in campos:
            if j < len(lineas) and lineas[j] == campo:
                campos[campo], doc, j = _recolectar(j + 1, campo)
                documento = documento or doc
        tema, organiza, lugar = campos["TEMA"], campos["ORGANIZA"], campos["LUGAR"]

        if not hora:
            if tema:
                avisos.append({"fecha": dia.strftime("%d/%m/%Y"), "aviso": tema,
                               **({"documento": documento} if documento else {})})
            i = max(j, i + 1)
            continue

        # La cámara sale primero de quien organiza ("Presidente: Senador
        # Carlos Mesía"): el tema puede nombrar a la otra cámara ("remitida
        # por la Cámara de Diputados") y confundir la detección.
        camara = _detectar_camara(organiza)
        # La Bicameral la preside un senador, pero es de las dos cámaras: así
        # sale en los pedidos de "Senado" y de "Diputados".
        if "BICAMERAL" in _sin_acentos(tema + " " + organiza):
            camara = "Bicameral (Senado y Diputados)"
        elif camara not in ("Senado", "Cámara de Diputados"):
            camara = _detectar_camara(tema + " " + organiza + " " + lugar)
        sesion = {
            "fecha": dia.strftime("%d/%m/%Y"),
            "hora": hora,
            "tema": tema,
            "organiza": organiza,
            "lugar": lugar,
            "camara": camara,
            "es_comision": _es_de_comision(tema + " " + organiza),
            "enlace": url,
        }
        if documento:
            sesion["documento"] = documento
        sesiones.append(sesion)
        i = j
    return sesiones, avisos


def _es_de_comision(texto: str) -> bool:
    """Sesión, mesa o audiencia que organiza una comisión (de cualquier cámara)."""
    return "COMISION" in _sin_acentos(texto)


def _detectar_camara(texto: str) -> str:
    t = texto.lower()
    # "senad"/"diputad": las comisiones que preside una senadora o una
    # diputada ("Presidenta: Diputada Flor Meza") quedaban sin cámara.
    if "senad" in t:
        return "Senado"
    if "diputad" in t:
        return "Cámara de Diputados"
    if "pleno" in t:
        return "Pleno del Congreso"
    if "comisión" in t or "comision" in t:
        return "Comisión"
    return "Congreso"


# ── Cuadro de comisiones y Comisión Permanente ─────────────────
#
# La API SPLEY (/comisiones) es un remanente del Congreso unicameral: devuelve
# 89 registros que mezclan comisiones ordinarias, especiales, investigadoras,
# bicamerales-de-transición y hasta comisiones ya disueltas — nunca se depuró
# tras la instalación del Congreso bicameral (2026) y no separa por cámara.
# Verificado en vivo: la respuesta actual sigue trayendo esa mezcla.
#
# El cuadro real de comisiones ordinarias vive en el Reglamento de cada
# cámara, no en una API. Se transcribe acá desde fuente primaria oficial:
#   - Senado: Reglamento del Senado, Art. 48 — "Nómina de las comisiones
#     ordinarias legislativas" (verificado contra el PDF oficial de la
#     Agenda del Pleno del Senado del 05/08/2026, que lo cita textual).
#   - Diputados: Reglamento de la Cámara de Diputados, Art. 45 y 46 —
#     descargado de https://www.congreso.gob.pe/wp-content/uploads/2026/07/
#     Reglamento-de-la-Camara-de-Diputados.pdf y verificado (16 legislativas,
#     coincide con el conteo publicado por prensa).
# Es una estructura fija por reglamento, no algo que cambie semana a semana;
# si el Pleno reforma el reglamento y crea o fusiona comisiones, esta lista
# hay que actualizarla a mano — igual que estado_legislativo() con las fechas
# de legislatura.
COMISIONES_SENADO = {
    "legislativas": [
        "Constitución, Reglamento y Relaciones Exteriores",
        "Defensa Nacional y Orden Interno",
        "Desarrollo Productivo, Energía y Minas, Infraestructura y Trabajo",
        "Economía, Medio Ambiente y Defensa del Consumidor",
        "Salud, Educación, Cultura, Mujer y Desarrollo Social y Digital",
        "Gestión del Estado y Contraloría",
        "Justicia y Derechos Humanos",
    ],
    "no_legislativas": [
        "Ética Parlamentaria",
        "Procedimientos Especiales",
        "Inteligencia",
        "Control Político sobre Actos Normativos del Ejecutivo",
    ],
}

COMISIONES_DIPUTADOS = {
    "legislativas": [
        "Constitución, Reglamento y Relaciones Exteriores",
        "Defensa Nacional y Orden Interno",
        "Desarrollo Agrario",
        "Defensa del Consumidor y Regulación de los Servicios Públicos",
        "Modernización de la Gestión del Estado y Contraloría",
        "Economía, Banca, Finanzas e Inteligencia Financiera",
        "Educación, Cultura y Deporte",
        "Energía y Minas",
        "Justicia y Derechos Humanos",
        "Inclusión Social, Familia, Mujer y Pueblos Andinos, Amazónicos y Afroperuanos",
        "Producción, Comercio Exterior y Turismo",
        "Medio Ambiente y Sostenibilidad",
        "Salud",
        "Trabajo y Seguridad Social",
        "Infraestructura, Vivienda y Transportes",
        "Ciencia, Innovación Tecnológica y Sociedad Digital",
    ],
    "no_legislativas": [
        "Ética Parlamentaria",
        "Ordenamiento y Seguimiento Legislativo",
        "Acusaciones Constitucionales",
    ],
}

# Compartida por ambas cámaras (Art. 38 del Reglamento de la Cámara de
# Diputados: se rige por el Reglamento del Congreso, no por el de cada
# cámara por separado).
COMISION_BICAMERAL_PRESUPUESTO = "Comisión Bicameral de Presupuesto y de la Cuenta General de la República"

# La Comisión Permanente es un órgano aparte de las comisiones ordinarias: no
# es "una comisión más" ni tampoco lo mismo que la Bicameral de Presupuesto.
# Descripción verificada contra la página oficial congreso.gob.pe/comision-
# permanente/ (fetch en vivo, 04/08/2026) — no hay página con la nómina de
# integrantes vigente, mismo caso que las comisiones ordinarias.
COMISION_PERMANENTE = {
    "descripcion": (
        "Órgano conformado por igual número de senadores y diputados elegidos "
        "por sus respectivas cámaras, más los miembros de las mesas directivas "
        "del Senado y la Cámara de Diputados como miembros natos. La preside "
        "el presidente del Congreso. Funciona durante el receso del Senado y "
        "la Cámara de Diputados, y no excede el 20% del total de miembros del "
        "Congreso."
    ),
    "enlaces": {
        "Comisión Permanente": "https://www.congreso.gob.pe/comision-permanente/",
        "Sesiones de la Comisión Permanente": "https://www.congreso.gob.pe/sesiones-de-la-comision-permanente/",
    },
}

# Enlaces oficiales verificados (todos responden 200). El visor de PDF de las
# páginas /comisiones/ del Senado y Diputados está roto del lado del Congreso
# —el iframe nunca recibe archivo—, así que no sirve como fuente de datos,
# pero sigue funcionando como acceso directo para el usuario.
COMISIONES_ENLACES = {
    "senado": {
        "Comisiones del Senado":        "https://senado.congreso.gob.pe/comisiones/",
        "Portal del Senado":            "https://senado.congreso.gob.pe/",
    },
    "diputados": {
        "Comisiones de Diputados":      "https://diputados.congreso.gob.pe/comisiones/",
        "Portal de Diputados":          "https://diputados.congreso.gob.pe/",
    },
    "comunes": {
        "Visor de sesiones de comisiones":
            "https://wb2server.congreso.gob.pe/visor-sesiones/#/",
        "Comisiones especiales":
            "https://wb2server.congreso.gob.pe/comisiones-especiales-visor/#/comisiones/especiales",
        "Comisiones investigadoras":
            "https://wb2server.congreso.gob.pe/comisiones-investigadoras-visor/#/comisiones/investigadoras",
    },
}


# ── Integrantes de comisiones ────────────────────────────────
#
# Desde el bicameral cada cámara publica, en su portal, una página por
# comisión con la tabla de integrantes (Apellidos y Nombres / Grupo
# Parlamentario / Cargo / e-Mail), y www.congreso.gob.pe/comision-permanente/
# la nómina de la Comisión Permanente aprobada el 18/08/2026. Verificado el
# 15/10/2026. Antes Diana decía que la composición "no está en una fuente
# abierta".
COMISION_PERMANENTE_URL = f"{CONGRESO}/comision-permanente/"
COMISIONES_INDICE = {"Senado": f"{SENADO}/comisiones/",
                     "Cámara de Diputados": f"{DIPUTADOS}/comisiones/"}


def _parsear_tabla_miembros(html: str) -> list[dict]:
    """
    Integrantes de la primera tabla con encabezado "Apellidos y Nombres".

    La página repite la tabla en formato celular: con la primera alcanza.
    La cámara sale del enlace al perfil (senado./diputados.), porque la
    Comisión Permanente mezcla senadores y diputados en una sola lista.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tabla in soup.find_all("table"):
        encabezados = [_sin_acentos(th.get_text(" ", strip=True)) for th in tabla.find_all("th")]
        if not any("APELLIDOS" in e for e in encabezados):
            continue
        miembros = []
        for fila in tabla.find_all("tr"):
            celdas = fila.find_all("td")
            if len(celdas) < len(encabezados) or len(celdas) < 2:
                continue
            fila_dict = {}
            for enc, td in zip(encabezados, celdas):
                texto = " ".join(td.get_text(" ", strip=True).split())
                if "FOTO" in enc:
                    img = td.find("img")
                    if img and url_permitida(img.get("src") or ""):
                        fila_dict["foto"] = img["src"]
                elif "APELLIDOS" in enc:
                    fila_dict["nombre"] = texto
                    enlace = td.find("a", href=True)
                    if enlace and url_permitida(enlace["href"]):
                        fila_dict["perfil"] = enlace["href"]
                    host = urllib.parse.urlparse(enlace["href"]).hostname if enlace else ""
                    if host and host.startswith("senado."):
                        fila_dict["camara"] = "Senado"
                    elif host and host.startswith("diputados."):
                        fila_dict["camara"] = "Cámara de Diputados"
                elif "GRUPO" in enc:
                    fila_dict["grupo_parlamentario"] = texto
                elif "CARGO" in enc:
                    fila_dict["cargo"] = texto
                elif "MAIL" in enc:
                    fila_dict["email"] = texto
            if fila_dict.get("nombre"):
                miembros.append(fila_dict)
        if miembros:
            return miembros
    return []


def _parsear_indice_comisiones(html: str, base: str) -> dict[str, str]:
    """Nombre de cada comisión → su página, desde /comisiones/ de una cámara."""
    host = urllib.parse.urlparse(base).hostname
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for a in soup.find_all("a", href=True):
        nombre = " ".join(a.get_text(" ", strip=True).split()).rstrip(".")
        # Las especiales también cuelgan de /comisiones/ de Diputados, pero no
        # son del cuadro de ordinarias: el modelo las listaba como "no
        # legislativas".
        if (_sin_acentos(nombre).startswith("COMISION ")
                and not _sin_acentos(nombre).startswith("COMISION ESPECIAL")
                and urllib.parse.urlparse(a["href"]).hostname == host
                and a["href"].rstrip("/") != base.rstrip("/")):
            out.setdefault(nombre, a["href"])
    return out


async def _indices_comisiones(c, camaras: list[str]) -> dict[str, dict[str, str]]:
    async def uno(camara):
        try:
            r = await c.get(COMISIONES_INDICE[camara])
            if r.status_code == 200:
                return camara, _parsear_indice_comisiones(r.text, COMISIONES_INDICE[camara])
        except Exception as _e:
            logger.warning("índice de comisiones de %s: %s", camara, _e)
        return camara, {}
    return dict(await asyncio.gather(*(uno(cam) for cam in camaras)))


async def _nomina_comision_permanente(c) -> dict:
    try:
        r = await c.get(COMISION_PERMANENTE_URL)
        miembros = _parsear_tabla_miembros(r.text) if r.status_code == 200 else []
    except Exception as _e:
        logger.warning("nómina de la Comisión Permanente: %s", _e)
        miembros = []
    if not miembros:
        return {"sin_datos": True,
                "mensaje": "No se pudo leer la nómina en www.congreso.gob.pe/comision-permanente/."}
    return {"fuente": COMISION_PERMANENTE_URL, "miembros": miembros,
            "total": len(miembros),
            "senadores": sum(1 for m in miembros if m.get("camara") == "Senado"),
            "diputados": sum(1 for m in miembros if m.get("camara") == "Cámara de Diputados")}


def _palabras_clave(texto: str) -> set[str]:
    vacias = {"COMISION", "DE", "DEL", "LA", "LAS", "LOS", "Y", "E", "EN", "ASUNTOS", "EL"}
    return {w for w in re.findall(r"[A-Z]+", _sin_acentos(texto)) if w not in vacias and len(w) > 2}


async def fetch_miembros_comision(comision: str, camara: str = None):
    """
    Integrantes de una comisión ordinaria, desde la página de la comisión en
    el portal de su cámara. `comision` es el nombre o parte ("Constitución",
    "energía y minas"); sin `camara` busca en las dos, porque varias
    comisiones existen con el mismo nombre en ambas cámaras.
    """
    cam = _sin_acentos(camara or "")
    camaras = [k for k in COMISIONES_INDICE
               if not cam or ("SENAD" in cam and k == "Senado")
               or ("DIPUTAD" in cam and k == "Cámara de Diputados")] or list(COMISIONES_INDICE)
    if _sin_acentos(comision or "").strip().startswith("PERMANENTE") or \
            "COMISION PERMANENTE" in _sin_acentos(comision or ""):
        async with _client() as c:
            return {"comision_permanente": await _nomina_comision_permanente(c)}

    buscadas = _palabras_clave(comision or "")
    async with _client() as c:
        indices = await _indices_comisiones(c, camaras)
        elegidas = []
        for camara_k, indice in indices.items():
            mejor = max(indice.items(), default=None,
                        key=lambda kv: len(buscadas & _palabras_clave(kv[0])))
            if mejor and buscadas & _palabras_clave(mejor[0]):
                elegidas.append((camara_k, *mejor))
        if not elegidas:
            disponibles = {k: sorted(v) for k, v in indices.items()}
            return {"sin_datos": True,
                    "mensaje": f"No encontré una comisión que coincida con «{comision}».",
                    "comisiones_disponibles": disponibles}

        async def miembros_de(camara_k, nombre, url):
            try:
                r = await _get_oficial(c, url)
                miembros = _parsear_tabla_miembros(r.text) if r is not None and r.status_code == 200 else []
            except Exception as _e:
                logger.warning("integrantes de %s: %s", url, _e)
                miembros = []
            for m in miembros:
                m.pop("camara", None)   # ya está en la comisión
            return {"camara": camara_k, "comision": nombre, "enlace": url,
                    "miembros": miembros, "total": len(miembros),
                    **({} if miembros else {"sin_datos": True})}

        resultados = await asyncio.gather(*(miembros_de(*e) for e in elegidas))
    return {"fuente": "Portales del Senado y de la Cámara de Diputados — página de cada comisión",
            "comisiones": resultados}


async def fetch_comisiones(camara: str = None):
    """
    Cuadro de comisiones ordinarias del Senado y/o la Cámara de Diputados.

    Estructura fija tomada del Reglamento de cada cámara (ver comentario
    arriba de COMISIONES_SENADO) — no de la API SPLEY, que quedó desactualizada
    tras el paso al bicameral. Trae además la página oficial de cada comisión
    (donde cada cámara publica sus integrantes) y la nómina de la Comisión
    Permanente; los integrantes de una comisión puntual los da
    fetch_miembros_comision.
    """
    cam = (camara or "").lower().strip()
    incluir_senado = cam in ("", "senado")
    incluir_diputados = cam in ("", "diputados")

    camaras = {}
    if incluir_senado:
        camaras["Senado"] = {
            "comisiones_legislativas": COMISIONES_SENADO["legislativas"],
            "comisiones_no_legislativas": COMISIONES_SENADO["no_legislativas"],
            "total_legislativas": len(COMISIONES_SENADO["legislativas"]),
        }
    if incluir_diputados:
        camaras["Cámara de Diputados"] = {
            "comisiones_legislativas": COMISIONES_DIPUTADOS["legislativas"],
            "comisiones_no_legislativas": COMISIONES_DIPUTADOS["no_legislativas"],
            "total_legislativas": len(COMISIONES_DIPUTADOS["legislativas"]),
        }

    enlaces = dict(COMISIONES_ENLACES["comunes"])
    if cam in ("senado", "diputados"):
        enlaces = {**COMISIONES_ENLACES[cam], **enlaces}
    else:
        enlaces = {**COMISIONES_ENLACES["senado"],
                   **COMISIONES_ENLACES["diputados"], **enlaces}

    async with _client() as c:
        indices, nomina = await asyncio.gather(
            _indices_comisiones(c, list(camaras)), _nomina_comision_permanente(c))
    for nombre_camara, datos in camaras.items():
        if indices.get(nombre_camara):
            datos["paginas_de_comisiones"] = indices[nombre_camara]

    return {
        "fuente": "Reglamento del Senado (Art. 48) y Reglamento de la Cámara de "
                  "Diputados (Art. 45-46); páginas de cada comisión y nómina de la "
                  "Comisión Permanente desde los portales oficiales",
        "camaras": camaras,
        "comision_bicameral_presupuesto": COMISION_BICAMERAL_PRESUPUESTO,
        "comision_permanente": {**COMISION_PERMANENTE, "nomina": nomina},
        "enlaces_oficiales": enlaces,
        "nota_composicion": (
            "Cada comisión publica sus integrantes en su página oficial "
            "(\"paginas_de_comisiones\"). Para la lista de una comisión puntual "
            "existe la herramienta fetch_miembros_comision."
        ),
    }


# ── Comisiones para la pantalla "Comisiones" de la app ─────────
#
# Lo mismo que lee el chat, más la ficha de cada integrante (su página de
# perfil en el portal de su cámara): partido político, región que
# representa, condición, votación obtenida y periodo. Verificado el
# 27/10/2026 en diputados.congreso.gob.pe/diputado/… y senado…/senador/….

_FICHA_CAMPOS = {
    "PARTIDO POLITICO": "partido_politico",
    "PERIODO DE FUNCIONES": "periodo",
    "GRUPO PARLAMENTARIO": "grupo_parlamentario",
    "REPRESENTA A": "representa_a",
    "CONDICION": "condicion",
    "VOTACION OBTENIDA": "votacion",
    "CORREO INSTITUCIONAL": "email",
}
_CACHE_FICHAS: dict[str, tuple[float, dict]] = {}
_TTL_FICHAS = 12 * 3600          # una ficha cambia muy de vez en cuando
_PERIODO_ANUAL_RE = re.compile(r"PERIODO ANUAL DE SESIONES\s+(\d{4}\s*-\s*\d{4})", re.IGNORECASE)


def _parsear_ficha(html: str) -> dict:
    """Etiqueta → valor de la ficha de un diputado o senador."""
    soup = BeautifulSoup(html, "html.parser")
    for x in soup(["header", "footer", "nav", "script", "style"]):
        x.decompose()
    lineas = [l.strip() for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]
    ficha = {}
    for i, linea in enumerate(lineas[:-1]):
        campo = _FICHA_CAMPOS.get(_sin_acentos(linea).rstrip(":"))
        if campo and campo not in ficha:
            valor = lineas[i + 1]
            # "No registrado/a": la fuente no tiene el dato; mejor no mostrarlo.
            if (_sin_acentos(valor).rstrip(":") not in _FICHA_CAMPOS
                    and not _sin_acentos(valor).startswith("NO REGISTRAD")):
                ficha[campo] = valor
    return ficha


async def _ficha(c, url: str) -> dict:
    hit = _CACHE_FICHAS.get(url)
    if hit and time.time() - hit[0] < _TTL_FICHAS:
        return hit[1]
    try:
        r = await _get_oficial(c, url)
        ficha = _parsear_ficha(r.text) if r is not None and r.status_code == 200 else {}
    except Exception as _e:
        logger.info("ficha %s: %s", url, _e)
        return {}
    if ficha:
        _CACHE_FICHAS[url] = (time.time(), ficha)
    return ficha


def _tipo_comision(camara: str, nombre: str) -> str:
    cuadro = COMISIONES_SENADO if camara == "Senado" else COMISIONES_DIPUTADOS
    claves = _palabras_clave(nombre)
    for tipo, lista in (("legislativa", cuadro["legislativas"]),
                        ("no legislativa", cuadro["no_legislativas"])):
        for oficial in lista:
            k = _palabras_clave(oficial)
            if k and len(claves & k) >= max(1, int(len(k) * 0.6)):
                return tipo
    return ""


async def fetch_comisiones_app():
    """Listado para la pantalla Comisiones: cada comisión con su cámara, tipo y página."""
    async with _client() as c:
        indices = await _indices_comisiones(c, list(COMISIONES_INDICE))
    camaras = []
    for camara, indice in indices.items():
        camaras.append({
            "camara": camara,
            "comisiones": [{"nombre": n, "url": u, "tipo": _tipo_comision(camara, n)}
                           for n, u in indice.items()],
            **({} if indice else {"error": f"No se pudo leer el índice de comisiones de {camara}."}),
        })
    return {
        "fuente": "Portales del Senado y de la Cámara de Diputados",
        "camaras": camaras,
        "comision_permanente": {"nombre": "Comisión Permanente", "url": COMISION_PERMANENTE_URL,
                                "descripcion": COMISION_PERMANENTE["descripcion"]},
        "enlaces_oficiales": {**COMISIONES_ENLACES["senado"], **COMISIONES_ENLACES["diputados"],
                              **COMISIONES_ENLACES["comunes"]},
    }


_CACHE_INTEGRANTES: tuple[float, dict] | None = None


async def fetch_integrantes_todas():
    """
    Quién integra qué comisión, en todas las comisiones de las dos cámaras y
    la Comisión Permanente (solo la tabla de cada página, sin las fichas:
    ~30 pedidos en paralelo). Sirve para buscar a un legislador y ver todas
    sus comisiones.
    """
    global _CACHE_INTEGRANTES
    if _CACHE_INTEGRANTES and time.time() - _CACHE_INTEGRANTES[0] < _TTL_FICHAS:
        return _CACHE_INTEGRANTES[1]
    async with _client() as c:
        indices = await _indices_comisiones(c, list(COMISIONES_INDICE))
        paginas = [(cam, n, u) for cam, ind in indices.items() for n, u in ind.items()]
        paginas.append(("Congreso", "Comisión Permanente", COMISION_PERMANENTE_URL))
        sem = asyncio.Semaphore(8)

        async def una(cam, nombre, url):
            async with sem:
                try:
                    r = await _get_oficial(c, url)
                    return cam, nombre, url, (_parsear_tabla_miembros(r.text)
                                              if r is not None and r.status_code == 200 else None)
                except Exception as _e:
                    logger.info("integrantes %s: %s", url, _e)
                    return cam, nombre, url, None
        resultados = await asyncio.gather(*(una(*p) for p in paginas))

    personas: dict[str, dict] = {}
    fallidas = []
    for cam, nombre, url, miembros in resultados:
        if miembros is None:
            fallidas.append(nombre)
            continue
        for m in miembros:
            clave = m.get("perfil") or _sin_acentos(m["nombre"])
            p = personas.setdefault(clave, {"nombre": m["nombre"], "perfil": m.get("perfil", ""),
                                            "foto": m.get("foto", ""), "email": m.get("email", ""),
                                            "grupo_parlamentario": m.get("grupo_parlamentario", ""),
                                            "camara": m.get("camara") or cam, "comisiones": []})
            p["comisiones"].append({"nombre": nombre, "url": url, "camara": cam,
                                    "cargo": m.get("cargo", "")})
    resultado = {"integrantes": sorted(personas.values(), key=lambda p: _sin_acentos(p["nombre"])),
                 "total": len(personas),
                 **({"no_leidas": fallidas} if fallidas else {})}
    if not fallidas:
        _CACHE_INTEGRANTES = (time.time(), resultado)
    return resultado


def _es_de_esta_comision(sesion: dict, nombre: str) -> bool:
    claves = _palabras_clave(nombre)
    texto = _palabras_clave(sesion.get("tema", "") + " " + sesion.get("organiza", ""))
    return bool(claves) and len(claves & texto) >= max(1, round(len(claves) * 0.7))


async def fetch_comision_detalle(url: str):
    """
    Una comisión: integrantes con su ficha (partido, región, condición,
    votación, correo), periodo anual y próximas sesiones (14 días).
    Solo páginas del índice oficial o la de la Comisión Permanente.
    """
    permanente = url.rstrip("/") == COMISION_PERMANENTE_URL.rstrip("/")
    async with _client() as c:
        if permanente:
            camara, nombre = "Congreso", "Comisión Permanente"
        else:
            indices = await _indices_comisiones(c, list(COMISIONES_INDICE))
            hallada = next(((cam, n) for cam, ind in indices.items()
                            for n, u in ind.items() if u.rstrip("/") == url.rstrip("/")), None)
            if not hallada:
                return {"error": "Esa página no está en el índice oficial de comisiones."}
            camara, nombre = hallada
        try:
            r = await _get_oficial(c, url)
            html = r.text if r is not None and r.status_code == 200 else ""
        except Exception as _e:
            logger.warning("comisión %s: %s", url, _e)
            html = ""
        if not html:
            return {"error": f"No respondió la página oficial de la comisión ({url})."}
        miembros = _parsear_tabla_miembros(html)
        periodo = _PERIODO_ANUAL_RE.search(BeautifulSoup(html, "html.parser").get_text(" "))

        sem = asyncio.Semaphore(8)

        async def con_ficha(m):
            if not m.get("perfil"):
                return m
            async with sem:
                ficha = await _ficha(c, m["perfil"])
            return {**ficha, **{k: v for k, v in m.items() if v}}
        miembros = await asyncio.gather(*(con_ficha(m) for m in miembros))

    for m in miembros:
        if camara != "Congreso":
            m.setdefault("camara", camara)

    sesiones = []
    try:
        filtro = {"Senado": "senado", "Cámara de Diputados": "diputados"}.get(camara)
        agenda = await fetch_agenda_camaras(dias=14, camara=filtro) if filtro else {}
        sesiones = [x for x in agenda.get("sesiones") or [] if _es_de_esta_comision(x, nombre)]
    except Exception as _e:
        logger.info("próximas sesiones de %s: %s", nombre, _e)

    grupos: dict[str, int] = {}
    for m in miembros:
        g = m.get("grupo_parlamentario") or "Sin grupo"
        grupos[g] = grupos.get(g, 0) + 1
    return {
        "fuente": url,
        "camara": camara,
        "nombre": nombre,
        "tipo": "" if permanente else _tipo_comision(camara, nombre),
        "periodo_anual": periodo.group(1).replace(" ", "") if periodo else "",
        "miembros": miembros,
        "total": len(miembros),
        "por_grupo": sorted(grupos.items(), key=lambda kv: -kv[1]),
        "proximas_sesiones": sesiones,
        "visor_sesiones": VISOR_SESIONES_WEB + {"Senado": "s", "Cámara de Diputados": "d"}.get(camara, ""),
        **({} if miembros else {"aviso": "La página oficial no trae la tabla de integrantes."}),
    }


# ── Estado legislativo (calendario de sesiones) ─────────────────
#
# No existe una fuente oficial única y siempre actualizada con estas fechas
# en formato consultable — pedidos.congreso.gob.pe/calendario/ (la candidata
# obvia) ni siquiera resuelve como dominio. Las fechas de acá salen de
# comunicados oficiales del Congreso (comunicaciones.congreso.gob.pe/noticias)
# y coinciden con la regla general del Reglamento:
#   - Período Anual de Sesiones: 27 de julio → 26 de julio del año siguiente.
#   - Primera Legislatura Ordinaria: 27 de julio → 15 de diciembre.
#   - Segunda Legislatura Ordinaria: 1 de marzo → 15 de junio.
# El Congreso a veces amplía el cierre de una legislatura por resolución —
# pasó en 2026, la Segunda Legislatura Ordinaria se extendió del 15 al 24 de
# junio (coincide con la última Agenda del Pleno real que se vio en pruebas).
# Esta función usa las fechas BASE, no las ampliaciones puntuales — puede
# quedar unos días desalineada en años con extensión hasta que se actualice
# a mano.
def estado_legislativo(hoy: date) -> dict:
    """Determina si `hoy` cae dentro de una Legislatura Ordinaria o en receso."""
    year = hoy.year
    inicio_periodo = year if hoy >= date(year, 7, 27) else year - 1

    primera_ini = date(inicio_periodo, 7, 27)
    primera_fin = date(inicio_periodo, 12, 15)
    segunda_ini = date(inicio_periodo + 1, 3, 1)
    segunda_fin = date(inicio_periodo + 1, 6, 15)

    if primera_ini <= hoy <= primera_fin:
        return {
            "en_legislatura": True,
            "legislatura": "Primera Legislatura Ordinaria",
            "fin": primera_fin.strftime("%d/%m/%Y"),
        }
    if segunda_ini <= hoy <= segunda_fin:
        return {
            "en_legislatura": True,
            "legislatura": "Segunda Legislatura Ordinaria",
            "fin": segunda_fin.strftime("%d/%m/%Y"),
        }
    proxima = segunda_ini if hoy < segunda_ini else date(inicio_periodo + 1, 7, 27)
    return {
        "en_legislatura": False,
        "legislatura": None,
        "proxima_legislatura": proxima.strftime("%d/%m/%Y"),
    }


def estado_legislativo_texto(hoy: date) -> str:
    """Versión en una línea de estado_legislativo(), lista para meter en un prompt."""
    e = estado_legislativo(hoy)
    if e["en_legislatura"]:
        return (
            f"Estado legislativo real: hoy el Congreso está en sesión — "
            f"{e['legislatura']} (vigente hasta el {e['fin']} salvo ampliación "
            f"por resolución). Si no hay sesiones/agenda igual, NO es por receso."
        )
    return (
        f"Estado legislativo real: hoy el Congreso está en receso parlamentario "
        f"— la próxima legislatura ordinaria empieza el {e['proxima_legislatura']}."
    )
