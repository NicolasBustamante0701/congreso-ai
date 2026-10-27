"""
Transcripciones guardadas: sesiones, ventanas de audio y búsqueda.

Antes la transcripción vivía solo en la memoria de la ventana de Live: al
cerrar la app se perdía, y no había forma de preguntarle nada a una sesión
ya transcrita sin volver a procesar el video. Ahora cada ventana de audio
(10 s) se guarda apenas se transcribe, en SQLite (stdlib, sin servidor ni
dependencias) en DATA_DIR/diana.db.

Qué se guarda por ventana: segundo de inicio y fin, texto y estado:
  ok        hubo voz y Whisper devolvió texto
  silencio  la ventana no tenía señal (no se le mandó a Whisper)
  error     Whisper falló en esa ventana
Guardar también las de silencio y error permite decir con precisión qué
partes del video están cubiertas y cuáles no (ver cobertura()), en vez de
tratar cualquier hueco como "no se dijo nada".

Tiempos: en un VIDEO GRABADO, start_s es el segundo real del video (los
enlaces &t= caen donde corresponde). En un stream EN VIVO no hay forma de
saber el segundo del video: start_s es tiempo de captura, acumulado entre
reconexiones (ver offset_captura). La sesión guarda en_vivo para que la UI y
las respuestas lo aclaren.

Búsqueda: FTS5 con BM25 sobre el texto, sin embeddings. Para preguntas sobre
una sesión alcanza con encontrar los tramos donde aparecen las palabras; si
eso no recupera bien en la práctica, ahí se evalúa otra cosa.
"""
import re

from config import ahora_lima
from services import db

# ── Tiempos ──────────────────────────────────────────────────────────────────

def fmt_ts(segundos: int) -> str:
    """Segundos → HH:MM:SS. Antes era mm:ss y una sesión de 7 h mostraba "420:00"."""
    s = max(0, int(segundos))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def parse_ts(txt: str) -> int:
    """"HH:MM:SS" o "MM:SS" → segundos. Dos partes = minutos:segundos."""
    partes = [int(p) for p in txt.strip().split(":")]
    if len(partes) == 3:
        return partes[0] * 3600 + partes[1] * 60 + partes[2]
    if len(partes) == 2:
        return partes[0] * 60 + partes[1]
    raise ValueError(txt)


_TS = r"\d{1,2}:\d{2}(?::\d{2})?"
RANGO_RE = re.compile(rf"({_TS})\s*(?:y|a|al|hasta|-|–)\s*(?:el\s+|las\s+)?({_TS})", re.I)


def rango_en_pregunta(pregunta: str):
    """(desde, hasta) en segundos si la pregunta nombra un intervalo; si no, None."""
    m = RANGO_RE.search(pregunta)
    if not m:
        return None
    a, b = parse_ts(m.group(1)), parse_ts(m.group(2))
    return (a, b) if a <= b else (b, a)


# ── Conexión ─────────────────────────────────────────────────────────────────
# Esquema y migraciones en services/db.py (los comparte con el seguimiento).
_db = db.conexion


def _ahora() -> str:
    return ahora_lima().strftime("%Y-%m-%d %H:%M:%S")


# ── Escritura ────────────────────────────────────────────────────────────────

def iniciar_sesion(video_id: str, titulo: str = "", en_vivo: bool = False) -> None:
    ahora = _ahora()
    url = f"https://www.youtube.com/watch?v={video_id}"
    with _db() as con:
        con.execute(
            """INSERT INTO sesion (video_id, titulo, url, en_vivo, estado, creado, actualizado)
               VALUES (?, ?, ?, ?, 'en_curso', ?, ?)
               ON CONFLICT(video_id) DO UPDATE SET
                 titulo = CASE WHEN excluded.titulo != '' THEN excluded.titulo ELSE sesion.titulo END,
                 en_vivo = excluded.en_vivo, estado = 'en_curso', actualizado = excluded.actualizado""",
            (video_id, titulo or "", url, int(en_vivo), ahora, ahora))


