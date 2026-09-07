"""
Tests de humo para los 4 endpoints críticos.
Requieren: pytest httpx pytest-asyncio  (asyncio_mode = auto en pytest.ini)
"""
import os
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Evitar que el servidor necesite GROQ_API_KEY real para arrancar
os.environ.setdefault("GROQ_API_KEY", "test-key-mock")

from server import app  # noqa: E402


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ── /status ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_status_ok(client):
    r = await client.get("/status")
    assert r.status_code == 200
    data = r.json()
    assert "ready" in data
    assert isinstance(data["ready"], bool)


# ── / (index.html) ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_root_returns_html(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<html" in r.text.lower()


# ── /congreso-proyectos ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_congreso_proyectos_shape(client):
    fake_data = {
        "items": [
            {
                "numero": "1234/2024",
                "sumilla": "Proyecto de prueba",
                "enlace": "https://wb2server.congreso.gob.pe/spley-portal/#/expediente/1234",
            }
        ]
    }
    with patch("routers.pdfs.fetch_proyectos", new=AsyncMock(return_value=fake_data)):
        r = await client.get("/congreso-proyectos")
    assert r.status_code == 200
    data = r.json()
    # El endpoint devuelve {"pdfs": [...]} (misma clave que /congreso-pdfs)
    assert "pdfs" in data
    assert isinstance(data["pdfs"], list)
    assert data["pdfs"][0]["tipo"] == "Proyecto de Ley"


# ── /congreso-pdfs ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_congreso_pdfs_shape(client):
    fake_destacados = {
        "camaras": {
            "Congreso": {
                "destacados": [
                    {"titulo": "Informe PDF", "enlace": "https://example.com/doc.pdf"},
                ],
                "citaciones": [],
            },
        },
    }
    with patch("routers.pdfs.fetch_destacados", new=AsyncMock(return_value=fake_destacados)):
        r = await client.get("/congreso-pdfs")
    assert r.status_code == 200
    data = r.json()
    assert "pdfs" in data
    assert isinstance(data["pdfs"], list)


# ── /dashboard-metrics ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_dashboard_usa_el_total_real_no_el_truncado(client):
    """
    La tarjeta "proyectos ingresados" tiene que mostrar cuántos hay, no cuántos
    devolvió el scraper. fetch_proyectos recorta por `limit` (20 por defecto) y
    deja el número real en `total_disponible`; leyendo `total` la pantalla de
    inicio decía 20 cuando eran 48.
    """
    import routers.pages as pages

    pages._METRICS_CACHE["data"] = None  # la caché de 5 min taparía el cambio
    recortado = {"total": 20, "total_disponible": 48, "truncado": True, "items": []}
    with patch("routers.pages.fetch_proyectos", new=AsyncMock(return_value=recortado)), \
         patch("routers.pages.fetch_agenda_camaras", new=AsyncMock(return_value={"sin_datos": True})), \
         patch("routers.pages.fetch_destacados", new=AsyncMock(return_value={"camaras": {}})):
        r = await client.get("/dashboard-metrics")

    pages._METRICS_CACHE["data"] = None
    assert r.status_code == 200
    assert r.json()["proyectos_ingresados"]["total"] == 48


@pytest.mark.asyncio
async def test_dashboard_sin_recorte_usa_total(client):
    """Sin truncado no hay `total_disponible`: entonces `total` ya es el real."""
    import routers.pages as pages

    pages._METRICS_CACHE["data"] = None
    completo = {"total": 7, "items": []}
    with patch("routers.pages.fetch_proyectos", new=AsyncMock(return_value=completo)), \
         patch("routers.pages.fetch_agenda_camaras", new=AsyncMock(return_value={"sin_datos": True})), \
         patch("routers.pages.fetch_destacados", new=AsyncMock(return_value={"camaras": {}})):
        r = await client.get("/dashboard-metrics")

    pages._METRICS_CACHE["data"] = None
    assert r.json()["proyectos_ingresados"]["total"] == 7


# ── /expediente ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_expediente_view_devuelve_html(client):
    r = await client.get("/expediente-view", params={"numero": "00088-2026-2031-CD"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # La vista tiene que traer sus dos columnas: la ficha y el chat del proyecto.
    assert 'id="exp-ficha"' in r.text
    assert 'id="exp-chat-log"' in r.text


@pytest.mark.asyncio
async def test_expediente_pasa_el_numero_al_scraper(client):
    fake = {
        "numero": "00088-2026-2031-CD",
        "titulo": "PROPOSICIÓN LEGISLATIVA DE PRUEBA",
        "sumilla": "PROPONE ALGO",
        "estado": "PRESENTADO",
        "seguimiento": [],
        "proyectos_acumulados": [],
        "documentacion_anexa": [],
        "opinion_ciudadana": {},
        "todos_los_adjuntos": [],
    }
    with patch("routers.expediente.fetch_expediente",
               new=AsyncMock(return_value=fake)) as mock:
        r = await client.get("/expediente", params={"numero": "00088-2026-2031-CD"})

    assert r.status_code == 200
    assert r.json()["numero"] == "00088-2026-2031-CD"
    mock.assert_awaited_once_with("00088-2026-2031-CD")


@pytest.mark.asyncio
async def test_expediente_con_numero_vacio_no_llama_al_scraper(client):
    with patch("routers.expediente.fetch_expediente", new=AsyncMock()) as mock:
        r = await client.get("/expediente", params={"numero": "   "})

    assert r.status_code == 200
    assert "error" in r.json()
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_expediente_devuelve_error_si_el_scraper_revienta(client):
    # Que SPLEY se caiga no puede tumbar el endpoint: el frontend espera JSON
    # con "error" para poder mostrar el aviso en la ficha.
    with patch("routers.expediente.fetch_expediente",
               new=AsyncMock(side_effect=RuntimeError("SPLEY caído"))):
        r = await client.get("/expediente", params={"numero": "00088-2026-2031-CD"})

    assert r.status_code == 200
    assert "error" in r.json()
