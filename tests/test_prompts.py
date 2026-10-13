"""
Los prompts que de verdad recibe el modelo: identidad, política de fuentes y
tablas. Un cambio de redacción que borre una de estas reglas rompe acá.
"""
from pathlib import Path

import pytest

from config import PROMPTS_DIR
from services.prompt_registry import (
    SYSTEM_MINI,
    WORKFLOWS,
    resumen_con_fechas,
    system_con_fecha,
)

HOY, DESDE = "12/10/2026", "05/10/2026"
TODOS = sorted(Path(PROMPTS_DIR).glob("*.md"))


def test_system_base_es_diana_y_solo_fuentes_oficiales():
    s = system_con_fecha(HOY)
    assert s.startswith("Eres **Diana**")
    assert "congreso.gob.pe" in s
    assert "No lo encontré en las fuentes oficiales consultadas" in s
    assert "Tablas para datos comparables" in s
    assert HOY in s


def test_system_mini_tiene_la_misma_politica():
    assert SYSTEM_MINI.startswith("Eres Diana")
    assert "fuentes oficiales" in SYSTEM_MINI and "Tablas" in SYSTEM_MINI


@pytest.mark.parametrize("ruta", TODOS, ids=lambda p: p.name)
def test_ningun_prompt_conserva_la_identidad_vieja(ruta):
    texto = ruta.read_text(encoding="utf-8")
    assert "Solón" not in texto
    assert "Julio César" not in texto


def test_interpelaciones_no_pide_prensa():
    texto = (Path(PROMPTS_DIR) / "workflow_interpelaciones.md").read_text(encoding="utf-8")
    assert "noticias_prensa" not in texto and "según prensa" not in texto


def test_system_base_no_fija_el_periodo_2021():
    assert "periodo parlamentario aún es `2021`" not in system_con_fecha(HOY)


def test_workflows_de_listados_siguen_pidiendo_tablas():
    for nombre in ("buscar_proyectos", "fetch_expediente", "fetch_comisiones"):
        assert "|---" in WORKFLOWS[nombre], nombre


def test_resumen_semanal_se_renderiza():
    r = resumen_con_fechas(HOY, DESDE)
    assert HOY in r and DESDE in r and "{" not in r.split("```")[0][:200]
