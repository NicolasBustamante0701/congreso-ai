"""buscar_mociones: mociones de cualquier tipo, sin que las de saludo tapen a las demás."""
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

import scraper

HOY = datetime(2026, 10, 19, 10, 0)


def _m(numero, tipo, fecha):
    return {"numero": numero, "tipo": tipo, "fecha": fecha, "estado": "PRESENTADO",
            "sumilla": "x", "autores": "A; B; C; D; E", "camara": "Diputados", "enlace": "e"}


MOCIONES = [
    _m("1", "Mociones de Saludo", "18/10/2026"),
    _m("2", "MOCIÓN DE SALUDO", "17/10/2026"),
    _m("3", "PEDIDO DE CONFORMACIÓN DE COMISIÓN ESPECIAL", "16/10/2026"),
    _m("4", "Conformación de Comisiones Especiales", "01/10/2026"),
    _m("5", "DE INTERÉS NACIONAL", "15/10/2026"),
]


@pytest.fixture
def fuente():
    with patch("scraper.fetch_mociones", new=AsyncMock(return_value=(list(map(dict, MOCIONES)), 5))), \
         patch("scraper.ahora_lima", return_value=HOY):
        yield


@pytest.mark.parametrize("pedido,tipo,esperado", [
    ("comisión especial", "Conformación de Comisiones Especiales", True),
    ("comisión especial", "PEDIDO DE CONFORMACIÓN DE COMISIÓN ESPECIAL", True),
    ("interés nacional", "Pedidos de Declaración de necesidad pública o interes nacional", True),
    ("saludo", "DE INTERÉS NACIONAL", False),
])
def test_tipo_coincide(pedido, tipo, esperado):
    assert scraper._tipo_coincide(pedido, tipo) is esperado


async def test_sin_tipo_deja_afuera_los_saludos_y_los_cuenta(fuente):
    r = await scraper.buscar_mociones()
    assert [m["numero"] for m in r["mociones"]] == ["3", "4", "5"]
    assert r["mociones_de_saludo"] == 2 and r["total"] == 3
    assert r["mociones"][0]["autores"].endswith("y 2 más")


async def test_filtra_por_tipo_y_por_dias(fuente):
    r = await scraper.buscar_mociones(tipo="comisión especial", dias=7)
    assert [m["numero"] for m in r["mociones"]] == ["3"]
    r = await scraper.buscar_mociones(tipo="saludo")
    assert [m["numero"] for m in r["mociones"]] == ["1", "2"] and "mociones_de_saludo" not in r


async def test_sin_resultados_lo_dice(fuente):
    r = await scraper.buscar_mociones(tipo="vacancia")
    assert r["sin_datos"] and "vacancia" in r["mensaje"]


def test_la_herramienta_esta_registrada_completa():
    from services.prompt_registry import ROUTER_PROMPT, WORKFLOWS
    from services.tools import STATUS_LABELS, TOOL_MAP, TOOLS
    assert "buscar_mociones" in {t["function"]["name"] for t in TOOLS}
    assert {"buscar_mociones"} <= TOOL_MAP.keys() & STATUS_LABELS.keys() & WORKFLOWS.keys()
    assert "buscar_mociones" in ROUTER_PROMPT


def test_router_no_inventa_filtros():
    from services.orchestrator import ChatOrchestrator
    orch = ChatOrchestrator([{"role": "user", "content": "Mociones presentadas esta semana"}])
    orch._analyze()
    assert orch._ajustar_args("buscar_mociones", {"tipo": "saludo", "texto": "Interior"}) == {"dias": 7}
    orch = ChatOrchestrator([{"role": "user", "content": "mociones para crear comisiones especiales"}])
    orch._analyze()
    assert orch._ajustar_args("buscar_mociones", {"tipo": "comisión especial"}) == {"tipo": "comisión especial"}


def test_autores_de_mocion_en_orden_natural_con_bancada():
    crudo = " (P)Vidaurre Floridas, César Manuel (Fuerza Popular); Rocha Gallegos, Roxana María (Renovación Popular)"
    assert scraper._autores_mocion(crudo) == (
        "César Manuel Vidaurre Floridas (Fuerza Popular); Roxana María Rocha Gallegos (Renovación Popular)")
