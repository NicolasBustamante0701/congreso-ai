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


async def test_settings_ai_informa_estado(client):
    from unittest.mock import AsyncMock, patch

    import routers.settings as settings
    settings._PRUEBA_CACHE.clear()
    settings._PRUEBA_EN_CURSO.clear()
    with patch("routers.settings._probar_key", AsyncMock(return_value=(False, "bloqueada"))), \
         patch("routers.settings.LLM_API_KEY", "k"):
        r = await client.get("/settings/ai")             # al instante, sin esperar la prueba
        assert r.status_code == 200
        assert set(r.json()) == {"provider", "ready", "funciona", "error", "transcripcion"}
        r = await client.get("/settings/ai?probar=1")    # Ajustes espera el resultado real
        assert r.json()["funciona"] is False and r.json()["error"] == "bloqueada"


async def test_settings_ai_test_rechaza_proveedor_o_key_vacios(client):
    r = await client.post("/settings/ai/test", json={"provider": "inventado", "api_key": "x"})
    assert r.json() == {"ok": False, "error": "Proveedor desconocido."}
    r = await client.post("/settings/ai/test", json={"provider": "gemini", "api_key": "  "})
    assert r.json()["ok"] is False


async def test_settings_ai_test_traduce_key_invalida(client):
    from unittest.mock import AsyncMock, patch

    with patch("routers.settings.AsyncOpenAI") as cls:
        inst = cls.return_value
        inst.chat.completions.create = AsyncMock(side_effect=Exception(
            "Error code: 400 - [{'error': {'message': 'Please pass a valid API key'}}]"))
        inst.close = AsyncMock()
        r = await client.post("/settings/ai/test", json={"provider": "gemini", "api_key": "mala"})
    body = r.json()
    assert body["ok"] is False and "no es válida para Gemini" in body["error"]


async def test_settings_ai_test_avisa_proyecto_bloqueado(client):
    # 18/10/2026: la lista de modelos respondía bien con un proyecto bloqueado
    # por Google y la pantalla decía "Funcionando"; la generación da 403.
    from unittest.mock import AsyncMock, patch

    with patch("routers.settings.AsyncOpenAI") as cls:
        inst = cls.return_value
        inst.chat.completions.create = AsyncMock(side_effect=Exception(
            "Error code: 403 - [{'error': {'code': 403, 'message': 'Your project has been denied "
            "access. Please contact support.', 'status': 'PERMISSION_DENIED'}}]"))
        inst.close = AsyncMock()
        r = await client.post("/settings/ai/test", json={"provider": "gemini", "api_key": "bloqueada"})
    body = r.json()
    assert body["ok"] is False and "bloqueó el proyecto" in body["error"]


# ── /inicio y /agenda (pantallas del rediseño) ────────────────────────────────
@pytest.fixture(autouse=False)
def sin_cache_vistas():
    import routers.pages as pages
    pages._CACHE_VISTAS.clear()
    yield
    pages._CACHE_VISTAS.clear()


@pytest.mark.asyncio
async def test_inicio_informa_la_fuente_que_fallo(client, sin_cache_vistas):
    agenda = {"sesiones": [
        {"fecha": "01/01/2099", "hora": "9:00 AM", "tema": "Futura", "camara": "Senado"},
        {"fecha": "01/01/2000", "hora": "9:00 AM", "tema": "Pasada", "camara": "Senado"},
        {"fecha": "", "hora": "", "tema": "Sin fecha", "camara": "Senado"},
    ]}
    with patch("routers.pages.fetch_agenda_camaras", AsyncMock(return_value=agenda)), \
         patch("routers.pages.fetch_proyectos", AsyncMock(side_effect=RuntimeError("SPLEY caído"))):
        r = (await client.get("/inicio")).json()
    assert [s["tema"] for s in r["agenda"]] == ["Futura"]   # ni pasadas ni sin fecha
    assert r["proyectos"] is None
    assert r["fallas"] == ["SPLEY (proyectos de ley)"]


@pytest.mark.asyncio
async def test_agenda_ordena_y_no_inventa_fechas(client, sin_cache_vistas):
    agenda = {"sesiones": [
        {"fecha": "02/01/2099", "hora": "3:00 PM", "tema": "B", "camara": "Diputados"},
        {"fecha": "02/01/2099", "hora": "9:00 AM", "tema": "A", "camara": "Senado"},
        {"fecha": "??", "hora": "", "tema": "X", "camara": "Senado"},
    ]}
    with patch("routers.pages.fetch_agenda_camaras", AsyncMock(return_value=agenda)):
        r = (await client.get("/agenda", params={"dias": 14})).json()
    assert [s["tema"] for s in r["sesiones"]] == ["A", "B"]



async def test_chat_pasa_la_extension_al_orquestador(client):
    from unittest.mock import patch

    recibido = {}

    class Falso:
        def __init__(self, messages, extension=None):
            recibido["extension"] = extension

        async def run(self):
            yield "data: [DONE]\n\n"

    with patch("routers.chat.ChatOrchestrator", Falso), patch("routers.chat.LLM_API_KEY", "k"):
        await client.post("/chat", json={"messages": [{"role": "user", "content": "hola"}], "extension": "corta"})
    assert recibido["extension"] == "corta"