def guardar_ventana(video_id: str, start_s: int, end_s: int, texto: str, estado: str) -> None:
    """
    Guarda una ventana. Si ya había ventanas que se pisan con este intervalo
    (se volvió a transcribir ese tramo, o se reconectó desde un segundo que
    no cae justo en el borde), se reemplazan: la última pasada gana y el
    texto no queda duplicado.
    """
    with _db() as con:
        con.execute("DELETE FROM ventana WHERE video_id = ? AND start_s < ? AND end_s > ?",
                    (video_id, end_s, start_s))
        con.execute("INSERT INTO ventana (video_id, start_s, end_s, texto, estado) VALUES (?, ?, ?, ?, ?)",
                    (video_id, int(start_s), int(end_s), texto or "", estado))
        # Si se re-transcribe un tramo ya leído por el índice de oradores
        # (services/oradores), ese tramo se vuelve a leer la próxima vez.
        con.execute("DELETE FROM orador WHERE video_id = ? AND start_s >= ?", (video_id, int(start_s)))
        con.execute("UPDATE orador_indice SET hasta_s = MIN(hasta_s, ?) WHERE video_id = ?",
                    (int(start_s), video_id))
        con.execute("UPDATE sesion SET actualizado = ? WHERE video_id = ?", (_ahora(), video_id))


def marcar_estado(video_id: str, estado: str) -> None:
    with _db() as con:
        con.execute("UPDATE sesion SET estado = ?, actualizado = ? WHERE video_id = ?",
                    (estado, _ahora(), video_id))


def guardar_informe(video_id: str, texto: str) -> None:
    with _db() as con:
        con.execute("UPDATE sesion SET informe = ?, informe_generado = ? WHERE video_id = ?",
                    (texto, _ahora(), video_id))


def borrar(video_id: str) -> bool:
    with _db() as con:
        return con.execute("DELETE FROM sesion WHERE video_id = ?", (video_id,)).rowcount > 0


# ── Lectura ──────────────────────────────────────────────────────────────────

def offset_captura(video_id: str) -> int:
    """Último segundo guardado: un stream en vivo retoma la numeración desde acá."""
    with _db() as con:
        r = con.execute("SELECT MAX(end_s) FROM ventana WHERE video_id = ?", (video_id,)).fetchone()
    return int(r[0] or 0)


def ultimo_texto(video_id: str) -> str:
    """Texto de la última ventana con voz (para empalmar una reconexión)."""
    with _db() as con:
        r = con.execute("""SELECT texto FROM ventana WHERE video_id = ? AND estado = 'ok'
                           ORDER BY start_s DESC LIMIT 1""", (video_id,)).fetchone()
    return r[0] if r else ""


def listar_sesiones() -> list[dict]:
    with _db() as con:
        filas = con.execute(
            """SELECT s.video_id, s.titulo, s.url, s.en_vivo, s.estado, s.creado, s.actualizado,
                      s.informe IS NOT NULL AS tiene_informe,
                      COUNT(v.id) AS ventanas,
                      SUM(v.estado = 'ok') AS con_texto,
                      MAX(v.end_s) AS hasta_s
               FROM sesion s LEFT JOIN ventana v ON v.video_id = s.video_id
               GROUP BY s.video_id ORDER BY s.actualizado DESC""").fetchall()
    return [{**dict(f), "en_vivo": bool(f["en_vivo"]), "tiene_informe": bool(f["tiene_informe"]),
             "hasta": fmt_ts(f["hasta_s"] or 0)} for f in filas]


def _seg(f) -> dict:
    return {"start_s": f["start_s"], "end_s": f["end_s"], "ts": fmt_ts(f["start_s"]),
            "texto": f["texto"], "estado": f["estado"]}


