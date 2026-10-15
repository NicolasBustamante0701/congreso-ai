"""
ChatOrchestrator — las tres fases del endpoint /chat.

  Fase 1 (router, 8B)  elige qué herramienta hace falta, o ninguna.
  Fase 2               ejecuta las herramientas elegidas y recorta sus resultados.
  Fase 3 (main, 70B)   redacta la respuesta final en streaming.

Hay además un atajo: analizar un PDF ya cargado o una sesión de YouTube no
necesita scraping, así que va directo a Fase 3 con el workflow correspondiente.

Todos los métodos públicos son generadores asíncronos que emiten strings SSE
ya formateados (ver services/sse.py).
"""
import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

from config import ahora_lima, logger
from scraper import fetch_transcript_youtube
from services import documentos, llm, sse, tool_cache
from services.observability import timed
from services.prompt_registry import (
    ROUTER_PROMPT,
    SYSTEM_MINI,
    WORKFLOW_PDF_FORMULA,
    WORKFLOW_SESION,
    WORKFLOWS,
    resumen_con_fechas,
    system_con_fecha,
)
from services.tools import STATUS_LABELS, TOOL_MAP, TOOLS

DOC_ID_RE = re.compile(r"\[documento_id: ([0-9a-f]{12})\]")

# Un tool result de 7k chars ≈ 2000 tokens. Más que eso dispara el TPM.
MAX_TOOL_RESULT_CHARS = 7000

# Tope por intento de una herramienta. Cada request al Congreso ya tiene 25s
# de timeout, pero algunas herramientas encadenan varios (el expediente pide 5
# pestañas): sin tope total, una herramienta podía colgar el chat minutos.
TOOL_TIMEOUT_SECONDS = 60

# Herramientas que corren a la vez en Fase 2. El resumen semanal pide 5: con
# 4 en paralelo ninguna fuente del Congreso recibe una ráfaga de pedidos.
MAX_TOOLS_PARALELO = 4

# Tope propio para las herramientas que devuelven listados largos.
#
# Con 7000 el JSON de un "últimos 15 días" (89 proyectos, ~400 caracteres cada
# uno entre título, sumilla, autores y enlace) se cortaba a la mitad: el modelo
# recibía un JSON inválido terminado en "[recortado]" y no podía clasificar por
# temas ni citar los enlaces de los que no llegó a ver. Subirlo para todas las
# herramientas no hace falta —las demás devuelven objetos chicos— y encarece
# cada pedido, así que el margen extra va solo donde el listado lo justifica.
# Medido: 89 proyectos (una consulta real de "últimos 15 días") ocupan ~49 mil
# caracteres ya con el payload aligerado — sin sumilla duplicada, sin campos
# vacíos y con los autores acotados a tres. 60 mil deja margen para una ventana
# algo más cargada sin volver a cortar por la mitad.
MAX_TOOL_RESULT_CHARS_POR_TOOL = {
    "buscar_proyectos": 60000,
    # Índice + conteo de asuntos de hasta tres agendas (una por cámara) y el
    # texto de la principal: con 7000 el texto se comía todo el espacio.
    "fetch_agenda_pleno": 16000,
    # Un proyecto debatido en el Pleno pasa de 15 pasos de seguimiento (PL 98:
    # 18 000 caracteres). Con 7000 se cortaban justo los últimos: Diana no
    # veía que el Pleno lo aprobó tras una reconsideración y lo envió al Senado.
    "fetch_expediente": 22000,
    # Cuadro de las dos cámaras + página de cada comisión + nómina de la
    # Comisión Permanente (28 integrantes): ~12 mil caracteres.
    "fetch_comisiones": 16000,
    # Una comisión de Diputados llega a 40 integrantes, y "Constitución" sin
    # cámara trae las dos.
    "fetch_miembros_comision": 20000,
}

# Si aun así no entra, esto se descarta antes de cortar filas: repite los
# PDFs que ya vienen dentro de cada paso del seguimiento.
_DESCARTABLES = ("todos_los_adjuntos",)
# En estas listas lo último es lo más nuevo: se recortan por el principio.
_LISTAS_CRONOLOGICAS = ("seguimiento",)


def _compactar_para_agrupado(resultado):
    """Deja en cada proposición solo las columnas del cuadro agrupado."""
    if not isinstance(resultado, dict) or not isinstance(resultado.get("items"), list):
        return resultado
    items = [{k: p[k] for k in CAMPOS_AGRUPADO if p.get(k)} for p in resultado["items"]]
    return {**resultado, "items": items}


# "dividido por temas", "agrupado por materia": el cuadro lo arma el código.
POR_TEMAS_RE = re.compile(r"\b(temas?|tem[aá]tic\w*|materias?)\b", re.IGNORECASE)

PROMPT_CLASIFICAR = """Clasifica proposiciones legislativas del Congreso del Perú por tema.

Recibes una lista "numero | titulo". Devuelve SOLO un objeto JSON, sin texto antes ni después:
{"grupos": [{"tema": "Seguridad ciudadana y justicia", "numeros": ["00588-2026-2031-CD", ...]}, ...],
 "lectura": "2-3 oraciones: qué temas concentran la producción y qué patrón se ve."}

Reglas:
- Entre 4 y 8 temas, con nombres cortos y concretos.
- Cada número va en UN solo tema, y todos los números de la lista van en alguno.
- Copia los números exactamente como vienen.
- "lectura" se basa solo en los títulos; no inventes autores, fechas ni resultados."""


def _parsear_clasificacion(texto: str, numeros: list[str]) -> tuple[list[tuple[str, list[str]]], str] | None:
    """
    Grupos (tema, números) validados contra la lista real: cada número una
    sola vez, los que el modelo inventó afuera y los que olvidó en "Otros".
    None si la respuesta no es un JSON con grupos.
    """
    m = re.search(r"\{.*\}", texto, re.DOTALL)
    if not m:
        return None
    try:
        datos = json.loads(m.group(0))
        grupos_crudos = datos["grupos"]
    except (ValueError, KeyError, TypeError):
        return None
    validos = set(numeros)
    vistos: set[str] = set()
    grupos: list[tuple[str, list[str]]] = []
    for g in grupos_crudos if isinstance(grupos_crudos, list) else []:
        if not isinstance(g, dict):
            continue
        tema = str(g.get("tema") or "").strip()
        suyos = [n for n in (g.get("numeros") or [])
                 if isinstance(n, str) and n in validos and n not in vistos]
        if not tema or not suyos:
            continue
        vistos.update(suyos)
        grupos.append((tema, suyos))
    if not grupos:
        return None
    faltan = [n for n in numeros if n not in vistos]
    if faltan:
        grupos.append(("Otros", faltan))
    return grupos, str(datos.get("lectura") or "").strip()


def _cuadro_por_temas(items: list[dict], grupos: list[tuple[str, list[str]]],
                      lectura: str, total_disponible: int | None = None) -> str:
    """Markdown del cuadro agrupado (formato de workflow_proyectos.md)."""
    por_numero = {p["numero"]: p for p in items}
    total = len(items)
    temas = f"{len(grupos)} tema" + ("s" if len(grupos) != 1 else "")
    partes = [f"Hay {total} proposiciones legislativas en el periodo consultado. "
              f"Te las muestro todas, agrupadas en {temas}:\n"]
    if total_disponible and total_disponible > total:
        partes = [f"Hay {total_disponible} proposiciones en el periodo; te muestro "
                  f"{total}, agrupadas en {temas}:\n"]
    for tema, nums in grupos:
        partes.append(f"### {tema} ({len(nums)})\n")
        partes.append("| Nº | FECHA | TÍTULO |\n|---|---|---|")
        for n in nums:
            p = por_numero[n]
            titulo = " ".join(str(p.get("titulo") or "").replace("|", "/").split())
            num = f"[{n}]({p['enlace']})" if p.get("enlace") else n
            partes.append(f"| {num} | {p.get('fecha_presentacion') or '—'} | {titulo} |")
        partes.append("")
    partes.append(f"**{total} proposiciones en {temas}.**\n")
    if lectura:
        partes.append(f"### Mi lectura\n{lectura}\n")
    return "\n".join(partes)


