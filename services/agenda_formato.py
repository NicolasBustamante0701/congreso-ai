"""
Separa el texto de una sesión de la agenda oficial en partes legibles.

La agenda (comunicaciones.congreso.gob.pe) publica cada sesión como un solo
párrafo: "Sesión de la Comisión X Invitado: Señor N, cargo. Tema: T. -Otros
temas en agenda. Agenda documentada", y el organizador como "Comisión X
Presidente: Diputado N (Grupo Parlamentario G)". La pantalla Agenda lo
mostraba tal cual y era un muro de texto. Esto solo arma la presentación: el
texto original sigue en `tema` y `organiza`, y lo que no calza con ningún
patrón se muestra entero en vez de perderse.
"""
import re

_ESPACIOS = re.compile(r"\s+")
# Coletillas que no dicen nada del contenido; se cambian por `mas_temas`.
_COLETILLAS = re.compile(
    r"\(?\s*-?\s*(?:Ver\s+)?otros\s+(?:invitados\s+y\s+)?temas\s+en\s+(?:la\s+)?agenda\s*\.?\s*\)?"
    r"|\bAgenda\s+documentada\s*\.?",
    re.IGNORECASE)
# "lnvitado" (ele en vez de i) aparece así en la fuente.
_INVITADO = re.compile(r"\b[IlL]nvitad[oa]s?\s*:\s*", re.IGNORECASE)
_TEMA = re.compile(r"\bTemas?\s*:\s*", re.IGNORECASE)
_HONORIFICO = re.compile(r"^(?:Señora?|Sr\.?|Sra\.?)\s+", re.IGNORECASE)
# "Sesión de la Comisión de Constitución -Sustentación de ..."
_SESION_GUION = re.compile(r"^(Sesi[oó]n\s+del?\s+.+?)\s+-\s*(\S.*)$", re.IGNORECASE)
_RESPONSABLE = re.compile(
    r"^(?P<organo>.+?)[.\s]+(?P<rol>Presidente|Presidenta|Coordinador|Coordinadora)\s*:\s*(?P<quien>.+)$")
_SESION_GENERICA = re.compile(r"^Sesi[oó]n\s+de\s+la\s+Comisi[oó]n\b", re.IGNORECASE)
# "Grupo de trabajo sobre X, constituido en el ámbito de la Comisión Y" o
# "Grupo de trabajo sobre X - Comisión Y": el grupo arriba, la comisión aparte.
_GRUPO_EN_COMISION = re.compile(
    r"^(?P<grupo>Grupo\s+de\s+trabajo\b.+?)\s*(?:,?\s*constitu[ií]do\s+en\s+el\s+[aá]mbito\s+de|\s+-)"
    r"\s*(?:la\s+)?(?P<comision>Comisi[oó]n\b.+)$", re.IGNORECASE)


def _limpio(texto: str) -> str:
    texto = _ESPACIOS.sub(" ", texto or "").strip()
    texto = re.sub(r"\s+([,.;:])", r"\1", texto)
    return texto.strip(" .-–")


def _invitado(texto: str) -> str:
    """'Señor José Tangherlini Casal , ministro de X.' → 'José Tangherlini Casal — ministro de X'."""
    texto = _HONORIFICO.sub("", _limpio(texto))
    nombre, _, cargo = texto.partition(",")
    nombre, cargo = nombre.strip(), cargo.strip()
    return f"{nombre} — {cargo}" if cargo else nombre


def _responsable(quien: str) -> str:
    # "(Grupo Parlamentario Fuerza Popular)" → "(Fuerza Popular)"
    return _limpio(re.sub(r"\(\s*Grupo Parlamentario\s+", "(", quien))


def partes(tema: str, organiza: str = "") -> dict:
    """
    Devuelve {titulo, actividad, invitado, tema, organo, comision, responsable, mas_temas}.

    - titulo: lo que encabeza la tarjeta (el órgano si se conoce).
    - actividad: qué es ("Mesa de trabajo «…»"), vacío si solo repetiría el órgano.
    - invitado / tema: las partes del párrafo, vacías si no vienen.
    """
    original = tema or ""
    mas_temas = bool(_COLETILLAS.search(original))
    texto = _limpio(_COLETILLAS.sub(" ", original))

    invitado = tema_txt = ""
    cabeza = texto
    m_inv = _INVITADO.search(texto)
    m_tema = _TEMA.search(texto)
    if m_inv:
        cabeza = texto[:m_inv.start()]
        fin_inv = m_tema.start() if m_tema and m_tema.start() > m_inv.end() else len(texto)
        invitado = _invitado(texto[m_inv.end():fin_inv])
    if m_tema:
        if not m_inv:
            cabeza = texto[:m_tema.start()]
        tema_txt = _limpio(texto[m_tema.end():])
    if not m_inv and not m_tema:
        m = _SESION_GUION.match(texto)
        if m:
            cabeza, tema_txt = m.group(1), _limpio(m.group(2))
    cabeza = _limpio(cabeza)

    organo = responsable = ""
    org = _limpio(organiza).strip("“”\"")
    m = _RESPONSABLE.match(org)
    if m:
        organo = _limpio(m.group("organo")).strip("“”\" ")
        responsable = f"{m.group('rol')}: {_responsable(m.group('quien'))}"
    elif org:
        responsable = _responsable(org)
    comision = ""
    m = _GRUPO_EN_COMISION.match(organo)
    if m:
        organo, comision = _limpio(m.group("grupo")).strip("“”\" "), _limpio(m.group("comision"))

    if organo:
        titulo = organo
        actividad = "" if _SESION_GENERICA.match(cabeza) else cabeza
        # "Grupo de trabajo sobre X Sesión de instalación" → "Sesión de instalación"
        if actividad.lower().startswith(organo.lower()):
            actividad = _limpio(actividad[len(organo):])
    else:
        titulo, actividad = cabeza or texto, ""

    return {
        "titulo": titulo,
        "actividad": actividad,
        "invitado": invitado,
        "tema": tema_txt,
        "organo": organo,
        "comision": comision,
        "responsable": responsable,
        "mas_temas": mas_temas,
    }
