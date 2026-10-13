"""
Transcripciones guardadas: persistencia, cobertura, búsqueda y preguntas.

El LLM está SIMULADO (fake_stream): estos tests prueban qué fragmentos se le
mandan y qué se guarda, no la calidad de la respuesta de un modelo real.
"""
import json
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from services import transcripts as tr

VID = "abc123"


@pytest.fixture(autouse=True)
def db_temporal(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "DB_PATH", tmp_path / "diana.db")


def _cargar(textos, vid=VID, en_vivo=False):
    tr.iniciar_sesion(vid, "Sesión de prueba", en_vivo=en_vivo)
    for i, t in enumerate(textos):
        tr.guardar_ventana(vid, i * 10, i * 10 + 10, t, "ok" if t else "silencio")


# ── tiempos ─────────────────────────────────────────────────────────────────

def test_fmt_ts_lleva_horas():
    assert tr.fmt_ts(0) == "00:00:00"
    assert tr.fmt_ts(7 * 3600 + 5) == "07:00:05"     # antes salía "420:05"


def test_rango_en_pregunta():
    assert tr.rango_en_pregunta("resumí entre 00:24:10 y 00:26:05") == (1450, 1565)
    assert tr.rango_en_pregunta("de 12:00 a 10:30") == (630, 720)   # MM:SS, y lo ordena
    assert tr.rango_en_pregunta("¿qué dijo sobre el agua?") is None


# ── persistencia ────────────────────────────────────────────────────────────

def test_guarda_y_recupera_en_orden():
    _cargar(["uno", "", "tres"])
    d = tr.obtener(VID)
    assert [s["texto"] for s in d["segmentos"]] == ["uno", "tres"]   # el silencio no es texto
    assert d["cobertura"]["silencio"] == 1 and d["cobertura"]["huecos"] == []
    assert d["cobertura"]["hasta"] == "00:00:30"


def test_reprocesar_un_tramo_no_duplica():
    _cargar(["a", "b", "c"])
    tr.guardar_ventana(VID, 5, 15, "b corregido", "ok")    # pisa 0-10 y 10-20
    textos = [s["texto"] for s in tr.obtener(VID)["segmentos"]]
    assert textos == ["b corregido", "c"]


def test_cobertura_marca_huecos_y_errores():
    tr.iniciar_sesion(VID)
    tr.guardar_ventana(VID, 0, 10, "hola", "ok")
    tr.guardar_ventana(VID, 60, 70, "", "error")
    c = tr.obtener(VID)["cobertura"]
    assert c["huecos"] == [["00:00:10", "00:01:00"]]
    assert c["errores"] == ["00:01:00"]


def test_offset_para_streams_en_vivo():
    _cargar(["a", "b"], en_vivo=True)
    assert tr.offset_captura(VID) == 20


def test_borrar_quita_todo():
    _cargar(["presupuesto"])
    assert tr.borrar(VID)
    assert tr.obtener(VID) is None and tr.buscar("presupuesto") == []


# ── búsqueda ────────────────────────────────────────────────────────────────

def test_busqueda_sin_tildes_y_por_raiz():
    _cargar(["se debatió el presupuesto de educación", "otro tema", "ley de aguas"])
    hits = tr.buscar("presupuestal educacion")
    assert hits and hits[0]["start_s"] == 0
    assert tr.buscar("agua")[0]["texto"] == "ley de aguas"


def test_fragmentos_suman_contexto_vecino():
    _cargar(["intro", "antes", "el ministro habló del canon minero", "después", "fin"])
    frag, modo = tr.fragmentos_para_pregunta(VID, "¿qué se dijo del canon?")
    assert modo == "busqueda"
    assert [f["texto"] for f in frag] == ["antes", "el ministro habló del canon minero", "después"]


def test_fragmentos_por_rango_de_tiempo():
    _cargar(["a", "b", "c", "d"])
    frag, modo = tr.fragmentos_para_pregunta(VID, "resumí entre 00:00:10 y 00:00:30")
    assert modo == "rango" and [f["texto"] for f in frag] == ["b", "c"]


def test_sesion_larga_sin_coincidencias_no_manda_nada(monkeypatch):
    monkeypatch.setattr(tr, "MAX_CHARS_TRANSCRIPT_COMPLETO", 10)
    _cargar(["texto largo uno", "texto largo dos"])
    assert tr.fragmentos_para_pregunta(VID, "zzzz inexistente") == ([], "nada")


# ── endpoints (LLM simulado) ───────────────────────────────────────────────

@pytest_asyncio.fixture
async def api():
    from server import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def _textos(resp_text):
    return "".join(json.loads(linea[6:]).get("text", "")
                   for linea in resp_text.splitlines()
                   if linea.startswith("data: {"))


async def test_preguntar_manda_solo_los_fragmentos(api):
    _cargar(["intro", "nada", "se aprobó el dictamen del proyecto 123", "cierre", "x", "y", "z"])
    visto = {}

    async def fake_stream(messages, **kw):
        visto["prompt"] = messages[-1]["content"]
        yield ("text", "Se aprobó [00:00:20].")

    with patch("routers.transcripts.LLM_API_KEY", "x"), \
         patch("routers.transcripts.llm.stream", fake_stream):
        r = await api.post(f"/transcripts/{VID}/preguntar", json={"pregunta": "¿qué pasó con el dictamen?"})
    assert "Se aprobó [00:00:20]." in _textos(r.text)
    assert "[00:00:20] se aprobó el dictamen" in visto["prompt"]
    assert "intro" not in visto["prompt"] and "] z" not in visto["prompt"]


async def test_preguntar_sin_evidencia_no_llama_al_modelo(api, monkeypatch):
    monkeypatch.setattr(tr, "MAX_CHARS_TRANSCRIPT_COMPLETO", 5)
    _cargar(["algo de texto", "más texto"])

    async def no_deberia(*a, **k):
        raise AssertionError("no hay evidencia: no se le pregunta al modelo")
        yield

    with patch("routers.transcripts.LLM_API_KEY", "x"), \
         patch("routers.transcripts.llm.stream", no_deberia):
        r = await api.post(f"/transcripts/{VID}/preguntar", json={"pregunta": "zzzz"})
    assert "No aparece en lo transcrito" in _textos(r.text)


async def test_informe_se_guarda(api):
    _cargar(["tema uno", "tema dos"])

    async def fake_stream(messages, **kw):
        yield ("text", "### Resumen ejecutivo\nSe trató el tema uno [00:00:00].")

    with patch("routers.transcripts.LLM_API_KEY", "x"), \
         patch("routers.transcripts.llm.stream", fake_stream):
        await api.post(f"/transcripts/{VID}/informe", json={})
    assert tr.listar_sesiones()[0]["tiene_informe"] is True


async def test_listar_y_404(api):
    _cargar(["a"])
    assert (await api.get("/transcripts")).json()["sesiones"][0]["video_id"] == VID
    assert (await api.get("/transcripts/noexiste")).status_code == 404
