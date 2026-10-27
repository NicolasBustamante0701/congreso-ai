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

from services import db
from services import transcripts as tr

VID = "abc123"


@pytest.fixture(autouse=True)
def db_temporal(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "diana.db")
    # Las capturas del video salen a YouTube: en los tests, sin placas.
    from services import placas
    monkeypatch.setattr(placas, "capturar", lambda *a, **k: [])


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


def test_cobertura_aclara_si_los_tiempos_son_del_video():
    from routers.transcripts import _texto_cobertura
    _cargar(["a"], en_vivo=False)
    assert "VIDEO GRABADO" in _texto_cobertura(tr.obtener(VID))
    tr.iniciar_sesion("vivo1", "🔴 EN VIVO", en_vivo=True)
    tr.guardar_ventana("vivo1", 0, 10, "x", "ok")
    assert "de captura" in _texto_cobertura(tr.obtener("vivo1"))


# ── preguntas sobre una persona ─────────────────────────────────────────────

@pytest.mark.parametrize("pregunta,nombre,cargo", [
    ("¿Qué dijo el congresista Pérez Gómez?", "Pérez Gómez", "congresista"),
    ("cuál fue la posición de la ministra Ana Torres", "Ana Torres", "ministra"),
    ("¿qué dijo el ministro?", "", "ministro"),
    ("¿Qué opinó Flor Meza sobre el canon?", "Flor Meza canon", ""),
])
def test_persona_en_pregunta(pregunta, nombre, cargo):
    p = tr.persona_en_pregunta(pregunta)
    assert p is not None and p["cargo"] == cargo
    assert p["nombre"].startswith(nombre.split(" canon")[0])


@pytest.mark.parametrize("pregunta", ["¿qué se dijo del canon?", "resume la sesión", "¿hubo votación?"])
def test_no_es_pregunta_de_persona(pregunta):
    assert tr.persona_en_pregunta(pregunta) is None


def test_fragmentos_de_persona_traen_su_intervencion(monkeypatch):
    monkeypatch.setattr(tr, "VENTANAS_INTERVENCION", 3)
    _cargar(["apertura", "tiene la palabra el congresista Pérez",
             "estoy en contra del dictamen", "porque recorta el presupuesto",
             "y no fue consultado", "otro tema", "cierre"])
    frag = tr.fragmentos_de_persona(VID, tr.persona_en_pregunta("¿Qué dijo el congresista Pérez?"))
    textos = [f["texto"] for f in frag]
    assert textos[:5] == ["apertura", "tiene la palabra el congresista Pérez",
                          "estoy en contra del dictamen", "porque recorta el presupuesto",
                          "y no fue consultado"]
    assert "cierre" not in textos


async def test_preguntar_por_persona_ausente_lo_dice_y_resalta(api, monkeypatch):
    monkeypatch.setattr(tr, "MAX_CHARS_TRANSCRIPT_COMPLETO", 5)
    _cargar(["el ministro expuso el presupuesto", "más texto largo"])

    async def no_deberia(*a, **k):
        raise AssertionError("sin evidencia no se le pregunta al modelo")
        yield

    with patch("routers.transcripts.LLM_API_KEY", "x"), \
         patch("routers.transcripts.llm.stream", no_deberia):
        r = await api.post(f"/transcripts/{VID}/preguntar",
                           json={"pregunta": "¿Qué dijo la congresista Huamán?"})
    assert "No hay intervenciones de congresista [[Huamán]]" in _textos(r.text)


# ── índice de oradores ──────────────────────────────────────────────────────

from services import oradores  # noqa: E402

_SESION_ORADORES = [
    "se abre la sesión", "tiene la palabra el congresista Huamán",
    "gracias presidente, estoy en contra del dictamen", "porque recorta el presupuesto regional",
    "gracias congresista Huamán, tiene la palabra la ministra Torres",
    "señor presidente, el ejecutivo apoya la propuesta", "cierre de la sesión",
]


def test_parsear_descarta_nombres_que_no_estan_en_el_texto():
    _cargar(_SESION_ORADORES)
    segs = tr.obtener(VID)["segmentos"]
    resp = ("00:00:10 | Huamán | congresista | tiene la palabra\n"
            "00:00:40 | Torres | ministra | tiene la palabra la ministra\n"
            "00:00:20 | Pedro Inventado | congresista | nada\n"
            "00:00:30 | Huamán | congresista | la mesa liderada por el congresista Huamán\n"
            "00:00:50 |  |  | señor presidente, el ejecutivo\n"
            "basura sin formato\n"
            "09:00:00 | Huamán | congresista | fuera de rango")
    turnos = oradores.parsear(resp, segs)
    # La mención ("liderada por") no es un turno; el orador sin nombre sí (corta al anterior).
    assert [(t["start_s"], t["nombre"]) for t in turnos] == [(10, "Huamán"), (40, "Torres"), (50, "")]