# Lo que usa el resumen semanal de cada proposición: sin autores ni
# proponente, 134 proposiciones de 15 días entran sin recortar filas.
CAMPOS_RESUMEN = ("numero", "fecha_presentacion", "titulo", "estado", "camara", "enlace")


def _separar_por_semana(resultado, desde: str):
    """
    Parte las proposiciones del resumen semanal en las de la semana y las de
    contexto (8 a 15 días), por su fecha real.

    El prompt pedía esa separación y el modelo igual metía proyectos del
    06/10 entre los hallazgos de la semana del 08/10 al 15/10 (visto el
    15/10/2026). Separadas de antemano, cada lista ya dice dónde va.
    """
    if not isinstance(resultado, dict) or not isinstance(resultado.get("items"), list):
        return resultado
    try:
        corte = datetime.strptime(desde, "%d/%m/%Y").date()
    except ValueError:
        return resultado
    semana, contexto = [], []
    for p in resultado["items"]:
        fila = {k: p[k] for k in CAMPOS_RESUMEN if p.get(k)}
        try:
            fecha = datetime.strptime(p.get("fecha_presentacion") or "", "%d/%m/%Y").date()
        except ValueError:
            continue   # sin fecha no se puede ubicar en ninguna ventana
        (semana if fecha >= corte else contexto).append(fila)
    otros = {k: v for k, v in resultado.items() if k not in ("items", "total")}
    return {**otros,
            "proposiciones_de_la_semana": semana,
            "contexto_vigente": contexto,
            "nota": (f"\"proposiciones_de_la_semana\" son las presentadas desde el {desde}: "
                     "van en las secciones por tema. \"contexto_vigente\" son anteriores: "
                     "solo pueden ir en la sección Contexto vigente, nunca en los temas.")}


def _recortar_resultado(nombre: str, resultado) -> str:
    """
    Serializa el resultado de una herramienta dentro del tope que le toca.

    Antes se cortaba el JSON en el carácter N y se pegaba '... [recortado]"}':
    el modelo recibía un JSON roto, con la última fila partida a la mitad, y
    sin saber cuántas faltaban. Ahora se quitan FILAS enteras de la lista más
    larga hasta que entra, y se avisa con "truncado": {mostrados, total}, que
    el prompt le pide comunicar. Solo si no hay lista que recortar (un texto
    gigante) se cae al corte por caracteres.
    """
    tope = MAX_TOOL_RESULT_CHARS_POR_TOOL.get(nombre, MAX_TOOL_RESULT_CHARS)
    texto = json.dumps(resultado, ensure_ascii=False)
    if len(texto) <= tope:
        return texto

    if isinstance(resultado, dict) and any(k in resultado for k in _DESCARTABLES):
        resultado = {k: v for k, v in resultado.items() if k not in _DESCARTABLES}
        texto = json.dumps(resultado, ensure_ascii=False)
        if len(texto) <= tope:
            return texto

    if isinstance(resultado, dict):
        listas = [(k, v) for k, v in resultado.items() if isinstance(v, list) and len(v) > 1]
        if listas:
            clave, filas = max(listas, key=lambda kv: len(json.dumps(kv[1], ensure_ascii=False)))
            total = len(filas)
            # Búsqueda binaria de cuántas filas entran.
            lo, hi = 1, total - 1
            mejor = None
            while lo <= hi:
                mid = (lo + hi) // 2
                parte = filas[-mid:] if clave in _LISTAS_CRONOLOGICAS else filas[:mid]
                aviso = {"campo": clave, "mostrados": mid, "total": total}
                if clave in _LISTAS_CRONOLOGICAS:
                    aviso["se_muestran"] = "los más recientes"
                prueba = {**resultado, clave: parte, "truncado": aviso}
                t = json.dumps(prueba, ensure_ascii=False)
                if len(t) <= tope:
                    mejor, lo = t, mid + 1
                else:
                    hi = mid - 1
            if mejor:
                return mejor

    return texto[:tope] + '\n[RESULTADO RECORTADO: el resto no entró y no está en estos datos]'


# Ventana del resumen semanal, en días hacia atrás desde hoy — lo que cuenta
# como "esta semana" para agenda de sesiones y el corte estricto del informe.
RESUMEN_DIAS = 7

# Ventana más amplia, SOLO para proyectos de ley: un proyecto presentado hace
# 10-13 días (fuera de la semana estricta) casi siempre sigue en su mismo
# estado inicial ("PRESENTADO", sin resolver) — mostrarlo como contexto
# vigente es de bajo riesgo. No se aplica a interpelaciones/noticias: esas sí
# pueden quedar obsoletas (resueltas, archivadas) en ese mismo lapso sin forma
# de saberlo desde los datos, así que ahí se mantiene el corte estricto de 7
# días (ver REGLA DE FECHA en resumen.md).
RESUMEN_CONTEXTO_DIAS = 15

# Herramientas fijas del resumen semanal — no se le deja la elección al
# router. Dejado a su criterio con tool_choice="required" terminaba llamando
# 7-8 herramientas de golpe (fetch_interpelaciones, fetch_comisiones,
# fetch_comisiones, buscar_agenda...) y reventaba el límite del proveedor,
# tumbando la función entera. Pasó con Groq y su TPM de 6k; con Gemini el techo
# es de requests por día, así que 7-8 llamadas por resumen queman la cuota
# igual de rápido y la lista fija sigue haciendo falta.
#
# fetch_interpelaciones sí se agregó a la lista fija: es la fuente de la
# noticia política más caliente de la semana (mociones contra ministros) y
# sin ella el resumen puede terminar hablando solo de trámite institucional
# mientras ignora lo más relevante — pasó en producción. Las otras 3
# (fetch_comisiones y buscar_agenda) siguen afuera: no aportan
# nada nuevo que buscar_destacados/fetch_agenda_pleno no cubran ya para un
# resumen semanal, así que agregarlas sería puro peso extra sin beneficio.
RESUMEN_TOOLS = (
    ("buscar_proyectos",       {"dias": RESUMEN_CONTEXTO_DIAS}),
    ("buscar_destacados",      {}),
    ("fetch_agenda_pleno",     {}),
    ("fetch_agenda_camaras",   {"dias": RESUMEN_DIAS}),
    ("fetch_interpelaciones",  {}),
)

# Marcadores que indican que ya se mostró un expediente completo en el hilo.
EXPEDIENTE_MARKERS = (
    "FICHA DEL PROYECTO",
    "COMISIONES A LAS QUE FUE DERIVADO",
    "ACTOS DE TRABAJO POR COMISIÓN",
    "MI LECTURA",
)

PALABRAS_ANALISIS = (
    "analiza", "analizar", "resume", "resumir", "resumen", "fórmula legal",
    "formula legal", "qué propone", "que propone", "qué modifica", "que modifica",
    "artículo", "articulo", "deroga", "explica",
)

YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|live/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)

# "proyectos de ley de los últimos N días" / "novedades de proyectos de
# ley..." — override determinístico del router. Probado en vivo, dos veces:
# el router (8B) es no determinístico para pedidos de "proyectos de ley
# recientes" — a veces sí llama a buscar_proyectos, a veces no llama a NADA
# y el modelo grande termina inventando una tabla completa de proyectos
# falsos (números, títulos, fechas, todo). La regla anti-invención del
# prompt ayuda pero no es 100% confiable — pasó en dos rondas de prueba
# distintas, con dos frases distintas ("...últimos 15 días..." y
# "...novedades de proyectos de ley en DIPUTADOS..."). Por eso la detección
# cubre ambos casos: un número explícito de días, o una palabra que implica
# "reciente" sin número — en ese caso se usa una ventana por defecto.
# No intenta preservar materia/autor del pedido original: se prioriza no
# inventar por sobre la precisión del filtro (SPLEY igual no filtra bien por
# materia — ver comentario en _fetch_spley_por_materia en scraper.py).
DEFAULT_DIAS_PROYECTOS = 15

