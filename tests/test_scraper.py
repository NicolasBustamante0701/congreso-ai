"""
Tests de funciones puras del scraper — sin red, sin VCR.
"""
from datetime import date
from pathlib import Path

import pytest

from scraper import (
    _contar_items_agenda,
    _detectar_camara,
    _parsear_agenda_dia,
    _periodos_de_numero,
    estado_legislativo,
    estado_legislativo_texto,
)


@pytest.mark.parametrize("hoy,en_legislatura,legislatura", [
    # Primera Legislatura Ordinaria: 27 jul → 15 dic
    (date(2026, 7, 27), True, "Primera Legislatura Ordinaria"),   # primer día
    (date(2026, 8, 2), True, "Primera Legislatura Ordinaria"),    # confirmado en vivo
    (date(2026, 12, 15), True, "Primera Legislatura Ordinaria"),  # último día
    # Receso entre legislaturas: 16 dic → último día de feb
    (date(2026, 12, 16), False, None),
    (date(2027, 1, 15), False, None),
    (date(2027, 2, 28), False, None),
    # Segunda Legislatura Ordinaria: 1 mar → 15 jun
    (date(2027, 3, 1), True, "Segunda Legislatura Ordinaria"),
    (date(2027, 6, 15), True, "Segunda Legislatura Ordinaria"),
    # Receso antes del próximo período anual: 16 jun → 26 jul
    (date(2027, 6, 16), False, None),
    (date(2027, 7, 26), False, None),
])
def test_estado_legislativo(hoy, en_legislatura, legislatura):
    e = estado_legislativo(hoy)
    assert e["en_legislatura"] is en_legislatura
    assert e["legislatura"] == legislatura


def test_estado_legislativo_cruza_año_en_diciembre():
    """31 dic sigue perteneciendo al período anual que empezó en julio del mismo año."""
    e = estado_legislativo(date(2026, 12, 31))
    assert e["en_legislatura"] is False
    # La próxima legislatura (segunda) empieza en marzo del año siguiente.
    assert e["proxima_legislatura"] == "01/03/2027"


def test_estado_legislativo_cruza_año_en_enero():
    """1 ene ya pertenece al período anual que empezó en julio del año anterior."""
    e = estado_legislativo(date(2027, 1, 1))
    assert e["en_legislatura"] is False
    assert e["proxima_legislatura"] == "01/03/2027"


def test_estado_legislativo_texto_en_sesion():
    texto = estado_legislativo_texto(date(2026, 8, 2))
    assert "en sesión" in texto
    assert "Primera Legislatura Ordinaria" in texto
    assert "15/12/2026" in texto


def test_estado_legislativo_texto_en_receso():
    texto = estado_legislativo_texto(date(2027, 1, 15))
    assert "receso" in texto
    assert "01/03/2027" in texto


# ── Búsqueda por materia (filtro local sobre títulos) ────────────────────────
from scraper import _coincide_materia, _keywords_materia  # noqa: E402


def test_materia_exige_palabras_completas_no_subcadenas():
    kw = _keywords_materia("salud mental")
    # Títulos reales que antes se colaban en "salud mental".
    assert not _coincide_materia(
        "PROPOSICIÓN LEGISLATIVA QUE FORTALECE LA LUCHA CONTRA EL CRIMEN ORGANIZADO E "
        "INCORPORA LA VIGILANCIA AÉREA Y LA INSTRUMENTALIZACIÓN DE NAVES NO TRIPULADAS", kw)
    assert not _coincide_materia(
        "LEY DE PROMOCIÓN Y DESARROLLO SOSTENIBLE DE LAS ACTIVIDADES CON ROCAS ORNAMENTALES", kw)
    assert not _coincide_materia(
        "LEY QUE PROMUEVE LA SEGURIDAD DEL PACIENTE EN EL SECTOR SALUD", kw)
    assert _coincide_materia("LEY QUE FORTALECE LA ATENCIÓN EN SALUD MENTAL COMUNITARIA", kw)
    assert _coincide_materia("LEY DE SERVICIOS DE SALUD PARA TRASTORNOS MENTALES", kw)