def obtener(video_id: str) -> dict | None:
    with _db() as con:
        s = con.execute("SELECT * FROM sesion WHERE video_id = ?", (video_id,)).fetchone()
        if s is None:
            return None
        ventanas = con.execute(
            "SELECT start_s, end_s, texto, estado FROM ventana WHERE video_id = ? ORDER BY start_s",
            (video_id,)).fetchall()
    return {
        "sesion": {**dict(s), "en_vivo": bool(s["en_vivo"])},
        "segmentos": [_seg(v) for v in ventanas if v["estado"] == "ok" and v["texto"].strip()],
        "cobertura": cobertura([dict(v) for v in ventanas]),
    }


def cobertura(ventanas: list[dict]) -> dict:
    """
    Qué partes del intervalo transcrito tienen texto, silencio, error, o
    directamente no se procesaron (huecos: el usuario detuvo y retomó más
    adelante, o se cortó la conexión).
    """
    if not ventanas:
        return {"desde": None, "hasta": None, "ok": 0, "silencio": 0, "error": 0,
                "huecos": [], "errores": []}
    vs = sorted(ventanas, key=lambda v: v["start_s"])
    huecos, prev_fin = [], vs[0]["start_s"]
    for v in vs:
        if v["start_s"] > prev_fin:
            huecos.append([fmt_ts(prev_fin), fmt_ts(v["start_s"])])
        prev_fin = max(prev_fin, v["end_s"])
    cuenta = {e: sum(1 for v in vs if v["estado"] == e) for e in ("ok", "silencio", "error")}
    return {"desde": fmt_ts(vs[0]["start_s"]), "hasta": fmt_ts(prev_fin), **cuenta,
            "huecos": huecos,
            "errores": [fmt_ts(v["start_s"]) for v in vs if v["estado"] == "error"]}


# ── Búsqueda ─────────────────────────────────────────────────────────────────

_VACIAS = set("""
a al algo ante antes así como con contra cual cuál cuando cuándo de del desde donde dónde
el ella ellas ellos en entre era es esa ese eso esta este esto fue han hay la las le les lo
los más me mi muy no nos o para pero por porque que qué quien quién se ser si sí sin sobre
su sus también te tiene tu un una uno unos y ya dijo dice dijeron habló hablaron mencionó
sesión sesion video parte momento minuto
""".split())


def _consulta_fts(texto: str) -> str:
    """
    Palabras de la pregunta → consulta FTS5 con OR, todas por prefijo para
    que el plural también cuente ("agua*" encuentra aguas). Las largas se
    recortan a 6 letras ("presup*" encuentra presupuesto y presupuestal):
    una raíz barata para el español sin meter un stemmer.
    """
    vistas, terminos = set(), []
    for t in re.findall(r"\w+", texto.lower()):
        if len(t) < 3 or t in _VACIAS:
            continue
        termino = f'"{t[:6]}"*'
        if termino not in vistas:
            vistas.add(termino)
            terminos.append(termino)
    return " OR ".join(terminos[:12])


def buscar(q: str, video_id: str | None = None, limite: int = 20) -> list[dict]:
    consulta = _consulta_fts(q)
    if not consulta:
        return []
    sql = """SELECT v.video_id, v.start_s, v.end_s, v.texto, v.estado, s.titulo, s.en_vivo,
                    snippet(ventana_fts, 0, '«', '»', '…', 14) AS fragmento
             FROM ventana_fts JOIN ventana v ON v.id = ventana_fts.rowid
             JOIN sesion s ON s.video_id = v.video_id
             WHERE ventana_fts MATCH ?"""
    args: list = [consulta]
    if video_id:
        sql += " AND v.video_id = ?"
        args.append(video_id)
    sql += " ORDER BY bm25(ventana_fts) LIMIT ?"
    args.append(limite)
    with _db() as con:
        filas = con.execute(sql, args).fetchall()
    return [{**_seg(f), "video_id": f["video_id"], "titulo": f["titulo"],
             "en_vivo": bool(f["en_vivo"]), "fragmento": f["fragmento"]} for f in filas]


# Hasta cuánto texto se le pasa el transcript ENTERO al modelo cuando la
# pregunta es general ("¿de qué se habló?") y no hay palabras para buscar.
MAX_CHARS_TRANSCRIPT_COMPLETO = 14000
MAX_FRAGMENTOS = 8


