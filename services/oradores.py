"""
Índice de oradores de una sesión: quién habla desde qué segundo.

La transcripción (Whisper) no separa voces: es texto corrido. Para responder
"¿qué dijo el congresista X?" no alcanza con buscar el nombre, porque a veces
lo nombran un minuto antes, o no lo nombran justo antes ("gracias,
congresista Pérez" llega DESPUÉS de que habló), o Whisper lo escribió mal.

Acá la IA lee la sesión por tramos de 15 min y anota cada cambio de orador
con la pista que lo delata en el propio texto: "tiene la palabra…", "gracias,
congresista X", alguien que se presenta, "por su intermedio, presidente…".
Cada intervención va desde su inicio hasta el inicio de la siguiente.

Contra inventos: solo se guarda un orador si alguna palabra de su nombre (o
su cargo, si no tiene nombre) aparece de verdad en el texto de ese tramo.

El índice se guarda en la base (tablas orador / orador_indice) y se arma una
sola vez por sesión; si la sesión sigue transcribiéndose, la próxima pregunta
lee solo lo nuevo.
"""
import asyncio
import difflib
import re
import unicodedata

from config import logger
from services import db, llm, placas
from services import transcripts as tr

BLOQUE_S = 15 * 60
# Una intervención sin cambio de orador detectado no se estira más que esto.
MAX_TURNO_S = 15 * 60
MAX_CHARS_TURNOS = 16000

PROMPT = """Lees un tramo de la transcripción automática de una sesión del Congreso del Perú (bicameral: Senado y Cámara de Diputados). Cada línea empieza con su marca de tiempo [HH:MM:SS]. El texto NO indica quién habla: tú lo deduces de las pistas del propio texto.

Puede haber líneas «(EN PANTALLA: placa …)»: son la placa del escritorio que muestra la cámara en ese momento, leída del video. Es una pista FUERTE de quién habla ahí (la cámara enfoca a quien tiene la palabra), pero a veces muestra a quien escucha: confírmala con el texto. Si la usas, la pista es "placa en pantalla". El nombre de la placa puede tener una letra mal leída: escríbelo como en la placa.

Anota CADA momento en que empieza a hablar otra persona. Pistas válidas:
- le dan la palabra: "tiene la palabra el congresista X", "adelante, ministra"
- se presenta: "soy el senador X", "les habla X"
- se dirige a la presidencia al empezar: "gracias, presidente; por su intermedio…" (es otro orador, aunque no diga su nombre)
- la presidencia retoma: "gracias, congresista X; tiene la palabra…" (desde ahí habla la presidencia; y la intervención anterior era de X)

NO anotes a alguien solo porque lo mencionan ("la mesa que lidera el diputado X", "como dijo la ministra Y"): solo cuando en esa línea EMPIEZA A HABLAR.
OJO con los saludos: si alguien empieza dirigiéndose a otra persona por su nombre ("Doctora X, primero agradecer…", "Gracias, congresista X…", "Señor ministro…"), quien habla NO es X: es otra persona que le habla a X. Busca en lo que sigue quién es ("vengo a representar a…", "soy…"); si no aparece, deja el nombre vacío.
Si empieza a hablar otra persona pero no se sabe quién (ej. "Gracias, señor presidente…" sin nombre), anótala igual con nombre y cargo vacíos: marca dónde terminó la intervención anterior.

Responde SOLO con líneas, una por cambio de orador, en este formato exacto:
HH:MM:SS | nombre | cargo | pista
- HH:MM:SS: la marca de la línea donde empieza a hablar (cópiala del texto).
- nombre: como aparece en el texto. Vacío si no se dice.
- cargo: congresista, senador, diputado, ministro de X, presidencia de la comisión, invitado, funcionario… Vacío si no se sabe.
- pista: la frase breve del texto que lo delata.
Si una persona habló antes de ser nombrada ("gracias, congresista X" al terminar), anota la línea donde EMPEZÓ a hablar.
No inventes nombres que no estén en el texto. Si no hay ningún cambio de orador identificable, responde solo: NINGUNO"""

# Pistas que son una mención, no alguien tomando la palabra ("la mesa que
# lidera la diputada X"). Visto en una sesión real con gemini-flash-lite.
_MENCION = re.compile(r"\b(liderad[oa]|lidera|presidid[oa]|encabezad[oa]|a cargo de|"
                      r"propuest[oa] (de|por)|como (dijo|señal\w+|mencion\w+)|autor[ía]*)\b",
                      re.IGNORECASE)
_TOMA_PALABRA = re.compile(r"\b(palabra|adelante|soy|saluda|les habla|gracias|buen[oa]s|"
                           r"presidente|presidenta|intermedio)\b", re.IGNORECASE)

_LINEA = re.compile(r"^\s*\[?(\d{1,2}:\d{2}:\d{2})\]?\s*\|\s*([^|]*)\|\s*([^|]*)\|?\s*(.*)$")


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", (t or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def _palabras(t: str) -> list[str]:
    return re.findall(r"[a-zñ]+", _norm(t))