# Un listado suelto muestra como mucho 15 filas (workflow_proyectos.md). Si le
# llegaban 100, el modelo mostraba 10-15 y decía "te muestro las 100". Solo
# cuando piden agrupar, contar o desglosar necesita el lote entero.
MAX_FILAS_LISTADO = 15
# Un cuadro agrupado necesita el lote entero. El router pedía limit=100 y en
# 15 días había 134 (15/10/2026): los encabezados sumaban 134, las tablas
# mostraban 91 y la nota decía "las primeras 100". El techo es de SPLEY, no
# del cuadro: 500 cubre de sobra una quincena.
MAX_FILAS_AGRUPADO = 500
# Lo único que usa la tabla compacta del cuadro agrupado
# (workflow_proyectos.md). Sin autores ni proponente, 134 proposiciones ocupan
# ~45 mil caracteres en vez de 76 mil, y entran sin recortar filas.
CAMPOS_AGRUPADO = ("numero", "fecha_presentacion", "titulo", "camara", "enlace")
AGRUPADO_RE = re.compile(
    r"\b(agrup\w*|cuadro|temas|clasific\w*|desglos\w*|resumen|bancadas?|"
    r"cu[aá]nt[oa]s|cantidad|total|todos|todas)\b", re.IGNORECASE)
DIAS_PROYECTOS_RE = re.compile(r"(\d+)\s*d[ií]as", re.IGNORECASE)
RECIENTE_RE = re.compile(r"\b(novedades|reciente|recientes|últim[oa]s?)\b", re.IGNORECASE)


# ── Atajo conversacional: saltear el router ────────────────────────────────
#
# prompts/router.md dice "usa SIEMPRE responder_directo" para saludos, cortesía
# y preguntas sobre el propio asistente. Medido contra Gemini: preguntarle eso
# al router cuesta un viaje de red completo que termina, sin excepción, en
# responder_directo — el 70% de la latencia de un "hola" se iba ahí. Cuando la
# respuesta del router es previsible con certeza, se la saltea.
#
# El criterio es deliberadamente estrecho: la regex ancla el mensaje ENTERO
# (^...$), así que "gracias, ahora dame los proyectos de ley" no matchea y sigue
# yendo al router. Ante cualquier duda se paga el router — equivocarse para el
# otro lado significa que el modelo grande responde sin datos, y este código ya
# tiene historial de inventar tablas de proyectos falsos cuando le falta la
# herramienta (ver _detecta_proyectos_por_dias).
#
# Los afirmativos sueltos ("sí", "dale", "ok") quedan AFUERA a propósito: si el
# turno anterior fue "¿quieres que busque los proyectos?", ese "sí" sí necesita
# herramienta y el router es quien tiene el contexto para verlo.
CONVERSACIONAL_RE = re.compile(
    r"^[\s¡¿]*(?:"
    r"hola|holis?|buenas|buen[oa]s?\s+(?:d[ií]as?|tardes|noches)|"
    r"qu[eé]\s+tal|c[oó]mo\s+(?:est[aá]s|va|andas|and[aá]s)|"
    r"gracias|muchas\s+gracias|mil\s+gracias|te\s+agradezco|"
    r"chau|adi[oó]s|hasta\s+luego|nos\s+vemos|"
    r"ja(?:ja)+|je(?:je)+|"
    r"qui[eé]n\s+(?:sos|eres)|c[oó]mo\s+te\s+llam[aá]s|"
    r"qu[eé]\s+(?:pod[eé]s|puedes|sab[eé]s|sabes)\s+hacer|"
    r"para\s+qu[eé]\s+(?:serv[ií]s|sirves)|"
    r"en\s+qu[eé]\s+(?:me\s+)?(?:pod[eé]s|puedes)\s+ayudar"
    r")[\s!¡?¿.,…\-]*$",
    re.IGNORECASE,
)

# Red de seguridad: si aparece cualquier término del dominio, va al router
# aunque la frase haya matcheado arriba.
DOMINIO_RE = re.compile(
    r"\b(proyecto|ley|leyes|comisi[oó]n|comisiones|congreso|senado|diputad|"
    r"sesi[oó]n|sesiones|pleno|dictamen|dictamenes|dict[aá]menes|citaci[oó]n|"
    r"moci[oó]n|votaci[oó]n|expediente|congresista|ministr|interpelaci[oó]n|"
    r"decreto|norma|agenda|pdf|documento)",
    re.IGNORECASE,
)


def _es_conversacional(texto: str) -> bool:
    """True solo si el mensaje entero es saludo, cortesía o meta-pregunta."""
    t = (texto or "").strip()
    if not t or len(t) > 80:
        return False
    if DOMINIO_RE.search(t):
        return False
    return bool(CONVERSACIONAL_RE.match(t))


def _detecta_proyectos_por_dias(texto: str) -> int | None:
    t = texto.lower()
    if "proyecto" not in t or "ley" not in t:
        return None
    m = DIAS_PROYECTOS_RE.search(t)
    if m:
        return int(m.group(1))
    if RECIENTE_RE.search(t):
        return DEFAULT_DIAS_PROYECTOS
    return None


# ── Corrección de nombres de congresistas en la respuesta final ────────────
#
# scraper.py normaliza los nombres de firmantes/autores a "Nombre Apellido"
# (ver _nombre_a_formato_natural y _normalizar_lista_autores) porque SPLEY
# los devuelve mezclados: unas veces "Apellido, Nombre", otras ya en orden
# natural, según qué endpoint respondió. Verificado en vivo (04/08/2026,
# probado con 3 variantes de instrucción distintas, incluyendo un ejemplo
# negativo explícito) que el modelo principal reescribe el nombre normalizado
# de vuelta a "Apellido, Nombre" en su respuesta pese a que el dato ya le
# llega limpio — es un estilo tan dominante en los documentos del Congreso
# que ninguna instrucción de prompt lo revirtió. Se corrige acá con un swap
# determinístico: solo se tocan los nombres que efectivamente trajeron las
# herramientas en este turno, nunca un patrón genérico de "Apellido, Nombre"
# que podría coincidir con texto no relacionado a personas.
_CAMPOS_NOMBRE = {"autor", "autor_principal", "coautores", "autores"}