def fragmentos_para_pregunta(video_id: str, pregunta: str) -> tuple[list[dict], str]:
    """
    Tramos de la transcripción que hacen falta para contestar, y cómo se
    eligieron ("rango", "busqueda", "completa" o "nada").

    1. Si la pregunta nombra un intervalo ("entre 00:24:10 y 00:26:05"), se
       toma exactamente ese intervalo: es un filtro por tiempo, no algo que
       haya que adivinar.
    2. Si no, búsqueda por palabras (BM25) y a cada acierto se le suma la
       ventana anterior y la siguiente, para que la frase no quede cortada.
    3. Si la búsqueda no encuentra nada y la sesión es corta, va completa
       (preguntas generales). Si es larga, no se manda nada y la respuesta
       dice que no aparece en lo transcrito.
    """
    datos = obtener(video_id)
    if not datos:
        return [], "nada"
    segs = datos["segmentos"]

    rango = rango_en_pregunta(pregunta)
    if rango:
        a, b = rango
        return [s for s in segs if s["end_s"] > a and s["start_s"] < b], "rango"

    hits = buscar(pregunta, video_id=video_id, limite=MAX_FRAGMENTOS)
    if hits:
        por_inicio = {s["start_s"]: i for i, s in enumerate(segs)}
        elegidos = set()
        for h in hits:
            i = por_inicio.get(h["start_s"])
            if i is None:
                continue
            elegidos.update(j for j in (i - 1, i, i + 1) if 0 <= j < len(segs))
        return [segs[i] for i in sorted(elegidos)], "busqueda"

    if sum(len(s["texto"]) for s in segs) <= MAX_CHARS_TRANSCRIPT_COMPLETO:
        return segs, "completa"
    return [], "nada"


# ── Preguntas sobre una persona ("¿qué dijo el congresista X?") ─────────────
#
# La transcripción no sabe quién habla: a una persona se la ubica porque la
# nombran ("tiene la palabra el congresista X") y su intervención sigue en
# los minutos siguientes. Con la búsqueda normal (ventana anterior y
# siguiente, 30 s) la intervención quedaba cortada en la primera frase.

_PREGUNTA_PERSONA = re.compile(
    r"\b(dij\w*|dice|habl\w*|opin\w*|posici[oó]n|postura|plante\w*|sostuv\w*|"
    r"defendi\w*|propus\w*|propuso|critic\w*|intervin\w*|intervenci[oó]n\w*|"
    r"argument\w*|pens\w*|votar\w*|vot[oó]|respondi\w*|pidi[oó]|pregunt[oó])\b",
    re.IGNORECASE)
_CARGOS = re.compile(
    r"\b(congresistas?|senador(?:a|es|as)?|diputad[oa]s?|parlamentari[oa]s?|"
    r"ministr[oa]s?|viceministr[oa]s?|presidente|presidenta|premier|"
    r"vicepresident[ea]s?|fiscal|contralor(?:a)?|defensor(?:a)?|alcalde(?:sa)?|"
    r"gobernador(?:a)?|jefe|jefa|director(?:a)?|superintendente|invitad[oa]s?|"
    r"funcionari[oa]s?|vocero|vocera|relator(?:a)?)\b", re.IGNORECASE)
# Palabras de la pregunta que no son ni nombre ni cargo.
_NO_NOMBRE = _VACIAS | set("""
cual cuál fue era es su sus posicion posición postura opinion opinión respecto tema
sobre acerca qué que dijo dice dijeron habló hablo opina opinó planteó planteo sostuvo
defendió propuso criticó intervino intervención intervenciones argumentó piensa votó
votaría respondió pidió preguntó señor señora doctor doctora del de la el
""".split())
# Ventanas de 10 s que se toman después de cada mención: ~4 minutos.
VENTANAS_INTERVENCION = 24
MAX_CHARS_PERSONA = 16000


def _sin_tildes(t: str) -> str:
    return t.translate(str.maketrans("áéíóúü", "aeiouu"))