# ── Base ─────────────────────────────────────────────────────────────────────

def listar(video_id: str) -> list[dict]:
    """Turnos en orden, cada uno con su fin (inicio del siguiente, con tope)."""
    with db.conexion() as con:
        filas = con.execute("SELECT start_s, nombre, cargo, pista FROM orador "
                            "WHERE video_id = ? ORDER BY start_s", (video_id,)).fetchall()
        fin = con.execute("SELECT MAX(end_s) FROM ventana WHERE video_id = ?", (video_id,)).fetchone()[0]
    turnos = [dict(f) for f in filas]
    for i, t in enumerate(turnos):
        sig = turnos[i + 1]["start_s"] if i + 1 < len(turnos) else (fin or t["start_s"])
        t["end_s"] = min(max(sig, t["start_s"] + 10), t["start_s"] + MAX_TURNO_S)
        t["ts"] = tr.fmt_ts(t["start_s"])
    return turnos


def _hasta(video_id: str) -> int:
    with db.conexion() as con:
        r = con.execute("SELECT hasta_s FROM orador_indice WHERE video_id = ?", (video_id,)).fetchone()
    return int(r[0]) if r else 0


def _guardar(video_id: str, turnos: list[dict], hasta_s: int) -> None:
    with db.conexion() as con:
        for t in turnos:
            con.execute("INSERT OR REPLACE INTO orador (video_id, start_s, nombre, cargo, pista) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (video_id, t["start_s"], t["nombre"], t["cargo"], t["pista"]))
        con.execute("INSERT INTO orador_indice (video_id, hasta_s) VALUES (?, ?) "
                    "ON CONFLICT(video_id) DO UPDATE SET hasta_s = excluded.hasta_s",
                    (video_id, hasta_s))


# ── Construcción ─────────────────────────────────────────────────────────────

def parsear(respuesta: str, bloque: list[dict], lecturas: list[dict] | None = None) -> list[dict]:
    """Líneas del modelo → turnos válidos. Descarta lo que no se respalda en el
    texto ni en las placas leídas del video."""
    if not bloque:
        return []
    texto = _norm(" ".join([s["texto"] for s in bloque]
                           + [f"{p['nombre']} {p.get('cargo', '')}" for p in lecturas or []]))
    inicios = sorted(s["start_s"] for s in bloque)
    desde, hasta = inicios[0], bloque[-1]["end_s"]
    turnos = []
    for linea in (respuesta or "").splitlines():
        m = _LINEA.match(linea)
        if not m:
            continue
        seg = tr.parse_ts(m.group(1))
        nombre = m.group(2).strip().strip("«»\"'*")
        cargo = m.group(3).strip().strip("«»\"'*")
        pista = m.group(4).strip()[:200]
        if not desde <= seg <= hasta:
            continue
        # La marca va a la ventana que la contiene.
        seg = max((s for s in inicios if s <= seg), default=desde)
        if _MENCION.search(pista) and not _TOMA_PALABRA.search(pista):
            logger.info("oradores: descarto «%s», la pista es una mención: %s", nombre, pista)
            continue
        if nombre:
            if not any(len(p) >= 4 and p[:5] in texto for p in _palabras(nombre)):
                logger.info("oradores: descarto «%s», no está en el texto", nombre)
                continue
        elif not cargo and not pista:
            continue
        turnos.append({"start_s": seg, "nombre": nombre[:80], "cargo": cargo[:80], "pista": pista})
    return turnos


def texto_con_placas(bloque: list[dict], lecturas: list[dict]) -> str:
    """Transcripción del tramo con las placas vistas en el video intercaladas por tiempo."""
    filas = [(s["start_s"], 0, f"[{s['ts']}] {s['texto']}") for s in bloque]
    filas += [(p["start_s"], 1, f"[{tr.fmt_ts(p['start_s'])}] (EN PANTALLA: placa «{p['nombre']}»"
               + (f" — {p['cargo']}" if p.get("cargo") else "") + ")") for p in lecturas]
    return "\n".join(f for _, _, f in sorted(filas))


async def _leer_bloque(bloque: list[dict], lecturas: list[dict] | None = None) -> str:
    salida = []
    async for kind, payload in llm.stream(
            [{"role": "system", "content": PROMPT},
             {"role": "user", "content": texto_con_placas(bloque, lecturas or [])}],
            max_tokens=1200, temperature=0):
        if kind == "text":
            salida.append(payload)
        elif kind == "error":
            raise RuntimeError(payload)
    return "".join(salida)


async def asegurar(video_id: str):
    """
    Arma (o completa) el índice. Generador: emite mensajes de estado para la UI.
    Si un tramo falla, se deja de avanzar ahí: la próxima pregunta lo reintenta.
    """
    datos = tr.obtener(video_id)
    if not datos:
        return
    desde = _hasta(video_id)
    pendientes = [s for s in datos["segmentos"] if s["start_s"] >= desde]
    if not pendientes:
        return
    bloques: list[list[dict]] = []
    for s in pendientes:
        if not bloques or s["start_s"] - bloques[-1][0]["start_s"] >= BLOQUE_S:
            bloques.append([])
        bloques[-1].append(s)
    mirar = placas.disponible() and not datos["sesion"]["en_vivo"]
    for i, b in enumerate(bloques, 1):
        tramo = f"tramo {i} de {len(bloques)} ({b[0]['ts']} – {tr.fmt_ts(b[-1]['end_s'])})"
        lecturas = []
        if mirar:
            yield f"Mirando el video para leer las placas de quien habla: {tramo}..."
            try:
                lecturas = await asyncio.to_thread(placas.capturar, video_id,
                                                   b[0]["start_s"], b[-1]["end_s"])
            except Exception as e:
                # Sin video (borrado, privado) se sigue solo con el texto.
                logger.info("oradores %s: sin capturas del video: %s", video_id, e)
                mirar = False
        yield f"Identificando quién habla en la sesión: {tramo}..."
        try:
            turnos = parsear(await _leer_bloque(b, lecturas), b, lecturas)
        except Exception as e:
            logger.warning("oradores %s: falló el tramo %s: %s", video_id, b[0]["ts"], e)
            return
        _guardar(video_id, turnos, b[-1]["end_s"])


# ── Búsqueda de una persona ──────────────────────────────────────────────────

_NO_NOMBRE = set(_palabras("""
que qué cual cuál fue era es su sus de del la las el los lo le en y o a al por para con sobre
posicion postura opinion respecto tema acerca dijo dice dijeron hablo opina opino planteo
sostuvo defendio propuso critico intervino intervencion intervenciones argumento piensa voto
votaria respondio pidio pregunto senor senora doctor doctora sesion comision cuando como
congresista congresistas senador senadora diputado diputada parlamentario parlamentaria
ministro ministra presidente presidenta invitado invitada funcionario funcionaria
"""))


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def buscar(turnos: list[dict], pregunta: str, cargo: str = "", estricto: bool = False) -> list[dict]:
    """
    Turnos cuya persona coincide con la pregunta. Tolera minúsculas, tildes y
    nombres mal reconocidos ("Huamán" / "Guamán"): compara palabra por palabra.
    Sin nombre en la pregunta, busca por cargo ("¿qué dijo el ministro?").
    """
    claves = [p for p in _palabras(pregunta) if len(p) >= 4 and p not in _NO_NOMBRE]
    # Sin nombre con mayúscula ni cargo en la pregunta, se piden 5+ letras:
    # menos chance de confundir un tema con un apellido corto.
    if estricto:
        claves = [p for p in claves if len(p) >= 5]
    umbral = 0.8
    elegidos = []
    for t in turnos:
        nombres = [p for p in _palabras(t["nombre"]) if len(p) >= 3]
        if claves and nombres and any(_similar(c, n) >= umbral for c in claves for n in nombres):
            elegidos.append(t)
    if not elegidos and cargo and not claves:
        raiz = _norm(cargo)[:6]
        elegidos = [t for t in turnos if raiz in _norm(t["cargo"])]
    return elegidos


def fragmentos_de_turnos(video_id: str, turnos: list[dict]) -> tuple[list[dict], bool]:
    """Ventanas de texto de esas intervenciones (con tope). Devuelve (fragmentos, recortado)."""
    datos = tr.obtener(video_id)
    if not datos:
        return [], False
    elegidos, total, recortado = [], 0, False
    vistos = set()
    for t in turnos:
        for s in datos["segmentos"]:
            if t["start_s"] <= s["start_s"] < t["end_s"] and s["start_s"] not in vistos:
                if total + len(s["texto"]) > MAX_CHARS_TURNOS:
                    recortado = True
                    break
                vistos.add(s["start_s"])
                elegidos.append(s)
                total += len(s["texto"])
        if recortado:
            break
    return sorted(elegidos, key=lambda s: s["start_s"]), recortado


def como_texto(turnos: list[dict]) -> str:
    return "\n".join(
        f"[{t['ts']} – {tr.fmt_ts(t['end_s'])}] {t['nombre'] or '(orador sin identificar)'}"
        + (f" — {t['cargo']}" if t["cargo"] else "")
        + (f" (pista: «{t['pista']}»)" if t["pista"] else "")
        for t in turnos)


def nombres_distintos(turnos: list[dict], maximo: int = 25) -> list[str]:
    vistos, salida = set(), []
    for t in turnos:
        etiqueta = (t["nombre"] or "").strip()
        if not etiqueta or _norm(etiqueta) in vistos:
            continue
        vistos.add(_norm(etiqueta))
        salida.append(f"{etiqueta} ({t['cargo']})" if t["cargo"] else etiqueta)
    return salida[:maximo]