def test_materia_ignora_acentos_y_palabras_vacias():
    assert _keywords_materia("proyectos de ley sobre educación") == ["EDUCACION"]
    assert _coincide_materia("LEY QUE MEJORA LA EDUCACIÓN RURAL", _keywords_materia("educacion"))
    assert _keywords_materia("ley de la") == []


async def test_materia_con_dias_filtra_por_tema():
    from datetime import datetime
    from unittest.mock import AsyncMock, patch

    import scraper

    hoy = datetime.utcnow().strftime("%Y-%m-%dT00:00:00")
    items = [
        {"pleyNum": 438, "proyectoLey": "00438-2026-2031-CD", "fecPresentacion": hoy, "_perPar": 2026, "_camara": "D",
         "titulo": "LEY QUE RECONOCE EL CESE IRREGULAR EN LA COMPENSACIÓN POR TIEMPO DE SERVICIOS"},
        {"pleyNum": 412, "proyectoLey": "00412-2026-2031-CD", "fecPresentacion": hoy, "_perPar": 2026, "_camara": "D",
         "titulo": "LEY QUE GARANTIZA LA OBSTETRICIA EN LOS ESTABLECIMIENTOS PÚBLICOS DE SALUD"},
    ]
    with patch.object(scraper, "_spley_proyectos", new=AsyncMock(return_value=items)):
        r = await scraper.fetch_proyectos(materia="salud", dias=15)
    assert [i["numero"] for i in r["items"]] == ["00412-2026-2031-CD"]


async def test_lista_de_sinonimos_cae_a_coincidencia_parcial_avisada():
    from unittest.mock import AsyncMock, patch

    import scraper

    items = [{"pleyNum": 1, "proyectoLey": "00001-2026-2031-CD", "_perPar": 2026, "_camara": "D",
              "titulo": "LEY QUE GARANTIZA MEDICAMENTOS GENÉRICOS"}]
    with patch.object(scraper, "_spley_proyectos", new=AsyncMock(return_value=items)):
        r = await scraper._fetch_spley_por_materia("hospitales medicamentos cirugia")
        assert r["criterio"].startswith("COINCIDENCIA PARCIAL")
        assert len(r["items"]) == 1
        # Con dos palabras no hay coincidencia parcial.
        r2 = await scraper._fetch_spley_por_materia("salud medicamentos")
        assert r2.get("sin_datos")


@pytest.mark.parametrize("numero,periodos", [
    # Formato viejo: solo existe en 2021-2026. Sin esto, la búsqueda se
    # conformaba con los 300+ proyectos de 2026-2031 y daba "no encontrado".
    ("14864/2025-CR", [2021]),
    ("00571-2026-2031-CD", [2026]),
    ("571", None),
])
def test_periodos_de_numero(numero, periodos):
    assert _periodos_de_numero(numero) == periodos


def test_detectar_camara_reconoce_diputada_y_senadora():
    assert _detectar_camara("Presidenta: Diputada Flor Meza Rivera") == "Cámara de Diputados"
    assert _detectar_camara("Senadora Patricia Juárez") == "Senado"


def test_contar_items_agenda_reinicia_por_subseccion():
    estructura = [{"seccion": "Mociones de orden del día", "subsecciones": [
        {"seccion": "Comisiones de investigación"},
        {"seccion": "Comisiones especiales"},
    ]}]
    texto = "\n".join([
        "MOCIONES DE ORDEN DEL DÍA", "COMISIONES DE INVESTIGACIÓN", "1.", "x", "2.",
        "COMISIONES ESPECIALES", "1.", "2.", "3.", "Página 7.", "5.",
    ])
    _contar_items_agenda(texto, estructura)
    subs = estructura[0]["subsecciones"]
    assert subs[0]["asuntos"] == 2
    assert subs[1]["asuntos"] == 3  # "5." no es correlativo
    assert estructura[0]["asuntos"] == 5