# "¿qué se dijo del canon?", "¿de qué se habló?": impersonal, no es una persona.
_IMPERSONAL = re.compile(r"\bse\s+(dij|dic|habl|trat|mencion|plante|propus|propuso|discuti|debati)",
                         re.IGNORECASE)


def posible_pregunta_persona(pregunta: str) -> bool:
    """Puede ser sobre una persona aunque el nombre venga en minúsculas ("qué dijo perez")."""
    if _CARGOS.search(pregunta):
        return True
    return bool(_PREGUNTA_PERSONA.search(pregunta)) and not _IMPERSONAL.search(pregunta)


def persona_en_pregunta(pregunta: str) -> dict | None:
    """
    Si la pregunta es sobre lo que dijo una persona, devuelve
    {"nombre": "Pérez Gómez" | "", "cargo": "congresista" | "", "busqueda": "..."}.
    None si no es una pregunta sobre una persona.
    """
    cargo = _CARGOS.search(pregunta)
    verbo = _PREGUNTA_PERSONA.search(pregunta)
    if not verbo and not cargo:
        return None
    sin_cargo = _CARGOS.sub(" ", pregunta)
    # El nombre: palabras que no son vacías, de 3+ letras, tal como las
    # escribió el usuario para mostrarlas en la respuesta.
    nombre = [w for w in re.findall(r"\w+", sin_cargo)
              if len(w) >= 3 and w.lower() not in _NO_NOMBRE and not w.isdigit()]
    if not cargo and not (verbo and nombre):
        return None
    if not cargo and not any(w[0].isupper() for w in nombre):
        # "¿qué se dijo del canon?" no es sobre una persona.
        return None
    cargo_txt = cargo.group(0).lower() if cargo else ""
    nombre_txt = " ".join(nombre[:4])
    return {"nombre": nombre_txt, "cargo": cargo_txt, "busqueda": nombre_txt or cargo_txt}


def es_legislador(cargo: str) -> bool:
    return bool(re.match(r"(congresist|senador|diputad|parlamentari)", (cargo or "").lower()))


def fragmentos_de_persona(video_id: str, persona: dict) -> list[dict]:
    """Cada mención de la persona y los ~4 minutos siguientes (su intervención)."""
    datos = obtener(video_id)
    if not datos or not persona.get("busqueda"):
        return []
    segs = datos["segmentos"]
    hits = buscar(persona["busqueda"], video_id=video_id, limite=MAX_FRAGMENTOS)
    if persona["nombre"] and hits:
        # Con nombre, solo cuentan las ventanas que lo traen de verdad: el OR
        # del buscador también acierta con una sola palabra del nombre.
        # Si ninguna ventana trae dos palabras del nombre, vale una (en la
        # sala suelen nombrar solo el apellido: "congresista Pérez").
        claves = [_sin_tildes(p.lower()[:6]) for p in persona["nombre"].split()]
        cuantas = [(h, sum(c in _sin_tildes(h["texto"].lower()) for c in claves)) for h in hits]
        minimo = 2 if len(claves) > 1 and any(n >= 2 for _, n in cuantas) else 1
        hits = [h for h, n in cuantas if n >= minimo]
    por_inicio = {s["start_s"]: i for i, s in enumerate(segs)}
    elegidos: set[int] = set()
    for h in sorted(hits, key=lambda h: h["start_s"]):
        i = por_inicio.get(h["start_s"])
        if i is None:
            continue
        nuevos = {j for j in range(i - 1, i + VENTANAS_INTERVENCION + 1) if 0 <= j < len(segs)}
        if elegidos and sum(len(segs[j]["texto"]) for j in elegidos | nuevos) > MAX_CHARS_PERSONA:
            break
        elegidos |= nuevos
    return [segs[j] for j in sorted(elegidos)]


def como_texto(segmentos: list[dict]) -> str:
    """Bloque para el prompt: una línea por ventana con su marca de tiempo."""
    return "\n".join(f"[{s['ts']}] {s['texto']}" for s in segmentos)
