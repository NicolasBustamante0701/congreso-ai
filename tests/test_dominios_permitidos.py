"""
La lista blanca de dominios del scraper.

Varios enlaces que el scraper sigue no son constantes: salen del HTML o del
JSON del propio Congreso (adjuntos de expediente, síntesis de agenda, agenda
del Pleno). Estos tests fijan qué se puede seguir y qué no, con foco en los
dominios que *se parecen* al oficial, que son los que un chequeo hecho con
`in` o `startswith` dejaría pasar.
"""
import pytest

from scraper import _url_permitida

OFICIALES = [
    "https://www.congreso.gob.pe/home/",
    "https://api.congreso.gob.pe/spley-portal-service/proyecto-ley",
    "https://wb2server.congreso.gob.pe/spley-portal/#/expediente",
    "https://senado.congreso.gob.pe/",
    "https://diputados.congreso.gob.pe/",
    "https://comunicaciones.congreso.gob.pe/agenda/2026/9/7/",
    "https://www2.congreso.gob.pe/Sicr/TraDocEstProc/",
    "https://congreso.gob.pe/",            # dominio pelado, sin subdominio
    "http://www.congreso.gob.pe/algo",     # http tambien vale: el sitio mezcla
]

EXTERNOS = [
    "https://google.com/",
    "https://www.youtube.com/watch?v=abc",
    "https://congreso.gob.pe.evil.com/x",   # sufijo falso: el host real es evil.com
    "https://notcongreso.gob.pe/x",         # termina igual pero no es subdominio
    "https://congreso.gob.pe.br/x",
    "https://evil.com/?u=https://congreso.gob.pe",  # el oficial va en el query
    "https://elcomercio.pe/politica/nota",
    "",
    "no-es-una-url",
    "javascript:alert(1)",
]


@pytest.mark.parametrize("url", OFICIALES)
def test_deja_pasar_los_del_congreso(url):
    assert _url_permitida(url), f"debería permitir {url}"


@pytest.mark.parametrize("url", EXTERNOS)
def test_bloquea_todo_lo_demas(url):
    assert not _url_permitida(url), f"NO debería permitir {url}"


# ── El candado a nivel de cliente y en los endpoints de PDF ────────────────
# Sin red: el hook corta antes de abrir conexión, y los endpoints rechazan la
# URL antes de llamar a download().
import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

import scraper  # noqa: E402


async def test_el_cliente_del_scraper_no_sale_del_congreso():
    async with scraper._client() as c:
        with pytest.raises(scraper.DominioNoPermitido):
            await c.get("http://127.0.0.1:1/interno")


async def test_el_hook_corta_una_redireccion_hacia_afuera():
    # Un servidor "oficial" falso que redirige a un dominio externo: la
    # segunda request (la del redirect) tiene que cortarse.
    def handler(request):
        if request.url.host.endswith("congreso.gob.pe"):
            return httpx.Response(302, headers={"location": "https://evil.com/x"})
        return httpx.Response(200, text="no debería llegar acá")

    c = scraper._client()
    c._transport = httpx.MockTransport(handler)
    async with c:
        with pytest.raises(scraper.DominioNoPermitido):
            await c.get("https://www.congreso.gob.pe/redirige")


@pytest_asyncio.fixture
async def api():
    from server import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.parametrize("url", ["http://127.0.0.1:8732/status", "https://evil.com/x.pdf",
                                 "file:///etc/passwd", "http://169.254.169.254/latest"])
async def test_pdf_thumbnail_rechaza_urls_externas(api, url):
    r = await api.get("/pdf-thumbnail", params={"url": url})
    assert r.status_code == 400


async def test_load_pdf_url_rechaza_urls_externas(api):
    r = await api.post("/load-pdf-url", json={"url": "http://192.168.0.1/admin.pdf"})
    assert r.status_code == 400
    assert r.json()["ok"] is False