def test_buscar_tolera_minusculas_y_nombre_mal_escrito():
    turnos = [{"start_s": 10, "end_s": 40, "nombre": "Huamán", "cargo": "congresista", "pista": ""},
              {"start_s": 40, "end_s": 70, "nombre": "Torres", "cargo": "ministra", "pista": ""}]
    assert [t["nombre"] for t in oradores.buscar(turnos, "que dijo huaman")] == ["Huamán"]
    assert [t["nombre"] for t in oradores.buscar(turnos, "qué dijo Guamán")] == ["Huamán"]
    assert [t["nombre"] for t in oradores.buscar(turnos, "¿qué dijo la ministra?", cargo="ministra")] == ["Torres"]
    assert oradores.buscar(turnos, "qué dijo Rodríguez") == []


async def test_preguntar_usa_el_indice_de_oradores(api):
    _cargar(_SESION_ORADORES)
    visto = {}

    async def fake_stream(messages, **kw):
        if messages[0]["content"] == oradores.PROMPT:
            yield ("text", "00:00:10 | Huamán | congresista | tiene la palabra el congresista Huamán\n"
                           "00:00:40 | Torres | ministra | tiene la palabra la ministra Torres")
            return
        visto["prompt"] = messages[-1]["content"]
        yield ("text", "[[Huamán]] está en contra [00:00:20].")

    with patch("routers.transcripts.LLM_API_KEY", "x"), \
         patch("routers.transcripts.llm.stream", fake_stream):
        r = await api.post(f"/transcripts/{VID}/preguntar",
                           json={"pregunta": "que dijo guaman"})     # minúsculas y mal escrito
    assert "[[Huamán]] está en contra" in _textos(r.text)
    assert "ÍNDICE DE ORADORES" in visto["prompt"]
    assert "estoy en contra del dictamen" in visto["prompt"]
    assert "el ejecutivo apoya" not in visto["prompt"]      # eso es de la ministra
    # El índice quedó guardado: la segunda pregunta no lo vuelve a armar.
    assert len(oradores.listar(VID)) == 2


# ── placas leídas del video ─────────────────────────────────────────────────

from services import placas  # noqa: E402


def _obs(texto, x=0.5, y=0.3, w=0.2, h=0.04, conf=0.9):
    return {"texto": texto, "conf": conf, "x": x, "y": y, "w": w, "h": h}


def test_elegir_placa_toma_la_principal_y_su_cargo():
    obs = [_obs("CÉSAR ALFONSO LUNA VICTORIA", x=0.53, y=0.22, w=0.22, h=0.04),
           _obs("Superintendente Nacional de Aduanas y de", x=0.55, y=0.19, w=0.18, h=0.015),
           _obs("CLAUDIA FREYRE SANTA MARIA", x=0.30, y=0.20, w=0.10, h=0.02),   # placa de al lado, chica
           _obs("CONGRESO", x=0.88, y=0.9), _obs("DE MANANT", x=0.0, y=0.2),
           _obs("San Mateo", x=0.0, y=0.25)]
    p = placas.elegir_placa(obs)
    assert p["nombre"] == "CÉSAR ALFONSO LUNA VICTORIA"
    assert p["cargo"].startswith("Superintendente Nacional")


def test_elegir_placa_sin_placa():
    assert placas.elegir_placa([_obs("CONGRESO TV DIGITAL"), _obs("PATRIARI")]) is None


async def test_indice_usa_las_placas_del_video(monkeypatch):
    # El texto no nombra a nadie: solo la placa dice quién habla.
    _cargar(["buenas tardes señor presidente", "la recaudación subió este año", "eso es todo"])
    monkeypatch.setattr(placas, "disponible", lambda: True)
    monkeypatch.setattr(placas, "capturar", lambda vid, a, b: [
        {"start_s": 0, "nombre": "CESAR LUNA VICTORIA", "cargo": "Superintendente SUNAT"}])
    visto = {}

    async def fake_stream(messages, **kw):
        visto["entrada"] = messages[-1]["content"]
        yield ("text", "00:00:00 | CESAR LUNA VICTORIA | Superintendente SUNAT | placa en pantalla")

    with patch("services.oradores.llm.stream", fake_stream):
        async for _ in oradores.asegurar(VID):
            pass
    assert "(EN PANTALLA: placa «CESAR LUNA VICTORIA» — Superintendente SUNAT)" in visto["entrada"]
    assert [t["nombre"] for t in oradores.listar(VID)] == ["CESAR LUNA VICTORIA"]
    assert oradores.buscar(oradores.listar(VID), "qué dijo luna victoria")