def _extraer_nombres_normalizados(tool_msgs: list[dict]) -> set[str]:
    nombres: set[str] = set()

    def _recolectar(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in _CAMPOS_NOMBRE and isinstance(v, str) and v:
                    for parte in v.split(";"):
                        parte = parte.strip()
                        if parte and "," not in parte and " " in parte:
                            nombres.add(parte)
                else:
                    _recolectar(v)
        elif isinstance(obj, list):
            for item in obj:
                _recolectar(item)

    for msg in tool_msgs:
        contenido = msg.get("content", "")
        idx = contenido.find("{")
        if idx == -1:
            continue
        try:
            _recolectar(json.loads(contenido[idx:]))
        except (json.JSONDecodeError, ValueError):
            continue
    return nombres


def _fix_nombres_reformateados(texto: str, nombres_normalizados: set[str]) -> str:
    for nombre in nombres_normalizados:
        partes = nombre.split()
        if len(partes) < 2:
            continue
        for corte in range(1, len(partes)):
            variante = f"{' '.join(partes[corte:])}, {' '.join(partes[:corte])}"
            if variante in texto:
                texto = texto.replace(variante, nombre)
    return texto


# ── Verificación de citas ──────────────────────────────────────────────────
#
# El modelo a veces cita números de proyecto o enlaces que no estaban en los
# datos (pasó con proyectos "de salud mental" que no existían). La respuesta
# ya salió en streaming, así que no se puede borrar: se agrega una advertencia
# al final nombrando exactamente qué no se pudo verificar.
# El guion entre los dos años es opcional: el Congreso a veces publica
# "00088-20262031-CD" y el modelo lo escribe bien, "00088-2026-2031-CD".
# Sufijo de 1 o 2 letras: "-S" es el del Senado, y la agenda del 15/10/2026
# escribía "00098-2026-2031-d" para uno de Diputados. Con [A-Z]{2} esos no se
# reconocían en las fuentes y la respuesta cerraba con un falso "no pude
# verificar 00098-2026-2031-CD".
NUM_PROYECTO_RE = re.compile(r"\b(\d{1,5})\s*[-/]\s*(\d{4})(?:\s*-?\s*\d{4})?\s*-\s*([A-Za-z]{1,2})\b")
# "D" suelto es la Cámara de Diputados mal escrita: su sufijo es "CD".
_SUFIJOS_EQUIVALENTES = {"D": "CD"}
# El host solo tiene que parecerse al del Congreso ("congr"): un link con el
# dominio mal escrito ("wb2server.congrosgob.pe", visto el 15/10/2026 en el
# resumen semanal) no está en los datos y también se desenlaza.
LINK_CONGRESO_RE = re.compile(r"https?://[^\s)\]>\"'`/]*congr[^\s)\]>\"'`]*")


# Portales oficiales que el modelo puede sugerir aunque no vengan en los datos
# (verificados el 13/10/2026: responden 200). Cualquier otro link del
# Congreso que no esté en lo consultado se muestra como texto: Mistral
# inventaba "spley.congreso.gob.pe" (no existe) y "congreso.gob.pe/spley/" (404).
PORTALES_OFICIALES = (
    "https://wb2server.congreso.gob.pe/spley-portal/",
    "https://wb2server.congreso.gob.pe/smociones-portal/",
    "https://comunicaciones.congreso.gob.pe/agenda/",
    "https://diputados.congreso.gob.pe/sesiones-del-pleno/",
    "https://senado.congreso.gob.pe/sesiones_del_pleno/",
    "https://www.congreso.gob.pe/",
    "https://diputados.congreso.gob.pe/",
    "https://senado.congreso.gob.pe/",
)

_LINK_MD_CONGRESO_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s/]*congr[^)\s]*)\)")


def _norm_link(u: str) -> str:
    return u.rstrip(".,;:").rstrip("/")


def _links_permitidos(fuentes: str) -> set[str]:
    return ({_norm_link(u) for u in LINK_CONGRESO_RE.findall(fuentes)}
            | {_norm_link(u) for u in PORTALES_OFICIALES})


_EXPEDIENTE_NUM_RE = re.compile(r"/expediente/\d{4}/0*(\d+)")
_MENCION_PROYECTO_RE = re.compile(
    r"(?:\bPL|Proyecto de Ley|Proposici[oó]n Legislativa|Moci[oó]n)\s*(?:N\.?\s*[°º]?\s*)?0*(\d{1,5})\b",
    re.IGNORECASE)


def _numeros_mencionados(linea: str) -> set[int]:
    nums = {int(m.group(1)) for m in _MENCION_PROYECTO_RE.finditer(linea)}
    nums |= {int(m.group(1)) for m in NUM_PROYECTO_RE.finditer(linea)}
    return nums


def _desenlazar(texto: str, permitidos: set[str]) -> str:
    """
    Quita los links del Congreso que no están en los datos ni son portales
    oficiales, y los de expediente que apuntan a otro proyecto que el que
    nombra la línea (visto: "PL 98 ... [Ver proyecto](.../expediente/2026/571)").
    """
    return "".join(_desenlazar_linea(linea, permitidos)
                   for linea in texto.splitlines(keepends=True))


def _desenlazar_linea(texto: str, permitidos: set[str]) -> str:
    mencionados = _numeros_mencionados(_LINK_MD_CONGRESO_RE.sub(r"\1", texto))

    def md(m):
        if _norm_link(m.group(2)) not in permitidos:
            return m.group(1)
        exp = _EXPEDIENTE_NUM_RE.search(m.group(2))
        if exp and mencionados and int(exp.group(1)) not in mencionados:
            return m.group(1)
        return m.group(0)

    def suelto(m):
        return m.group(0) if _norm_link(m.group(0)) in permitidos else ""

    texto = _LINK_MD_CONGRESO_RE.sub(md, texto)
    # Los que quedan son sueltos (los de un [texto](url) válido ya pasaron).
    partes = re.split(r"(\[[^\]\n]+\]\([^)\s]+\))", texto)
    return "".join(pt if i % 2 else LINK_CONGRESO_RE.sub(suelto, pt)
                   for i, pt in enumerate(partes))


# Razonamiento del modelo que se cuela como respuesta (Gemini por el shim de
# OpenAI): empieza hablando del pedido en tercera persona y termina con un
# "Redacto la respuesta..." pegado al texto real.
_INICIO_RAZONAMIENTO_RE = re.compile(
    r"^\s*(?:El usuario (?:pide|quiere|pregunta|solicita)|Reviso (?:los|el) (?:datos|resultado)|"
    r"Analizo|La instrucción dice|Debo (?:responder|revisar))", re.IGNORECASE)
_FIN_RAZONAMIENTO_RE = re.compile(
    r"(?:Redacto|Escribo|Preparo|Armo|Procedo a (?:redactar|escribir))[^.\n]*\.\s*")


def _quitar_razonamiento(texto: str) -> str:
    """Saca el razonamiento filtrado al comienzo de una respuesta, si lo hay."""
    if not _INICIO_RAZONAMIENTO_RE.match(texto):
        return texto
    finales = list(_FIN_RAZONAMIENTO_RE.finditer(texto))
    if finales:
        return texto[finales[-1].end():].lstrip()
    m = re.search(r"^(?:#|\||\*\*)", texto, re.MULTILINE)
    return texto[m.start():] if m else texto


def _clave_proyecto(m: re.Match) -> tuple:
    # "00434-2026-2031-CD" y "434-2026-2031-CD" son el mismo proyecto.
    sufijo = m.group(3).upper()
    return int(m.group(1)), m.group(2), _SUFIJOS_EQUIVALENTES.get(sufijo, sufijo)


def _citas_no_verificadas(respuesta: str, fuentes: str) -> tuple[list[str], list[str]]:
    """Números de proyecto y enlaces del Congreso que están en la respuesta pero no en las fuentes."""
    nums_ok = {_clave_proyecto(m) for m in NUM_PROYECTO_RE.finditer(fuentes)}
    links_ok = _links_permitidos(fuentes)
    nums, links = [], []
    for m in NUM_PROYECTO_RE.finditer(respuesta):
        if _clave_proyecto(m) not in nums_ok and m.group(0) not in nums:
            nums.append(m.group(0))
    for u in LINK_CONGRESO_RE.findall(respuesta):
        u = u.rstrip(".,;:")
        if _norm_link(u) not in links_ok and u not in links:
            links.append(u)
    return nums, links


