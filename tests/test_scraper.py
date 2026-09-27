"""
Tests de funciones puras del scraper — sin red, sin VCR.
"""
from datetime import date

import pytest

from scraper import estado_legislativo, estado_legislativo_texto


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