def test_contar_items_agenda_asunto_unico_sin_numerar():
    # Agenda de Diputados del 06/10/2026: un solo dictamen y una sola moción
    # de interpelación, sin "1.". Sin texto en el cuerpo no se cuenta nada.
    estructura = [
        {"seccion": "Dictamen"},
        {"seccion": "Mociones de orden del día", "subsecciones": [
            {"seccion": "Moción de interpelación"},
            {"seccion": "Comisiones especiales"},
        ]},
        {"seccion": "Informes finales"},
    ]
    texto = "\n".join([
        "DICTAMEN", "Proyecto de Ley 98. Se propone delegar en el Poder Ejecutivo...",
        "MOCIONES DE ORDEN DEL DÍA", "MOCIÓN DE INTERPELACIÓN",
        "Moción 424, de los diputados Huilca Flores, Colchado Huamani...",
        "COMISIONES ESPECIALES", "1.", "Moción 10 de los diputados ...", "2.",
        "INFORMES FINALES",
    ])
    _contar_items_agenda(texto, estructura)
    assert estructura[0]["asuntos"] == 1
    subs = estructura[1]["subsecciones"]
    assert subs[0]["asuntos"] == 1
    assert subs[1]["asuntos"] == 2
    assert estructura[1]["asuntos"] == 3
    assert "asuntos" not in estructura[2]


def test_parsear_agenda_tema_de_tres_lineas():
    # 15/10/2026: la sesión de Constitución del Senado tiene un tema de tres
    # líneas. Con el tope de 2 líneas salía sin cámara ni lugar, y Diana
    # decía que el Senado no tenía sesiones de comisión ese día.
    html = (Path(__file__).parent / "fixtures" / "agenda-comunicaciones-2026-10-15.html").read_text()
    sesiones, avisos = _parsear_agenda_dia(html, date(2026, 10, 15), "https://x/")
    assert len(sesiones) == 10
    constitucion = next(s for s in sesiones if "Constitución" in s["tema"])
    assert constitucion["camara"] == "Senado"
    assert constitucion["es_comision"]
    assert constitucion["hora"] == "11:00 AM"
    assert constitucion["lugar"] == "Palacio Legislativo. Sala Miguel Grau Seminario"
    assert "00132026-2031-S" in constitucion["tema"]
    assert constitucion["documento"].endswith("COMISION-CONSTITUCION-Y-RREE-151026.pdf")
    # Ninguna sesión arrastra "Descargar" ni texto del pie de página.
    for s in sesiones:
        assert "Descargar" not in s["tema"] + s["lugar"]
        assert s["organiza"] and s["lugar"]
    assert [a["aviso"] for a in avisos] == [
        "Semana de Representación Del lunes 12 al viernes 16 de octubre de 2026."]


@pytest.mark.parametrize("texto,codigo", [
    ("diputados", "D"), ("Cámara de Diputados", "D"), ("SENADO", "S"),
    ("congreso", "C"), ("", None), ("pleno", None),
])
def test_codigo_camara(texto, codigo):
    from scraper import _codigo_camara
    assert _codigo_camara(texto) == codigo


def test_herramienta_ignora_argumentos_inventados():
    from services.tools import _solo

    def f(dias=None, camara=None):
        return None

    assert _solo({"dias": 7, "camara": "senado", "inventado": 1}, f) == {"dias": 7, "camara": "senado"}


def test_mesa_directiva_desde_html_y_agenda():
    from scraper import _mesa_desde_html, _preside_desde_agenda

    html = ("<div><p>Mesa Directiva</p><p>Torres Morales, Miguel Ángel</p><p>Presidente</p>"
            "<p>mtorres@congreso.gob.pe</p><p>Chacón Trujillo, Nilza Merly</p>"
            "<p>Segunda Vicepresidenta</p></div>")
    assert _mesa_desde_html(html) == [
        {"cargo": "Presidente", "nombre": "Torres Morales, Miguel Ángel"},
        {"cargo": "Segunda Vicepresidenta", "nombre": "Chacón Trujillo, Nilza Merly"},
    ]
    portada = "Sesión del martes 6 de octubre de 2026\nPRESIDENCIA DEL SEÑOR  \nOSCAR DE JESÚS RETO OTERO \n"
    assert _preside_desde_agenda(portada) == "Oscar De Jesús Reto Otero"