class ChatOrchestrator:
    """Ejecuta una vuelta completa de conversación sobre el historial dado."""

    def __init__(self, messages: list):
        self.messages = messages or []
        ahora = ahora_lima()
        self.hoy = ahora.strftime("%d/%m/%Y")
        # Ventana del resumen semanal: los 7 días que terminan hoy.
        self.desde = (ahora - timedelta(days=RESUMEN_DIAS)).strftime("%d/%m/%Y")
        self.system_base = system_con_fecha(self.hoy)

        # Estado que rellena _analyze()
        self.last_msg = ""
        self.is_resumen = False
        self.forzar_dias_proyectos: int | None = None
        self.proyectos_agrupados = False
        self.proyectos_items: list[dict] = []
        self.proyectos_total: int | None = None
        self.sector = None
        self.analizar_documento = False
        self.has_sesion = False
        self.has_expediente_en_contexto = False
        self.doc_en_contexto = False
        self.es_conversacional = False
        self.conversation: list = []

        # Estado que rellenan las fases 2 y 3
        self.tool_msgs: list = []
        self.tools_usados: list = []
        self.solo_responder_directo = False

    # ── Análisis del pedido ──────────────────────────────────────────────────

    def _analyze(self):
        """Clasifica el pedido para decidir el camino y armar el historial."""
        msgs = self.messages
        self.last_msg = msgs[-1].get("content", "") if msgs else ""
        low_last = self.last_msg.lower()

        # Resumen semanal: el frontend lo marca con un centinela.
        self.is_resumen = self.last_msg.strip().startswith("__RESUMEN_SEMANAL__")
        if self.is_resumen and ":" in self.last_msg:
            self.sector = self.last_msg.strip().split(":", 1)[1].strip()

        # "proyectos de ley de los últimos N días" — no is_resumen (que ya
        # tiene su propio set fijo de herramientas).
        if not self.is_resumen:
            self.forzar_dias_proyectos = _detecta_proyectos_por_dias(self.last_msg)

        # ¿Hay un PDF cargado? El frontend lo inyecta como mensaje de usuario.
        recientes = msgs[-6:]
        self.doc_en_contexto = any(
            "He cargado el documento" in (m.get("content", "") or "")
            for m in recientes if m.get("role") == "user"
        )
        # Páginas del documento completo que responden a esta pregunta (el
        # contexto inicial solo trae el comienzo de un PDF largo).
        self.paginas_doc = None
        if self.doc_en_contexto:
            self.paginas_doc = self._paginas_del_documento(recientes)
        pide_analisis = any(w in low_last for w in PALABRAS_ANALISIS)
        # Analizar un doc ya cargado se resuelve con el texto en contexto:
        # no hace falta ninguna herramienta de scraping.
        self.analizar_documento = self.doc_en_contexto and (
            pide_analisis or len(self.last_msg) < 60
        )

        self.has_sesion = (
            "youtube.com" in low_last or "youtu.be" in low_last
            or "transcript" in low_last or "[sesión" in low_last
        )

        # Saludo/cortesía puro: se saltea el router (ver CONVERSACIONAL_RE).
        # No aplica al resumen ni al override de proyectos, que ya tienen sus
        # propias herramientas fijas y ni pasan por Fase 1.
        self.es_conversacional = (
            not self.is_resumen
            and self.forzar_dias_proyectos is None
            and _es_conversacional(self.last_msg)
        )

        recent_assistant = " ".join(
            m.get("content", "") for m in msgs[-6:] if m.get("role") == "assistant"
        )
        self.has_expediente_en_contexto = any(
            marker in recent_assistant for marker in EXPEDIENTE_MARKERS
        )

        if self.is_resumen:
            base = (
                "Genera el resumen ejecutivo semanal completo del Congreso del Perú "
                f"cubriendo únicamente del {self.desde} al {self.hoy} "
                f"(últimos {RESUMEN_DIAS} días)."
            )
            if self.sector and self.sector != "general":
                base += (
                    f" Enfoca el análisis especialmente en el sector {self.sector} y los "
                    "proyectos de ley, noticias y agenda que impacten a ese sector."
                )
            self.conversation = [{"role": "user", "content": base}]
        else:
            self.conversation = msgs[-20:]

    def _paginas_del_documento(self, recientes: list) -> dict | None:
        m = None
        for msg in reversed(recientes):
            if msg.get("role") == "user":
                m = DOC_ID_RE.search(msg.get("content", "") or "")
                if m:
                    break
        if not m:
            return None
        try:
            paginas = documentos.paginas_relevantes(m.group(1), self.last_msg)
        except Exception as e:
            logger.warning("no se pudieron recuperar páginas del documento %s: %s", m.group(1), e)
            return None
        if not paginas:
            return None
        texto = "\n\n".join(f"[Página {n}]\n{t}" for n, t in paginas)
        return {"role": "user", "content": (
            "[Páginas del documento cargado que corresponden a la próxima pregunta — texto "
            "completo de esas páginas, con su número de página física. Responde con esto y "
            "cita las páginas.]\n" + texto)}

    def _con_paginas(self, msgs: list) -> list:
        """Inserta las páginas recuperadas justo antes de la última pregunta."""
        if not self.paginas_doc or not msgs:
            return msgs
        return msgs[:-1] + [self.paginas_doc] + msgs[-1:]

    @property
    def _short_circuit(self) -> bool:
        """Analizar doc o sesión no requiere Fase 1 ni 2."""
        return self.analizar_documento or (self.has_sesion and not self.is_resumen)

    # ── Atajo: documento o sesión en contexto ────────────────────────────────

    async def _run_short_circuit(self):
        system_p3 = self.system_base
        if self.analizar_documento:
            system_p3 += "\n" + WORKFLOW_PDF_FORMULA
        if self.has_sesion:
            system_p3 += "\n" + WORKFLOW_SESION
            ok = True
            async for ev in self._inject_transcript():
                if ev is None:          # centinela: sin transcript, ya se avisó
                    ok = False
                    break
                yield ev
            if not ok:
                return

        msgs = self._con_paginas([{"role": "system", "content": system_p3}] + self.conversation)
        async for ev in self._stream_final(msgs, max_tokens=2048):
            yield ev

    async def _inject_transcript(self):
        """
        Busca el transcript del video de YouTube del último mensaje y lo mete
        en la conversación. Emite None si no hay transcript disponible.
        """
        m = YT_ID_RE.search(self.last_msg)
        if not m:
            return

        yield sse.status("Obteniendo transcript de la sesión...")
        try:
            data = await fetch_transcript_youtube(m.group(1))
        except Exception as e:
            logger.warning("fetch_transcript_youtube falló: %s", e)
            data = None

        if data and data.get("ok") and data.get("text"):
            transcript_msg = {
                "role": "user",
                "content": (
                    f"[TRANSCRIPT DE LA SESIÓN — fuente: {data.get('source', 'youtube')}]\n"
                    f"{data['text']}\n"
                    "[FIN DEL TRANSCRIPT]"
                ),
            }
            self.conversation = (
                self.conversation[:-1] + [transcript_msg, self.conversation[-1]]
            )
            return

        # Sin subtítulos: responder sin gastar una llamada al modelo.
        yield sse.text(
            "No pude obtener el transcript de ese video (sin subtítulos disponibles "
            "o video privado). Para analizar la sesión puedes: (1) cargar el PDF del "
            "acta con el botón de adjunto, o (2) pegar el texto del transcript "
            "directamente en el chat."
        )
        yield sse.DONE
        yield None

    # ── Fase 1: elección de herramienta ──────────────────────────────────────

    def _router_messages(self) -> list:
        """
        Historial que se manda al router. Se recorta agresivamente: un PDF o un
        expediente en contexto son miles de tokens que no aportan nada a la
        elección de herramienta y revientan el TPM ya en la Fase 1.
        """
        # is_resumen ni pasa por acá — run() lo desvía a _phase2_resumen()
        # directo, sin Fase 1 (ver RESUMEN_TOOLS).
        if self.doc_en_contexto:
            return [{"role": "system", "content": ROUTER_PROMPT},
                    {"role": "user", "content": self.last_msg}]

        if self.has_expediente_en_contexto:
            ctx = (
                "[CONTEXTO: El asistente ya mostró el expediente completo de un proyecto "
                "en esta conversación (FICHA DEL PROYECTO, SEGUIMIENTO, COMISIONES, etc.). "
                "Si el usuario pregunta sobre ese expediente — comisiones, actos, predictamen, "
                "adjuntos, estado, autores — usa responder_directo. "
                "Solo llama fetch_expediente si pide OTRO proyecto diferente.]\n\n"
                f"Pregunta: {self.last_msg}"
            )
            return [{"role": "system", "content": ROUTER_PROMPT},
                    {"role": "user", "content": ctx}]

        return [{"role": "system", "content": ROUTER_PROMPT}] + self.messages[-4:]

    async def _phase1(self):
        """Devuelve el choice del router, o None si ya se emitió una respuesta."""
        try:
            return await llm.complete_router(self._router_messages(), tools=TOOLS)
        except Exception as e:
            logger.warning("Fase 1 (router) falló: %s", e)
            raise

    async def _phase1_fallback(self, exc):
        """
        El router falló. Si fue por un tool_call malformado, respondemos igual
        con el modelo grande y sin herramientas; si no, mostramos el error.
        """
        if not llm.is_tool_format_error(exc):
            yield sse.error(llm.friendly_error(exc))
            return

        msgs = [{"role": "system", "content": self.system_base}] + self.conversation
        async for ev in self._stream_final(msgs, max_tokens=2048):
            yield ev

    # ── Fase 2: ejecución de herramientas ────────────────────────────────────

    @staticmethod
    def _clean_args(raw: str) -> dict:
        try:
            args = json.loads(raw or "{}")
        except (ValueError, TypeError):
            return {}
        if not isinstance(args, dict):
            return {}
        if "limit" in args:
            try:
                args["limit"] = int(args["limit"])
            except (ValueError, TypeError):
                args["limit"] = 20
        return {k: v for k, v in args.items() if v != ""}

    # Herramientas donde repetir la llamada en el mismo turno nunca agrega
    # cobertura real — con el parámetro por defecto (sin camara) ya traen
    # todo. El router a veces llama esto una vez por cada cámara que el
    # usuario nombra (ej. "Senado y Diputados"), pensando que hace falta una
    # llamada por cámara — confirmado en vivo con fetch_comisiones, dos veces
    # seguidas en pruebas distintas, incluso después de aclarar la descripción
    # de la herramienta (el fix de prompt solo no alcanzó).
    TOOLS_UNA_SOLA_VEZ = {"fetch_comisiones"}

    @classmethod
    def _deduplicar_tool_calls(cls, real_calls: list) -> list:
        """
        Si una herramienta de TOOLS_UNA_SOLA_VEZ aparece más de una vez, se
        queda solo con la primera — las demás no aportan nada. La llamada que
        sobrevive se fuerza a args vacíos: si el router la mandó con
        camara='senado', quedarse con esos args tal cual perdería los enlaces
        de Diputados (fetch_comisiones con una cámara puntual NO incluye la
        otra — solo camara=None trae ambas). Sin argumentos garantiza la
        cobertura completa sin importar qué haya mandado el router.
        """
        vistas = set()
        resultado = []
        for tc in real_calls:
            name = tc.function.name
            if name in cls.TOOLS_UNA_SOLA_VEZ:
                if name in vistas:
                    continue
                vistas.add(name)
                tc = SimpleNamespace(id=tc.id, function=SimpleNamespace(name=name, arguments="{}"))
            resultado.append(tc)
        return resultado

    async def _phase2(self, choice):
        """Ejecuta las tool calls del router, emitiendo el estado de cada una."""
        if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
            return

        # responder_directo no es una herramienta real: es la señal de
        # "contesta con tu conocimiento, sin datos externos".
        real_calls = [tc for tc in choice.message.tool_calls
                      if tc.function.name != "responder_directo"]
        real_calls = self._deduplicar_tool_calls(real_calls)
        if not real_calls:
            self.solo_responder_directo = True
            return

        # OJO: acá antes se armaba el intercambio nativo de function-calling de
        # OpenAI (mensaje "assistant" con tool_calls + mensaje "tool" por cada
        # resultado). Eso rompe con Gemini: exige un campo interno
        # ("thought_signature") atado a la respuesta original del modelo que
        # nosotros no tenemos cómo reconstruir a mano. Fase 3 nunca manda
        # `tools=` en su propio pedido (no es una llamada nativa real, ver
        # _stream_final) así que no hace falta ese formato — un mensaje de
        # texto plano con el resultado le sirve igual al modelo y funciona
        # con cualquier proveedor.
        llamadas = [(tc.function.name,
                     self._ajustar_args(tc.function.name, self._clean_args(tc.function.arguments)))
                    for tc in real_calls]
        async for ev in self._ejecutar_herramientas(llamadas):
            yield ev

    async def _phase2_tools_fijas(self, tools: tuple):
        """
        Corre una lista fija de herramientas directamente, sin pasar por el
        router — mismo formato de tool_calls que arma _phase2, pero con ids
        sintéticos en vez de los que devolvería el modelo. Usado cuando ya
        sabemos con certeza qué herramienta hace falta y no vale la pena
        (o es riesgoso) dejárselo a la elección del router de 8B — ver
        RESUMEN_TOOLS y _detecta_proyectos_por_dias.
        """
        llamadas = [(name, self._ajustar_args(name, dict(args)) if not self.is_resumen else args)
                    for name, args in tools]
        async for ev in self._ejecutar_herramientas(llamadas):
            yield ev

    async def _ejecutar_herramientas(self, llamadas: list):
        """
        Corre las herramientas EN PARALELO (hasta MAX_TOOLS_PARALELO a la vez)
        y agrega sus resultados a tool_msgs en el orden pedido.

        Antes iban una tras otra: el resumen semanal esperaba la suma de sus
        5 consultas al Congreso, cuando son independientes entre sí.

        Texto plano en vez del formato nativo de tool_calls (mensaje
        "assistant" + mensajes "tool"): rompe con Gemini, que exige un
        "thought_signature" atado a la respuesta original que no se puede
        reconstruir a mano. Fase 3 no manda `tools=`, así que no hace falta.
        """
        if not llamadas:
            return
        for name, _ in llamadas:
            self.tools_usados.append(name)
        etiquetas = list(dict.fromkeys(STATUS_LABELS.get(n, "Consultando el Congreso...")
                                       for n, _ in llamadas))
        yield sse.status(etiquetas[0] if len(etiquetas) == 1
                         else "Consultando en paralelo: " + " · ".join(e.rstrip(".") for e in etiquetas))

        sem = asyncio.Semaphore(MAX_TOOLS_PARALELO)

        async def una(name, args):
            async with sem:
                return await self._run_tool(name, args)

        resultados = await asyncio.gather(*(una(n, a) for n, a in llamadas))
        for (name, _), result in zip(llamadas, resultados):
            if name == "buscar_proyectos" and self.is_resumen:
                result = _separar_por_semana(result, self.desde)
            elif name == "buscar_proyectos" and self.proyectos_agrupados:
                result = _compactar_para_agrupado(result)
                if isinstance(result, dict) and isinstance(result.get("items"), list):
                    self.proyectos_items = result["items"]
                    self.proyectos_total = result.get("total_disponible") or result.get("total")
            self.tool_msgs.append({
                "role": "user",
                "content": f"[Resultado de la herramienta {name}]\n"
                           f"(Datos que consultó el sistema para responder la última pregunta; "
                           f"el usuario no los envió ni los ve.)\n{_recortar_resultado(name, result)}",
            })

    def _ajustar_args(self, name: str, args: dict) -> dict:
        if name != "buscar_proyectos":
            return args
        # El pedido decía "últimos N días": que el router no pierda la ventana.
        if self.forzar_dias_proyectos and "dias" not in args and "numero" not in args:
            args["dias"] = self.forzar_dias_proyectos
        if AGRUPADO_RE.search(self.last_msg):
            args["limit"] = MAX_FILAS_AGRUPADO
            self.proyectos_agrupados = True
        else:
            args["limit"] = min(int(args.get("limit") or MAX_FILAS_LISTADO), MAX_FILAS_LISTADO)
        return args

    @staticmethod
    async def _run_tool(name: str, args: dict) -> dict:
        """
        Ejecuta una herramienta y normaliza cualquier fallo a 'sin_datos'.

        Un reintento corto ante excepción: los servidores del Congreso tienen
        hipos transitorios (timeout puntual, conexión caída) que no vuelven a
        pasar un segundo después — confirmado en vivo con buscar_proyectos,
        que falló una vez y funcionó perfecto al tiro siguiente contra la
        misma consulta. Sin esto, un hipo de 1 request tumbaba toda la
        respuesta con un "no puedo buscar ahora mismo".
        """
        if name not in TOOL_MAP:
            logger.warning("El modelo pidió una herramienta inexistente: %s", name)
            return {"sin_datos": True, "mensaje": f"Herramienta '{name}' no disponible."}

        en_cache = tool_cache.get(name, args)
        if en_cache is not None:
            logger.info("herramienta %s desde caché args=%s", name, args)
            return en_cache

        last_exc = None
        result = None
        ok = False
        for intento in range(2):
            inicio = time.monotonic()
            try:
                with timed("herramienta", nombre=name, args=args, intento=intento + 1):
                    result = await asyncio.wait_for(TOOL_MAP[name](args), TOOL_TIMEOUT_SECONDS)
                ok = True
                break
            except Exception as e:
                last_exc = e
                # Si ya consumió el tope entero no se reintenta: duplicaba la
                # espera (2 × 60s) por una fuente que claramente no responde.
                agoto_el_tope = time.monotonic() - inicio >= TOOL_TIMEOUT_SECONDS * 0.95
                if intento == 0 and not agoto_el_tope:
                    logger.warning("Herramienta %s falló (intento 1/2), reintentando: %s", name, e)
                    await asyncio.sleep(1.5)
                    continue
                break

        if not ok:
            logger.error("Herramienta %s falló con args=%s: %s", name, args, last_exc)
            # "error", no "sin_datos": la fuente no respondió, que no es lo
            # mismo que "no hay resultados". Antes los dos casos llegaban
            # igual y el modelo afirmaba "no hay proyectos" cuando en
            # realidad SPLEY estaba caído.
            tipo = "timeout" if isinstance(last_exc, TimeoutError) else type(last_exc).__name__
            return {"error": "no_se_pudo_consultar",
                    "mensaje": (f"La fuente oficial no respondió ({tipo}). No se sabe si hay "
                                "datos: decile al usuario que no se pudo consultar y que reintente.")}

        if isinstance(result, dict) and "error" in result:
            return {"sin_datos": True,
                    "mensaje": result.get("error", "No hay información disponible.")}
        if not (isinstance(result, dict) and result.get("sin_datos")):
            result = tool_cache.put(name, args, result)
        return result

    # ── Fase 3: respuesta final ──────────────────────────────────────────────

    def _phase3_system(self) -> str:
        """Arma el system prompt: base compacta + solo los flujos que aplican."""
        if self.is_resumen:
            return resumen_con_fechas(self.hoy, self.desde)
        if self.solo_responder_directo or not self.tool_msgs:
            return self.system_base

        # Con tool results conviene SYSTEM_MINI (~800 tokens menos), salvo que
        # la herramienta tenga un workflow de formato propio que sí hace falta.
        needs_workflow = any(t in WORKFLOWS for t in self.tools_usados)
        if not needs_workflow:
            return SYSTEM_MINI.format(hoy=self.hoy)

        system_p3 = self.system_base
        for t in self.tools_usados:
            if t in WORKFLOWS:
                system_p3 += "\n" + WORKFLOWS[t]
        return system_p3

    def _phase3_max_tokens(self) -> int:
        if self.is_resumen:
            # Reporte multi-tema con links y agenda. Con 3500 se cortaba a
            # mitad de la agenda (visto 27/09/2026: terminaba en "**28/"):
            # en Gemini el razonamiento interno también descuenta de acá.
            # Es un techo, no un costo: solo se paga lo generado.
            return 12000
        if "fetch_expediente" in self.tools_usados:
            return 8000
        if self.tools_usados:
            # Proyectos/agenda: respuesta más corta, menos presión sobre el TPM.
            #
            # Salvo cuando la herramienta devolvió un listado grande. Una fila
            # de la tabla de proposiciones ronda los 70 tokens, así que un
            # "últimos 15 días" (89 proyectos) necesita más de 6000 y con 1800
            # la tabla se cortaba a la cuarta fila, en mitad de un título.
            # El presupuesto sigue el tamaño de lo que vino en vez de ser fijo:
            # así el caso grande entra entero sin inflar cada respuesta corta,
            # que son la mayoría.
            # 1800 cortaba la tabla a la mitad: 15 filas con título completo
            # y autores rondan 2500 tokens, y en Gemini el razonamiento
            # interno también descuenta de este presupuesto. Es un techo, no
            # un costo: solo se paga lo que se genera.
            chars = sum(len(m.get("content") or "") for m in self.tool_msgs)
            if self.proyectos_agrupados and chars > 30000:
                # Cuadro agrupado completo: 134 filas con link rondan 12 mil
                # tokens, más el razonamiento interno de Gemini.
                return 24000
            return 14000 if chars > 12000 else 8000
        return 2500

    async def _stream_final(self, msgs, max_tokens):
        """
        Streaming de Fase 3 con reintento sobre rate limit.

        Si este turno trajo nombres de congresistas de una herramienta, el
        texto se corrige (ver _fix_nombres_reformateados) LÍNEA POR LÍNEA: se
        emite cada línea apenas se completa. Antes se bufereaba la respuesta
        entera y el usuario no veía nada hasta el final — la línea base medía
        primer texto ≈ total (13.7s de 13.8s) en cada búsqueda de proyectos.
        Un nombre nunca cruza un salto de línea (cada fila de tabla trae el
        suyo), así que corregir por línea da el mismo resultado.
        """
        nombres = _extraer_nombres_normalizados(self.tool_msgs) if self.tool_msgs else set()
        permitidos = _links_permitidos(
            "\n".join(str(m.get("content") or "") for m in msgs + self.messages))

        def limpio(texto):
            texto = _desenlazar(texto, permitidos)
            return _fix_nombres_reformateados(texto, nombres) if nombres else texto

        pendiente = ""
        completo = ""
        # Mientras el comienzo parezca razonamiento filtrado, no se emite nada:
        # se decide al completarse la primera línea (o el final).
        revisando_inicio = True
        async for kind, payload in llm.stream(msgs, max_tokens=max_tokens):
            if kind == "text":
                # Siempre por línea: un link se limpia entero, nunca a medias.
                pendiente += payload
                if revisando_inicio:
                    if "\n" not in pendiente and len(pendiente) < 300:
                        continue
                    if _INICIO_RAZONAMIENTO_RE.match(pendiente):
                        continue  # se limpia completo al final
                    revisando_inicio = False
                corte = pendiente.rfind("\n")
                if corte < 0 and len(pendiente) > 1500:
                    # Párrafo larguísimo sin saltos: cortar en el último punto
                    # para no retener todo; un nombre no lleva ". " adentro.
                    corte = pendiente.rfind(". ")
                if corte >= 0:
                    listo, pendiente = pendiente[:corte + 1], pendiente[corte + 1:]
                    listo = limpio(listo)
                    completo += listo
                    yield sse.text(listo)
            elif kind == "status":
                yield sse.status(payload)
            elif kind == "error":
                if pendiente:
                    listo = limpio(pendiente)
                    completo += listo
                    yield sse.text(listo)
                yield sse.error(payload)
                return
        if pendiente:
            if revisando_inicio:
                pendiente = _quitar_razonamiento(pendiente)
            listo = limpio(pendiente)
            completo += listo
            yield sse.text(listo)
        aviso = self._verificar_citas(completo, msgs)
        if aviso:
            yield sse.text(aviso)
        yield sse.DONE

    def _verificar_citas(self, respuesta: str, msgs: list) -> str:
        # Fuentes: lo que el modelo tuvo delante (resultados de herramientas,
        # PDF o transcript cargado, turnos anteriores ya verificados).
        fuentes = "\n".join(str(m.get("content") or "") for m in msgs + self.messages)
        nums, links = _citas_no_verificadas(respuesta, fuentes)
        if not nums and not links:
            return ""
        logger.warning("Citas no verificadas en la respuesta: proyectos=%s enlaces=%s "
                       "(herramientas: %s)", nums, links, self.tools_usados)
        partes = []
        if nums:
            partes.append("números de proyecto " + ", ".join(nums))
        if links:
            partes.append("enlaces " + ", ".join(links))
        return ("\n\n---\n**Ojo:** no pude verificar " + " y ".join(partes) +
                " contra los datos consultados al Congreso. Pueden estar mal: "
                "confírmalos en el portal SPLEY antes de usarlos.")

    # ── Punto de entrada ─────────────────────────────────────────────────────

    async def run(self):
        """
        Generador de eventos SSE para una vuelta completa de conversación.

        Cualquier excepción termina en un evento de error legible: sin esto el
        stream se cortaba a mitad y el frontend se quedaba sin saber qué pasó.
        """
        inicio = time.monotonic()
        primer_texto = None
        resultado = "ok"
        try:
            async for ev in self._run():
                if primer_texto is None and ev.startswith('data: {"text"'):
                    primer_texto = time.monotonic() - inicio
                yield ev
        except (GeneratorExit, asyncio.CancelledError):
            resultado = "cancelado"
            raise
        except Exception as e:
            resultado = "error"
            logger.exception("Chat falló (último mensaje: %.120r)", self.last_msg)
            yield sse.error(llm.friendly_error(e))
            yield sse.DONE
        finally:
            # Una línea por vuelta con lo que importa para la línea base de
            # rendimiento (scripts/medir_chat.py la lee del log): cuánto tardó
            # el usuario en ver texto y cuánto la respuesta entera.
            logger.info("CHAT_METRICA resultado=%s primer_texto=%s total=%.2fs herramientas=%s",
                        resultado, f"{primer_texto:.2f}s" if primer_texto is not None else "-",
                        time.monotonic() - inicio, ",".join(map(str, self.tools_usados)) or "-")

    async def _run(self):
        self._analyze()
        logger.info("Chat: %d mensajes, resumen=%s, proyectos_dias=%s, conversacional=%s, atajo=%s",
                    len(self.messages), self.is_resumen, self.forzar_dias_proyectos,
                    self.es_conversacional, self._short_circuit)

        if self._short_circuit:
            async for ev in self._run_short_circuit():
                yield ev
            return

        if self.is_resumen:
            # Herramientas fijas — nada de router acá (ver RESUMEN_TOOLS).
            async for ev in self._phase2_tools_fijas(RESUMEN_TOOLS):
                yield ev
        elif self.forzar_dias_proyectos is not None:
            # "proyectos de ley de los últimos N días". Primero el router,
            # porque sabe extraer el tema ("de SALUD de los últimos 15 días");
            # forzar la búsqueda de entrada descartaba el tema. Pero el router
            # a veces no busca nada y el modelo inventaba una tabla entera
            # (ver _detecta_proyectos_por_dias): en ese caso se fuerza.
            choice = None
            try:
                choice = await self._phase1()
            except Exception as e:
                logger.warning("Router falló en pedido de proyectos por días, se fuerza: %s", e)
            llamadas = (choice.message.tool_calls or []) if choice is not None else []
            if any(tc.function.name == "buscar_proyectos" for tc in llamadas):
                async for ev in self._phase2(choice):
                    yield ev
            else:
                tools = (("buscar_proyectos", {"dias": self.forzar_dias_proyectos}),)
                async for ev in self._phase2_tools_fijas(tools):
                    yield ev
        elif self.es_conversacional:
            # Saludo o cortesía: el router siempre contesta responder_directo
            # acá, así que se ahorra ese viaje de red entero (ver
            # CONVERSACIONAL_RE). Fase 3 queda igual que si hubiera pasado por
            # el router: mismo system prompt, mismo historial, sin tool_msgs.
            logger.info("Atajo conversacional: se saltea el router")
            self.solo_responder_directo = True
        else:
            try:
                choice = await self._phase1()
            except Exception as e:
                async for ev in self._phase1_fallback(e):
                    yield ev
                return

            async for ev in self._phase2(choice):
                yield ev

        # Con tool results el contexto ya viene del resultado; mandar 20 mensajes
        # extra dispara el TPM. Con herramientas: solo los últimos 4.
        conv_p3 = self.messages[-4:] if self.tool_msgs else self.conversation
        # Los datos van ANTES de la pregunta: puestos al final, el último
        # mensaje "del usuario" era un bloque de datos y el modelo respondía
        # como si el usuario se los hubiera traído ("los que acabas de traer"),
        # o arrancaba de cero con un saludo a mitad de la conversación.
        msgs = self._con_paginas(
            [{"role": "system", "content": self._phase3_system()}]
            + conv_p3[:-1]
            + self.tool_msgs
            + conv_p3[-1:]
        )

        if self._toca_cuadro_por_temas():
            yield sse.status(f"Clasificando {len(self.proyectos_items)} proposiciones por tema...")
            cuadro = await self._armar_cuadro_por_temas()
            if cuadro:
                yield sse.text(cuadro)
                yield sse.DONE
                return

        async for ev in self._stream_final(msgs, self._phase3_max_tokens()):
            yield ev

    def _toca_cuadro_por_temas(self) -> bool:
        """
        Cuadro de proposiciones "por temas" con más filas que un listado.

        Con 134 proposiciones (15/10/2026) el modelo armaba las tablas él
        mismo y perdía la cuenta: 4 sin poner, 1 repetida, encabezados que no
        coincidían con sus filas. Ahora el modelo solo clasifica y el código
        arma las tablas y los conteos.
        """
        return (self.proyectos_agrupados and not self.is_resumen
                and len(self.proyectos_items) > MAX_FILAS_LISTADO
                and bool(POR_TEMAS_RE.search(self.last_msg)))

    async def _armar_cuadro_por_temas(self) -> str | None:
        items = [p for p in self.proyectos_items if p.get("numero")]
        numeros = [p["numero"] for p in items]
        lista = "\n".join(f"{p['numero']} | {p.get('titulo') or ''}" for p in items)
        msgs = [{"role": "system", "content": PROMPT_CLASIFICAR},
                {"role": "user", "content": f"Pedido del usuario: {self.last_msg}\n\n{lista}"}]
        texto = ""
        try:
            async for kind, payload in llm.stream(msgs, max_tokens=8000, temperature=0.1):
                if kind == "text":
                    texto += payload
                elif kind == "error":
                    logger.warning("clasificación por temas falló: %s", payload)
                    return None
        except Exception as e:
            logger.warning("clasificación por temas falló: %s", e)
            return None
        clasificacion = _parsear_clasificacion(texto, numeros)
        if clasificacion is None:
            logger.warning("clasificación por temas sin JSON válido; se usa la respuesta libre")
            return None
        grupos, lectura = clasificacion
        return _cuadro_por_temas(items, grupos, lectura, self.proyectos_total)
