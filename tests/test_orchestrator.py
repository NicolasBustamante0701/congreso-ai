"""
Tests del ChatOrchestrator.

Toda la lógica de decisión (qué fase correr, qué historial mandar, qué system
prompt armar) es pura y se testea sin tocar Groq ni la red.
"""
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.orchestrator import (
    DEFAULT_DIAS_PROYECTOS,
    MAX_FILAS_AGRUPADO,
    MAX_FILAS_LISTADO,
    RESUMEN_DIAS,
    RESUMEN_TOOLS,
    ChatOrchestrator,
    _agenda_pleno_de_la_semana,
    _compactar_para_agrupado,
    _cuadro_por_temas,
    _detecta_proyectos_por_dias,
    _mociones_de_la_semana,
    _parsear_clasificacion,
    _separar_por_semana,
)
from services.prompt_registry import SYSTEM_MINI, resumen_con_fechas


def make(messages, **kwargs):
    """Orquestador ya analizado, con un cliente falso."""
    orch = ChatOrchestrator(messages)
    orch._analyze()
    for k, v in kwargs.items():
        setattr(orch, k, v)
    return orch


def user(content):
    return {"role": "user", "content": content}


def assistant(content):
    return {"role": "assistant", "content": content}


# ── _analyze ─────────────────────────────────────────────────────────────────

def test_detecta_resumen_semanal_con_sector():
    orch = make([user("__RESUMEN_SEMANAL__: mineria")])
    assert orch.is_resumen
    assert orch.sector == "mineria"
    # El historial se reemplaza por una instrucción sintética.
    assert len(orch.conversation) == 1
    assert "sector mineria" in orch.conversation[0]["content"]


def test_resumen_general_no_menciona_sector():
    orch = make([user("__RESUMEN_SEMANAL__: general")])
    assert orch.is_resumen
    assert "Enfoca el análisis" not in orch.conversation[0]["content"]


def test_detecta_documento_en_contexto():
    orch = make([
        user("He cargado el documento PL-1234.pdf"),
        user("resume la fórmula legal"),
    ])
    assert orch.doc_en_contexto
    assert orch.analizar_documento
    assert orch._short_circuit


def test_documento_con_pregunta_corta_tambien_analiza():
    """Una pregunta breve tras cargar un doc se asume sobre ese doc."""
    orch = make([user("He cargado el documento X.pdf"), user("¿y esto?")])
    assert orch.analizar_documento


def test_documento_con_pregunta_larga_no_relacionada_no_analiza():
    pregunta = (
        "cuales son los proyectos de ley sobre transporte publico presentados "
        "por la comision de economia en el ultimo periodo parlamentario"
    )
    orch = make([user("He cargado el documento X.pdf"), user(pregunta)])
    assert not orch.analizar_documento


def test_detecta_link_de_sesion():
    orch = make([user("analiza https://youtu.be/abcdefghijk")])
    assert orch.has_sesion
    assert orch._short_circuit


def test_resumen_semanal_no_hace_short_circuit_por_sesion():
    """El resumen semanal manda aunque el texto mencione un transcript."""
    orch = make([user("__RESUMEN_SEMANAL__: general transcript")])
    assert orch.is_resumen
    assert not orch._short_circuit


def test_pregunta_normal_no_hace_short_circuit():
    orch = make([user("qué proyectos de ley hay sobre salud")])
    assert not orch._short_circuit
    assert not orch.doc_en_contexto


def test_detecta_expediente_en_contexto():
    orch = make([
        user("dame el expediente 14864"),
        assistant("## FICHA DEL PROYECTO\n...\n## MI LECTURA"),
        user("qué comisiones lo vieron"),
    ])
    assert orch.has_expediente_en_contexto


# ── Fase 1: recorte del historial al router ──────────────────────────────────

def test_router_recorta_historial_si_hay_documento():
    """Un PDF en contexto son miles de tokens que no ayudan a elegir tool."""
    orch = make([user("He cargado el documento " + "x" * 5000), user("hola")])
    msgs = orch._router_messages()
    assert len(msgs) == 2
    assert msgs[1]["content"] == "hola"


def test_router_resume_expediente_en_vez_de_reenviarlo():
    orch = make([
        user("expediente 14864"),
        assistant("FICHA DEL PROYECTO " + "y" * 9000),
        user("y qué comisiones?"),
    ])
    msgs = orch._router_messages()
    assert len(msgs) == 2
    assert "responder_directo" in msgs[1]["content"]
    assert "y qué comisiones?" in msgs[1]["content"]
    assert "y" * 9000 not in msgs[1]["content"]


def test_router_usa_ultimos_4_mensajes_por_defecto():
    msgs_in = [user(f"m{i}") for i in range(10)]
    orch = make(msgs_in)
    msgs = orch._router_messages()
    assert len(msgs) == 5           # system + 4
    assert msgs[-1]["content"] == "m9"


# ── Fase 2: saneado de argumentos ────────────────────────────────────────────

@pytest.mark.parametrize("raw,esperado", [
    ('{"materia": "salud"}',        {"materia": "salud"}),
    ('{"limit": "20"}',             {"limit": 20}),      # string → int
    ('{"limit": "muchos"}',         {"limit": 20}),      # basura → default
    ('{"materia": ""}',             {}),                 # vacíos se descartan
    ("no es json",                  {}),
    ("",                            {}),
    ("[1,2,3]",                     {}),                 # no es un objeto
])
def test_clean_args(raw, esperado):
    assert ChatOrchestrator._clean_args(raw) == esperado


# ── Fase 3: elección de system prompt ────────────────────────────────────────

def test_phase3_usa_prompt_de_resumen():
    orch = make([user("__RESUMEN_SEMANAL__: general")])
    assert orch._phase3_system() == resumen_con_fechas(orch.hoy, orch.desde)


def test_prompt_de_resumen_lleva_la_ventana_de_fechas():
    """Sin fechas reales el modelo no puede descartar material viejo."""
    orch = make([user("__RESUMEN_SEMANAL__: general")])
    system = orch._phase3_system()
    assert "[fecha actual]" not in system
    assert orch.hoy in system
    assert orch.desde in system
    assert orch.desde != orch.hoy


def test_ventana_del_resumen_son_siete_dias():
    orch = make([user("__RESUMEN_SEMANAL__: general")])
    fmt = "%d/%m/%Y"
    delta = datetime.strptime(orch.hoy, fmt) - datetime.strptime(orch.desde, fmt)
    assert delta.days == RESUMEN_DIAS


@pytest.mark.asyncio
async def test_resumen_usa_herramientas_fijas_no_el_router():
    """
    El resumen semanal no le deja la elección de herramientas al router (8B):
    dejado a su criterio terminaba llamando 7-8 de golpe y reventaba el TPM
    de Groq. _phase2_tools_fijas corre exactamente RESUMEN_TOOLS, en orden.
    """
    orch = make([user("__RESUMEN_SEMANAL__: general")])
    with patch.object(ChatOrchestrator, "_run_tool", new=AsyncMock(return_value={"ok": True})):
        events = [ev async for ev in orch._phase2_tools_fijas(RESUMEN_TOOLS)]

    assert orch.tools_usados == [name for name, _ in RESUMEN_TOOLS]
    # Corren en paralelo: un solo sse.status que las nombra a todas.
    assert len(events) == 1 and "en paralelo" in events[0]

    # Los resultados NO van en el formato nativo de function-calling (mensaje
    # "assistant" con tool_calls + un mensaje "tool" por cada uno): Gemini lo
    # rechaza porque exige un "thought_signature" que no se puede reconstruir
    # a mano. Van como texto plano en mensajes de usuario, uno por herramienta
    # — ver el comentario en ChatOrchestrator._phase2.
    assert len(orch.tool_msgs) == len(RESUMEN_TOOLS)
    assert all(m["role"] == "user" for m in orch.tool_msgs)
    for (name, _), msg in zip(RESUMEN_TOOLS, orch.tool_msgs):
        assert msg["content"].startswith(f"[Resultado de la herramienta {name}]")


@pytest.mark.asyncio
async def test_run_no_llama_a_fase1_para_resumen():
    """run() desvía is_resumen a _phase2_tools_fijas directo, sin pasar por Fase 1."""
    orch = make([user("__RESUMEN_SEMANAL__: general")])
    with patch.object(ChatOrchestrator, "_phase1", new=AsyncMock(side_effect=AssertionError("no debería llamarse"))), \
         patch.object(ChatOrchestrator, "_run_tool", new=AsyncMock(return_value={"ok": True})), \
         patch.object(ChatOrchestrator, "_stream_final") as mock_stream:
        async def fake_stream(*a, **k):
            yield "data: [DONE]\n\n"
        mock_stream.side_effect = fake_stream
        events = [ev async for ev in orch.run()]

    assert events  # llegó hasta el final sin explotar en Fase 1


# ── _detecta_proyectos_por_dias ───────────────────────────────────────────────

@pytest.mark.parametrize("texto,esperado", [
    ("Revisa los proyectos de ley de los últimos 15 días calendario, y "
     "entrégame un cuadro resumen dividido por temas", 15),
    ("qué proyectos de ley se presentaron en los últimos 7 días sobre minería", 7),
    # Recaída real en producción: la misma pregunta que ya se había arreglado
    # ("...últimos N días...") volvió a fabricar datos con esta otra frase,
    # sin número de días explícito — "novedades" sola debe alcanzar.
    ("Busca y entrégame sistematizado en versión editable las novedades de "
     "proyectos de ley en DIPUTADOS", DEFAULT_DIAS_PROYECTOS),
    ("cuáles son los proyectos de ley más recientes", DEFAULT_DIAS_PROYECTOS),
    ("dame los proyectos de ley de hoy", None),                    # sin número de días
    ("explícame qué es un proyecto de ley", None),                 # conceptual, sin "días"
    ("qué sesiones hay en los próximos 2 días", None),              # "días" sin "proyecto"+"ley"
    ("busca proyectos de autor Chirinos", None),                    # sin ventana de tiempo
])
def test_detecta_proyectos_por_dias(texto, esperado):
    assert _detecta_proyectos_por_dias(texto) == esperado


def test_analyze_setea_forzar_dias_proyectos():
    orch = make([user("Revisa los proyectos de ley de los últimos 15 días calendario, "
                       "y entrégame un cuadro resumen dividido por temas")])
    assert orch.forzar_dias_proyectos == 15


def test_resumen_no_activa_forzar_dias_proyectos():
    """is_resumen tiene su propio set de herramientas — no debe pisarse con esto."""
    orch = make([user("__RESUMEN_SEMANAL__: general, con proyectos de ley de 15 días")])
    assert orch.forzar_dias_proyectos is None


@pytest.mark.asyncio
async def test_run_fuerza_buscar_proyectos_sin_pasar_por_router():
    """
    Bug real encontrado en producción: para esta frase el router (8B) elegía
    responder_directo con demasiada frecuencia (~4 de 7 intentos en pruebas
    en vivo) y el modelo grande terminaba inventando una tabla de proyectos
    falsos. forzar_dias_proyectos bypasea el router por completo acá.
    """
    orch = make([user("Revisa los proyectos de ley de los últimos 15 días calendario, "
                       "y entrégame un cuadro resumen dividido por temas")])
    assert orch.forzar_dias_proyectos == 15

    directo = SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(
        tool_calls=[tool_call("1", "responder_directo")]))
    with patch.object(ChatOrchestrator, "_phase1", new=AsyncMock(return_value=directo)), \
         patch.object(ChatOrchestrator, "_run_tool", new=AsyncMock(return_value={"ok": True})) as mock_run_tool, \
         patch.object(ChatOrchestrator, "_stream_final") as mock_stream:
        async def fake_stream(*a, **k):
            yield "data: [DONE]\n\n"
        mock_stream.side_effect = fake_stream
        events = [ev async for ev in orch.run()]

    assert events
    assert orch.tools_usados == ["buscar_proyectos"]
    mock_run_tool.assert_awaited_once_with(
        "buscar_proyectos", {"dias": 15, "limit": MAX_FILAS_AGRUPADO})


async def test_proyectos_por_dias_conserva_el_tema_que_elige_el_router():
    orch = make([user("proyectos de ley de salud de los ultimos 15 dias")])
    choice = SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(
        tool_calls=[tool_call("1", "buscar_proyectos", {"materia": "salud", "limit": 100})]))
    with patch.object(ChatOrchestrator, "_phase1", new=AsyncMock(return_value=choice)), \
         patch.object(ChatOrchestrator, "_run_tool", new=AsyncMock(return_value={"ok": True})) as mock_run_tool, \
         patch.object(ChatOrchestrator, "_stream_final") as mock_stream:
        async def fake_stream(*a, **k):
            yield "data: [DONE]\n\n"
        mock_stream.side_effect = fake_stream
        [ev async for ev in orch.run()]
    # Tema del router + ventana del pedido + tope de listado suelto.
    mock_run_tool.assert_awaited_once_with("buscar_proyectos", {"materia": "salud", "limit": 15, "dias": 15})


# ── _deduplicar_tool_calls ─────────────────────────────────────────────────────

def tool_call(id_, name, args=None):
    return SimpleNamespace(id=id_, function=SimpleNamespace(name=name, arguments=json.dumps(args or {})))


def test_deduplica_fetch_comisiones_repetido():
    """
    Bug real: el router llamaba fetch_comisiones dos veces en el mismo turno
    cuando el usuario nombraba Senado y Diputados — confirmado en vivo dos
    veces, incluso después de aclarar la descripción de la herramienta (el
    fix de prompt solo no fue suficiente).
    """
    calls = [
        tool_call("1", "fetch_comisiones", {"camara": "senado"}),
        tool_call("2", "fetch_comisiones", {"camara": "diputados"}),
    ]
    result = ChatOrchestrator._deduplicar_tool_calls(calls)
    assert len(result) == 1
    assert result[0].function.name == "fetch_comisiones"
    # Sin camara: con una cámara puntual se pierden los enlaces de la otra
    # (ver fetch_comisiones en scraper.py) — la que sobrevive va sin filtro.
    assert json.loads(result[0].function.arguments) == {}


def test_no_deduplica_herramientas_distintas():
    calls = [
        tool_call("1", "fetch_comisiones", {}),
        tool_call("2", "buscar_proyectos", {"materia": "salud"}),
    ]
    result = ChatOrchestrator._deduplicar_tool_calls(calls)
    assert len(result) == 2


def test_no_deduplica_herramientas_fuera_de_la_lista():
    """buscar_proyectos con distintos filtros SÍ puede repetirse con sentido."""
    calls = [
        tool_call("1", "buscar_proyectos", {"materia": "salud"}),
        tool_call("2", "buscar_proyectos", {"materia": "educacion"}),
    ]
    result = ChatOrchestrator._deduplicar_tool_calls(calls)
    assert len(result) == 2


# ── _run_tool: reintento ante falla transitoria ───────────────────────────────

@pytest.mark.asyncio
async def test_run_tool_reintenta_una_vez_y_se_recupera():
    """
    Bug real: SPLEY tuvo un hipo puntual, buscar_proyectos falló, y Solón
    respondió "no puedo buscar ahora" en vez de reintentar — pese a que la
    misma consulta funcionaba perfecto al segundo intento.
    """
    fake_tool = AsyncMock(side_effect=[TimeoutError("boom"), {"items": [1, 2, 3]}])
    with patch("services.orchestrator.TOOL_MAP", {"buscar_proyectos": fake_tool}), \
         patch("services.orchestrator.asyncio.sleep", new=AsyncMock()):
        result = await ChatOrchestrator._run_tool("buscar_proyectos", {"dias": 15})

    assert result["items"] == [1, 2, 3]
    assert "consultado" in result          # hora de la consulta real (tool_cache)
    assert fake_tool.await_count == 2


@pytest.mark.asyncio
async def test_run_tool_solo_reintenta_una_vez():
    """Si falla dos veces seguidas, se rinde — no reintenta indefinidamente."""
    fake_tool = AsyncMock(side_effect=TimeoutError("boom"))
    with patch("services.orchestrator.TOOL_MAP", {"buscar_proyectos": fake_tool}), \
         patch("services.orchestrator.asyncio.sleep", new=AsyncMock()):
        result = await ChatOrchestrator._run_tool("buscar_proyectos", {"dias": 15})

    # Falla de la fuente ≠ "no hay datos": llega como error para que el
    # modelo diga que no se pudo consultar.
    assert result["error"] == "no_se_pudo_consultar"
    assert fake_tool.await_count == 2


def test_phase3_usa_mini_con_tools_sin_workflow():
    """Sin workflow propio se ahorra ~800 tokens usando SYSTEM_MINI."""
    orch = make([user("perfil del congresista Fulano")],
                tools_usados=["buscar_congresista"],
                tool_msgs=[{"role": "tool", "content": "{}"}])
    system = orch._phase3_system()
    assert system == SYSTEM_MINI.format(hoy=orch.hoy)


def test_phase3_inyecta_workflow_de_la_tool_usada():
    orch = make([user("expediente 14864")],
                tools_usados=["fetch_expediente"],
                tool_msgs=[{"role": "tool", "content": "{}"}])
    system = orch._phase3_system()
    assert system.startswith(orch.system_base)
    assert len(system) > len(orch.system_base)


def test_phase3_sin_tools_usa_base():
    orch = make([user("hola")])
    assert orch._phase3_system() == orch.system_base


def test_phase3_responder_directo_usa_base():
    orch = make([user("hola")], solo_responder_directo=True)
    assert orch._phase3_system() == orch.system_base


# ── Fase 3: presupuesto de tokens ────────────────────────────────────────────

@pytest.mark.parametrize("tools,esperado", [
    (["fetch_expediente"],  8000),   # expedientes son largos
    (["buscar_proyectos"],  8000),   # 15 filas completas sin cortar la tabla
    ([],                    4000),   # conversación libre
])
def test_phase3_max_tokens(tools, esperado):
    orch = make([user("x")], tools_usados=tools)
    assert orch._phase3_max_tokens() == esperado


def test_phase3_max_tokens_resumen_tiene_prioridad():
    """
    is_resumen manda su propio presupuesto aunque tools_usados matchee otro caso.

    El número vive en ChatOrchestrator._phase3_max_tokens (3500 desde que se
    sumó la sección de interpelaciones al resumen); lo que este test cuida es
    que gane sobre el de fetch_expediente, no el valor exacto.
    """
    resumen = make([user("__RESUMEN_SEMANAL__: general")],
                   tools_usados=["fetch_expediente"])
    solo_expediente = make([user("x")], tools_usados=["fetch_expediente"])

    assert resumen._phase3_max_tokens() == 12000
    assert resumen._phase3_max_tokens() != solo_expediente._phase3_max_tokens()


async def test_una_excepcion_termina_en_error_legible_y_no_corta_el_stream():
    orch = ChatOrchestrator([user("dame los proyectos del congreso sobre salud")])
    with patch.object(ChatOrchestrator, "_analyze", side_effect=RuntimeError("boom")):
        evs = [ev async for ev in orch.run()]
    assert any('"error"' in ev for ev in evs)
    assert evs[-1] == "data: [DONE]\n\n"


async def test_herramienta_colgada_corta_por_timeout():
    import asyncio

    dormir_de_verdad = asyncio.sleep

    async def colgada(args):
        await dormir_de_verdad(10)

    with patch.dict("services.orchestrator.TOOL_MAP", {"buscar_agenda": colgada}), \
         patch("services.orchestrator.TOOL_TIMEOUT_SECONDS", 0.05), \
         patch("services.orchestrator.asyncio.sleep", new=AsyncMock()):
        result = await ChatOrchestrator._run_tool("buscar_agenda", {})
    assert result["error"] == "no_se_pudo_consultar"


def test_citas_no_verificadas_detecta_proyectos_y_links_inventados():
    from services.orchestrator import _citas_no_verificadas

    fuentes = ('{"numero": "00434-2026-2031-CD", "enlace": '
               '"https://wb2server.congreso.gob.pe/spley-portal/#/diputados/expediente/2026/434"}')
    respuesta = (
        "- 434-2026-2031-CD, ver https://wb2server.congreso.gob.pe/spley-portal/#/diputados/expediente/2026/434.\n"
        "- 00999-2026-2031-CD inventado, https://wb2server.congreso.gob.pe/spley-portal/#/diputados/expediente/2026/999\n"
        "- fecha 26/09/2026 no es un proyecto"
    )
    nums, links = _citas_no_verificadas(respuesta, fuentes)
    assert nums == ["00999-2026-2031-CD"]
    assert links == ["https://wb2server.congreso.gob.pe/spley-portal/#/diputados/expediente/2026/999"]


async def test_respuesta_con_proyecto_inventado_lleva_advertencia():
    orch = ChatOrchestrator([user("hola")])

    async def fake_stream(msgs, max_tokens, **k):
        yield ("text", "El proyecto 01234-2026-2031-CD trata de salud mental.")

    with patch("services.orchestrator.llm.stream", fake_stream):
        evs = [ev async for ev in orch._stream_final([{"role": "system", "content": "x"}], 100)]
    texto = "".join(json.loads(e[6:]).get("text", "") for e in evs if e.startswith("data: {"))
    assert "no pude verificar" in texto and "01234-2026-2031-CD" in texto


async def test_datos_de_herramientas_van_antes_de_la_pregunta():
    orch = ChatOrchestrator([user("hola"), assistant("Hola"), user("dame proyectos de salud")])
    choice = SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(
        tool_calls=[SimpleNamespace(id="1", function=SimpleNamespace(
            name="buscar_proyectos", arguments='{"materia": "salud"}'))]))
    with patch.object(ChatOrchestrator, "_phase1", new=AsyncMock(return_value=choice)), \
         patch.object(ChatOrchestrator, "_run_tool", new=AsyncMock(return_value={"items": []})), \
         patch.object(ChatOrchestrator, "_stream_final") as mock_stream:
        async def fake(msgs, max_tokens):
            yield "data: [DONE]\n\n"
        mock_stream.side_effect = fake
        [ev async for ev in orch.run()]
    msgs = mock_stream.call_args[0][0]
    assert msgs[-1] == user("dame proyectos de salud")
    assert msgs[-2]["content"].startswith("[Resultado de la herramienta buscar_proyectos]")
    assert "el usuario no los envió" in msgs[-2]["content"]


def test_citas_acepta_numero_publicado_sin_guion_entre_anios():
    from services.orchestrator import _citas_no_verificadas

    nums, _ = _citas_no_verificadas("Se sustenta el 00088-2026-2031-CD.",
                                    "Sustentación de la Proposición Legislativa N° 00088-20262031-CD")
    assert nums == []


async def test_con_nombres_de_congresistas_igual_transmite_en_vivo():
    # Regresión de la línea base: con autores en los datos, la respuesta
    # entera se retenía hasta el final (primer texto ≈ total). Ahora sale
    # línea por línea, ya corregida.
    orch = ChatOrchestrator([user("proyectos de agua")])
    orch.tool_msgs = [{"role": "user",
                       "content": 'Datos: {"items": [{"autor": "Juan Pérez García"}]}'}]

    async def fake_stream(msgs, max_tokens, **k):
        yield ("text", "| Proyecto | Autor |\n|---|---|\n")
        yield ("text", "| 1 | Pérez García, Juan |\n")
        yield ("text", "Fin.")

    with patch("services.orchestrator.llm.stream", fake_stream):
        evs = [ev async for ev in orch._stream_final([{"role": "system", "content": "x"}], 100)]
    textos = [json.loads(e[6:])["text"] for e in evs if e.startswith('data: {"text"')]
    assert len(textos) >= 3                       # no un solo bloque al final
    junto = "".join(textos)
    assert "| 1 | Juan Pérez García |" in junto   # el nombre quedó corregido
    assert "Pérez García, Juan" not in junto


# ── Fase 2: paralelo, recorte por filas, caché ──────────────────────────────

async def test_herramientas_corren_en_paralelo_y_conservan_el_orden():
    import asyncio
    import time as _time

    dormir = asyncio.sleep

    async def lenta(args):
        await dormir(0.2)
        return {"de": args["n"]}

    orch = make([user("x")])
    with patch.dict("services.orchestrator.TOOL_MAP", {"buscar_agenda": lenta}):
        t0 = _time.monotonic()
        _ = [ev async for ev in orch._ejecutar_herramientas(
            [("buscar_agenda", {"n": i}) for i in range(3)])]
        dur = _time.monotonic() - t0
    assert dur < 0.5                        # en serie serían ≥ 0.6s
    assert ['"de": 0', '"de": 1', '"de": 2'] == [
        next(f'"de": {i}' for i in range(3) if f'"de": {i}' in m["content"])
        for m in orch.tool_msgs]


async def test_timeout_agotado_no_se_reintenta():
    import asyncio
    dormir = asyncio.sleep
    llamadas = []

    async def colgada(args):
        llamadas.append(1)
        await dormir(10)

    with patch.dict("services.orchestrator.TOOL_MAP", {"buscar_agenda": colgada}), \
         patch("services.orchestrator.TOOL_TIMEOUT_SECONDS", 0.05):
        r = await ChatOrchestrator._run_tool("buscar_agenda", {})
    assert r["error"] == "no_se_pudo_consultar" and len(llamadas) == 1


def test_recorte_quita_filas_enteras_y_avisa():
    from services.orchestrator import MAX_TOOL_RESULT_CHARS, _recortar_resultado
    filas = [{"numero": i, "titulo": "x" * 200} for i in range(200)]
    out = _recortar_resultado("buscar_agenda", {"fuente": "SPLEY", "items": filas})
    assert len(out) <= MAX_TOOL_RESULT_CHARS
    data = json.loads(out)                  # sigue siendo JSON válido
    assert data["truncado"]["total"] == 200
    assert data["truncado"]["mostrados"] == len(data["items"]) < 200


async def test_segunda_consulta_igual_sale_de_cache():
    llamadas = []

    async def tool(args):
        llamadas.append(1)
        return {"items": [1]}

    with patch.dict("services.orchestrator.TOOL_MAP", {"fetch_expediente": tool}):
        a = await ChatOrchestrator._run_tool("fetch_expediente", {"numero": "1"})
        b = await ChatOrchestrator._run_tool("fetch_expediente", {"numero": "1"})
    assert len(llamadas) == 1
    assert b["desde_cache"] is True and b["consultado"] == a["consultado"]


async def test_sin_datos_no_se_cachea():
    llamadas = []

    async def tool(args):
        llamadas.append(1)
        return {"sin_datos": True, "mensaje": "vacío"}

    with patch.dict("services.orchestrator.TOOL_MAP", {"buscar_agenda": tool}):
        await ChatOrchestrator._run_tool("buscar_agenda", {})
        await ChatOrchestrator._run_tool("buscar_agenda", {})
    assert len(llamadas) == 2


def test_desenlaza_links_del_congreso_inventados():
    from services.orchestrator import _desenlazar, _links_permitidos

    fuentes = "enlace: https://wb2server.congreso.gob.pe/spley-portal/#/diputados/expediente/2026/571"
    permitidos = _links_permitidos(fuentes)
    texto = ("Mira el [SPLEY del Senado](https://spley.congreso.gob.pe) o "
             "[el expediente](https://wb2server.congreso.gob.pe/spley-portal/#/diputados/expediente/2026/571) "
             "y la agenda https://diputados.congreso.gob.pe/agenda/ o "
             "[la agenda oficial](https://comunicaciones.congreso.gob.pe/agenda/).")
    limpio = _desenlazar(texto, permitidos)
    assert "spley.congreso.gob.pe" not in limpio and "SPLEY del Senado" in limpio
    assert "expediente/2026/571)" in limpio
    assert "diputados.congreso.gob.pe/agenda" not in limpio
    assert "(https://comunicaciones.congreso.gob.pe/agenda/)" in limpio


def test_desenlaza_expediente_de_otro_proyecto():
    from services.orchestrator import _desenlazar, _links_permitidos

    url = "https://wb2server.congreso.gob.pe/spley-portal/#/diputados/expediente/2026/571"
    permitidos = _links_permitidos(url)
    mal = f"- **Proyecto de Ley 98**: delega facultades. [Ver proyecto]({url})\n"
    bien = f"| [00571-2026-2031-CD]({url}) | 12/10/2026 | Modifica la Ley 26497 |\n"
    assert url not in _desenlazar(mal, permitidos)
    assert url in _desenlazar(bien, permitidos)


def test_recorte_del_expediente_conserva_los_pasos_mas_recientes(monkeypatch):
    import json

    from services import orchestrator
    monkeypatch.setitem(orchestrator.MAX_TOOL_RESULT_CHARS_POR_TOOL, "fetch_expediente", 3000)
    expediente = {
        "numero": "00098-2026-2031-CD",
        "seguimiento": [{"fecha": f"{d:02d}/10/2026", "detalle": "x" * 200} for d in range(1, 31)],
        "todos_los_adjuntos": [{"url": "y" * 200}] * 20,
    }
    recortado = json.loads(orchestrator._recortar_resultado("fetch_expediente", expediente))
    assert "todos_los_adjuntos" not in recortado
    assert recortado["seguimiento"][-1]["fecha"] == "30/10/2026"
    assert recortado["truncado"]["se_muestran"] == "los más recientes"


def test_quita_razonamiento_filtrado():
    from services.orchestrator import _quitar_razonamiento

    filtrado = ("El usuario pide la agenda de comisiones del Senado.\n\nReviso los datos: hay 2 eventos.\n"
                "Redacto la respuesta siguiendo el formato indicado.No se registran sesiones de comisiones.\n\n"
                "### Otras actividades\n| a | b |")
    limpio = _quitar_razonamiento(filtrado)
    assert limpio.startswith("No se registran sesiones de comisiones.")
    assert "El usuario" not in limpio
    normal = "## Agenda\n| a | b |"
    assert _quitar_razonamiento(normal) == normal


def test_cuadro_por_temas_pide_el_lote_entero():
    # 15/10/2026: el router pidió limit=100 con 134 proposiciones en 15 días y
    # el cuadro por temas mostró 91 filas con encabezados que sumaban 134.
    orch = make([user("Revisa los proyectos de ley de los últimos 15 días calendario, "
                      "y entrégame un cuadro resumen dividido por temas")])
    args = orch._ajustar_args("buscar_proyectos", {"limit": 100, "dias": 15})
    assert args["limit"] == MAX_FILAS_AGRUPADO
    assert orch.proyectos_agrupados


def test_listado_suelto_sigue_acotado():
    orch = make([user("novedades de proyectos de ley en diputados")])
    args = orch._ajustar_args("buscar_proyectos", {"limit": 100})
    assert args["limit"] == MAX_FILAS_LISTADO
    assert not orch.proyectos_agrupados


def test_compactar_para_agrupado_deja_solo_las_columnas_del_cuadro():
    resultado = {"total": 1, "items": [{
        "numero": "00588-2026-2031-CD", "fecha_presentacion": "15/10/2026",
        "titulo": "T", "camara": "Diputados", "enlace": "https://e",
        "autor": "A; B; C", "autores_restantes": 5, "proponente": "Cámara de Diputados",
    }]}
    compacto = _compactar_para_agrupado(resultado)
    assert compacto["total"] == 1
    assert compacto["items"] == [{"numero": "00588-2026-2031-CD", "fecha_presentacion": "15/10/2026",
                                  "titulo": "T", "camara": "Diputados", "enlace": "https://e"}]


def test_parsear_clasificacion_corrige_lo_que_el_modelo_pierde():
    numeros = ["001-CD", "002-CD", "003-CD", "004-CD"]
    texto = """```json
    {"grupos": [
      {"tema": "Seguridad", "numeros": ["001-CD", "002-CD", "999-CD"]},
      {"tema": "Salud", "numeros": ["002-CD", "003-CD"]},
      {"tema": "Vacío", "numeros": []}
    ], "lectura": "Predomina seguridad."}
    ```"""
    grupos, lectura = _parsear_clasificacion(texto, numeros)
    # 999 no existe, 002 va una sola vez, 004 quedó sin tema y va a "Otros".
    assert grupos == [("Seguridad", ["001-CD", "002-CD"]), ("Salud", ["003-CD"]),
                      ("Otros", ["004-CD"])]
    assert lectura == "Predomina seguridad."
    assert _parsear_clasificacion("no hay json", numeros) is None


def test_cuadro_por_temas_cuenta_lo_que_muestra():
    items = [{"numero": f"00{i}-CD", "fecha_presentacion": "15/10/2026",
              "titulo": f"Título | {i}", "enlace": f"https://e/{i}"} for i in range(1, 4)]
    md = _cuadro_por_temas(items, [("A", ["001-CD", "002-CD"]), ("B", ["003-CD"])], "Lectura.")
    assert "### A (2)" in md and "### B (1)" in md
    assert md.count("| [00") == 3
    assert "Título / 1" in md  # el "|" del título no rompe la tabla
    assert "**3 proposiciones en 2 temas.**" in md
    assert "### Mi lectura\nLectura." in md


@pytest.mark.asyncio
async def test_cuadro_por_temas_lo_arma_el_codigo():
    orch = make([user("proyectos de los últimos 15 días en un cuadro dividido por temas")])
    orch.proyectos_agrupados = True
    orch.proyectos_items = [{"numero": f"{i:05d}-2026-2031-CD", "titulo": f"T{i}",
                             "fecha_presentacion": "15/10/2026", "enlace": f"https://e/{i}"}
                            for i in range(20)]
    respuesta = json.dumps({"grupos": [{"tema": "Todo", "ids": list(range(1, 21))}],
                            "lectura": "L."})

    async def fake_stream(*a, **k):
        yield ("text", respuesta)

    assert orch._toca_cuadro_por_temas()
    with patch("services.orchestrator.llm.stream", fake_stream):
        md = await orch._armar_cuadro_por_temas()
    assert "### Todo (20)" in md
    assert "**20 proposiciones en 1 tema.**" in md


def test_desenlaza_link_con_dominio_mal_escrito():
    from services.orchestrator import _desenlazar, _links_permitidos
    bueno = "https://wb2server.congreso.gob.pe/spley-portal/#/diputados/expediente/2026/559"
    permitidos = _links_permitidos(bueno)
    texto = ("PL 559 [(Ver fuente)](https://wb2server.congrosgob.pe/spley-portal/#/diputados/expediente/2026/559)"
             f" — [Ver fuente oficial]({bueno})\n")
    assert _desenlazar(texto, permitidos) == f"PL 559 (Ver fuente) — [Ver fuente oficial]({bueno})\n"


def test_citas_reconoce_sufijos_de_una_letra():
    from services.orchestrator import _citas_no_verificadas
    fuentes = "00013-2026-2031-S ... (Proposición Legislativa N° 00098-2026-2031-d)."
    nums, _ = _citas_no_verificadas("PL 00013-2026-2031-S y 00098-2026-2031-CD", fuentes)
    assert nums == []
    nums, _ = _citas_no_verificadas("PL 00099-2026-2031-CD", fuentes)
    assert nums == ["00099-2026-2031-CD"]


def test_resumen_separa_proyectos_de_la_semana_y_de_contexto():
    resultado = {"fuente": "SPLEY", "total": 3, "items": [
        {"numero": "00588-2026-2031-CD", "fecha_presentacion": "15/10/2026", "titulo": "A",
         "estado": "PRESENTADO", "autor": "X; Y", "enlace": "https://e/588"},
        {"numero": "00559-2026-2031-CD", "fecha_presentacion": "08/10/2026", "titulo": "B"},
        {"numero": "00534-2026-2031-CD", "fecha_presentacion": "06/10/2026", "titulo": "C"},
    ]}
    r = _separar_por_semana(resultado, "08/10/2026")
    assert [p["numero"] for p in r["proposiciones_de_la_semana"]] == [
        "00588-2026-2031-CD", "00559-2026-2031-CD"]
    assert [p["numero"] for p in r["_contexto_vigente"]] == ["00534-2026-2031-CD"]
    assert "autor" not in r["proposiciones_de_la_semana"][0]
    assert r["fuente"] == "SPLEY" and "items" not in r


@pytest.mark.asyncio
async def test_respuesta_con_relleno_de_espacios_se_genera_de_nuevo():
    # Mistral, 15/10/2026: "| Fecha       | Hora     | Sesión" y miles de
    # espacios hasta agotar max_tokens. El primer intento se descarta sin
    # mostrar nada y el segundo sale bien.
    orch = make([user("agenda de comisiones del senado")])
    llamadas = []

    def fake_stream(msgs, **k):
        llamadas.append(msgs)
        async def gen():
            if len(llamadas) == 1:
                yield ("text", "## Agenda\n\n| Fecha       | Hora")
                for _ in range(50):
                    yield ("text", " " * 40)
            else:
                yield ("text", "## Agenda\n\n| Fecha | Hora |\n|---|---|\n| 15/10 | 11:00 AM |\n")
        return gen()

    with patch("services.orchestrator.llm.stream", fake_stream):
        eventos = [ev async for ev in orch._stream_final(
            [{"role": "system", "content": "S"}, user("x")], 1000)]
    texto = "".join(json.loads(e[6:])["text"] for e in eventos
                    if e.startswith("data: {") and '"text"' in e)
    assert len(llamadas) == 2
    assert "Nunca alinees columnas" in llamadas[1][0]["content"]
    assert texto.count("## Agenda") == 1
    assert "| 15/10 | 11:00 AM |" in texto
    assert "     " not in texto


@pytest.mark.asyncio
async def test_relleno_dos_veces_pasa_a_otro_proveedor():
    orch = make([user("agenda de comisiones del senado")])
    llamadas = []

    def fake_stream(msgs, *, excluir=(), info=None, **k):
        llamadas.append(excluir)
        async def gen():
            if "mistral" not in excluir:
                info["proveedor"] = "mistral"
                yield ("text", "| Fecha       | Hora" + " " * 200)
            else:
                info["proveedor"] = "groq"
                yield ("text", "| Fecha | Hora |\n|---|---|\n| 15/10 | 11:00 AM |\n")
        return gen()

    with patch("services.orchestrator.llm.stream", fake_stream), \
         patch("services.orchestrator.llm.provider_chain", return_value=["mistral", "groq"]):
        eventos = [ev async for ev in orch._stream_final(
            [{"role": "system", "content": "S"}, user("x")], 1000)]
    texto = "".join(json.loads(e[6:])["text"] for e in eventos
                    if e.startswith("data: {") and '"text"' in e)
    assert llamadas == [(), (), ("mistral",)]
    assert "| 15/10 | 11:00 AM |" in texto
    assert "se cortó" not in texto


def test_parsear_clasificacion_por_ids_y_json_roto():
    numeros = ["001-CD", "002-CD", "003-CD"]
    grupos, _ = _parsear_clasificacion('{"grupos": [{"tema": "A", "ids": [1, 3, 9]}]}', numeros)
    assert grupos == [("A", ["001-CD", "003-CD"]), ("Otros", ["002-CD"])]
    # Coma de más y comillas sin escapar en la lectura: se rescatan los grupos.
    roto = '{"grupos": [{"tema": "A", "ids": [1, 2]}, {"tema": "B", "ids": [3]},], "lectura": "dice "hola""}'
    grupos, _ = _parsear_clasificacion(roto, numeros)
    assert grupos == [("A", ["001-CD", "002-CD"]), ("B", ["003-CD"])]


def test_router_con_numero_mal_copiado_usa_el_del_pedido():
    from services.orchestrator import _corregir_numero_copiado
    pedido = "Resume la fórmula legal del proyecto de ley 14864/2025-CR"
    assert _corregir_numero_copiado({"numero_proyecto": "148864/2025-CR"}, pedido) == {
        "numero_proyecto": "14864/2025-CR"}
    assert _corregir_numero_copiado({"numero_proyecto": "14864/2025-CR"}, pedido) == {
        "numero_proyecto": "14864/2025-CR"}
    # Sin número en el pedido (viene del historial) no se toca.
    assert _corregir_numero_copiado({"numero_proyecto": "588"}, "¿y ese?") == {"numero_proyecto": "588"}


def test_resumen_no_usa_agenda_del_pleno_anterior_a_la_semana():
    agenda = {"camara": "Cámara de Diputados", "titulo": "Agenda 06/10", "fecha": "06/10/2026",
              "enlace": "https://e/a.pdf", "texto": "MOCIÓN DE INTERPELACIÓN ...",
              "estructura": [{"seccion": "Dictamen"}],
              "otras_agendas": [{"camara": "Senado", "fecha": "14/10/2026", "texto": "x"}],
              "agendas_anteriores": [{"titulo": "vieja"}]}
    r = _agenda_pleno_de_la_semana(agenda, "09/10/2026")
    assert "texto" not in r and "estructura" not in r
    assert r["anterior_a_la_semana"] and "06/10/2026" in r["nota"]
    assert r["otras_agendas"][0]["texto"] == "x"   # la del 14/10 sí es de la semana
    assert "agendas_anteriores" not in r


def test_resumen_solo_mociones_de_la_semana():
    r = _mociones_de_la_semana({"mociones_interpelacion": [
        {"numero": "424", "fecha": "21/09/2026"}, {"numero": "500", "fecha": "10/10/2026"}],
        "mociones_censura": [{"numero": "445", "fecha": "24/09/2026"}]}, "09/10/2026")
    assert [m["numero"] for m in r["mociones_interpelacion"]] == ["500"]
    assert r["mociones_censura"] == []
    assert "2 mociones" in r["nota"]


@pytest.mark.asyncio
async def test_resumen_inserta_contexto_vigente_antes_de_la_agenda():
    orch = make([user("__RESUMEN_SEMANAL__: general")])
    orch.contexto_resumen = [{"numero": "00534-2026-2031-CD", "fecha_presentacion": "06/10/2026",
                              "titulo": "PROPOSICIÓN X", "estado": "EN COMISIÓN",
                              "enlace": "https://wb2server.congreso.gob.pe/spley-portal/#/diputados/expediente/2026/534"}]

    def fake_stream(msgs, **k):
        async def gen():
            yield ("text", "# RESUMEN\n\n" + "x " * 400 + "\n\n## Agenda de sesiones\n- 16/10\n")
        return gen()

    with patch("services.orchestrator.llm.stream", fake_stream):
        eventos = [ev async for ev in orch._stream_final([{"role": "system", "content": "S"}, user("x")], 1000)]
    texto = "".join(json.loads(e[6:])["text"] for e in eventos if e.startswith("data: {") and '"text"' in e)
    assert texto.index("## Contexto vigente") < texto.index("## Agenda de sesiones")
    assert "00534-2026-2031-CD" in texto and "(06/10)" in texto
    assert "no pude verificar" not in texto


@pytest.mark.asyncio
async def test_agenda_sin_ia_cuando_ningun_proveedor_responde():
    orch = make([user("sesiones de comisiones de los próximos 2 días")])
    orch.ultimos_resultados["fetch_agenda_comisiones"] = {"sesiones": [
        {"fecha": "16/10/2026", "hora": "9:00 AM", "camara": "Bicameral (Senado y Diputados)",
         "tema": "Sesión de la Comisión Bicameral de Presupuesto", "organiza": "Comisión de Presupuesto",
         "lugar": "Palacio Legislativo. Hemiciclo", "es_comision": True,
         "enlace": "https://comunicaciones.congreso.gob.pe/agenda/2026/10/16/"},
        {"fecha": "16/10/2026", "hora": "6:00 PM", "camara": "Cámara de Diputados", "tema": "Conferencia",
         "organiza": "Diputado X", "lugar": "Sala", "es_comision": False, "enlace": "https://e"}],
        "avisos": [{"aviso": "Semana de Representación"}]}

    def fake_stream(msgs, **k):
        async def gen():
            yield ("error", "Groq tardó demasiado en responder.")
        return gen()

    with patch("services.orchestrator.llm.stream", fake_stream):
        eventos = [ev async for ev in orch._stream_final([{"role": "system", "content": "S"}, user("x")], 1000)]
    texto = "".join(json.loads(e[6:])["text"] for e in eventos if e.startswith("data: {") and '"text"' in e)
    assert not any('"error"' in e for e in eventos)
    assert "| 16/10 | 9:00 AM | Bicameral (Senado y Diputados) | Comisión de Presupuesto |" in texto
    assert "Palacio Legislativo. Hemiciclo" in texto
    assert "Otras actividades" in texto and "Semana de Representación" in texto


def test_nombre_de_la_comision_sale_de_quien_organiza():
    from services.orchestrator import _nombre_comision
    assert _nombre_comision({"organiza": "Comisión de Inclusión Social, Familia, Mujer y Pueblos "
                             "Andinos, Amazónicos y Afroperuanos Presidenta: Diputada Flor Meza Rivera"}) == (
        "Comisión de Inclusión Social, Familia, Mujer y Pueblos Andinos, Amazónicos y Afroperuanos")
    assert _nombre_comision({"organiza": "Comisión de Presupuesto y Cuenta General de la República. "
                             "Presidente: Senador José Arista"}) == (
        "Comisión de Presupuesto y Cuenta General de la República")
    assert _nombre_comision({"organiza": "Senador X", "tema": "Sesión de la Comisión de Constitución (Continuación)"}) == (
        "Comisión de Constitución")


def test_tabla_agenda_ordena_por_fecha_y_hora():
    from services.orchestrator import _tabla_agenda
    base = {"camara": "Senado", "organiza": "Comisión X", "lugar": "Sala", "es_comision": True, "enlace": "https://e"}
    md = _tabla_agenda({"sesiones": [
        {**base, "fecha": "16/10/2026", "hora": "1:30 PM", "tema": "C"},
        {**base, "fecha": "16/10/2026", "hora": "9:00 AM", "tema": "B"},
        {**base, "fecha": "16/10/2026", "hora": "8:30 AM", "tema": "A"},
        {**base, "fecha": "15/10/2026", "hora": "4:00 PM", "tema": "Z"}]}, True)
    filas = [l for l in md.splitlines() if l.startswith("| 1")]
    assert [f.split("|")[5].strip() for f in filas] == ["Z", "A", "B", "C"]


def test_agenda_del_pleno_sin_camara_nombrada_no_prioriza_congreso():
    orch = make([user("Dame un resumen de la estructura de la Agenda del Pleno (cuántos dictámenes)")])
    assert orch._ajustar_args("fetch_agenda_pleno", {"camara": "congreso"}) == {}
    orch = make([user("la última agenda del pleno del Senado")])
    assert orch._ajustar_args("fetch_agenda_pleno", {"camara": "senado"}) == {"camara": "senado"}


def test_interpelaciones_sin_ministro_nombrado_no_filtra():
    orch = make([user("Indicame si se ha presentado alguna moción de interpelación a algún Ministro")])
    assert orch._ajustar_args("fetch_interpelaciones", {"ministro": "Interior"}) == {}
    orch = make([user("¿hay interpelación al ministro del Interior?")])
    assert orch._ajustar_args("fetch_interpelaciones", {"ministro": "Interior"}) == {"ministro": "Interior"}


def test_citas_acepta_numeros_listados_con_de_anio():
    from services.orchestrator import _citas_no_verificadas
    fuentes = "Segunda mesa técnica “Evaluación Jurídica de las Propuestas Legislativas: 0008, 0013 y 0016 de 2026”."
    nums, _ = _citas_no_verificadas("Analiza la 0016-2026-2031-CD y la 0013-2026-2031-CD", fuentes)
    assert nums == []
    nums, _ = _citas_no_verificadas("y la 0017-2026-2031-CD", fuentes)
    assert nums == ["0017-2026-2031-CD"]


@pytest.mark.asyncio
async def test_relleno_a_mitad_de_respuesta_se_continua():
    orch = make([user("estatus del 14864/2025-CR")])
    llamadas = []

    def fake_stream(msgs, **k):
        llamadas.append(msgs)
        async def gen():
            if len(llamadas) == 1:
                yield ("text", "## Ficha\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n### Seguimiento\n\n")
                yield ("text", "| FECHA       | ESTADO" + " " * 200)
            else:
                yield ("text", "| FECHA | ESTADO |\n|---|---|\n| 22/07/2026 | PRESENTADO |\n")
        return gen()

    with patch("services.orchestrator.llm.stream", fake_stream):
        eventos = [ev async for ev in orch._stream_final([{"role": "system", "content": "S"}, user("x")], 1000)]
    texto = "".join(json.loads(e[6:])["text"] for e in eventos if e.startswith("data: {") and '"text"' in e)
    assert len(llamadas) == 2
    assert llamadas[1][-2]["role"] == "assistant" and "### Seguimiento" in llamadas[1][-2]["content"]
    assert texto.count("## Ficha") == 1
    assert "| 22/07/2026 | PRESENTADO |" in texto
    assert "se cortó" not in texto and "     " not in texto


def test_quita_detalles_de_procedimiento_que_no_estan_en_la_fuente():
    from services.orchestrator import _quitar_no_verificable
    fuentes = "moción 424 ... pliego de 44 preguntas ... plazo solicitado: 90 días hábiles".lower()
    texto = ("La moción 424 trae 44 preguntas.\n"
             "- Plazo: debe renunciar en 48 horas (art. 120 de la Constitución).\n"
             "- Requiere mayoría absoluta.\n"
             "- Plazo solicitado: 90 días hábiles.\n"
             "- Comparece dentro de 5 días.\n")
    limpio, n = _quitar_no_verificable(texto, fuentes + " plazo de 15 días")
    assert n == 3
    assert "48 horas" not in limpio and "mayoría absoluta" not in limpio
    assert "90 días hábiles" in limpio and "44 preguntas" in limpio


@pytest.mark.parametrize("texto,esperado", [
    ("¿Y qué pasa si después lo censuran? Dame el detalle clave", True),
    ("Explícame la interpelación al ministro de Educación", True),
    ("¿Cuántas firmas necesita una moción de censura?", True),
    ("Dame la agenda de comisiones de hoy", False),
    ("Revisa los proyectos de ley de los últimos 15 días", False),
    ("hola", False),
])
def test_detecta_preguntas_de_procedimiento(texto, esperado):
    from services.orchestrator import _es_procedimiento
    assert _es_procedimiento(texto) is esperado


def test_filtro_reconoce_numeros_en_letras_y_quita_calculos():
    from services.orchestrator import (
        _numeros_en_letras_a_cifras,
        _quitar_no_verificable,
    )
    fuentes = _numeros_en_letras_a_cifras(
        "el presidente acepta la dimisión dentro de las setenta y dos horas siguientes. "
        "se debate y vota entre el cuarto y décimo día natural.")
    texto = ("- Acepta la dimisión dentro de 72 horas.\n"
             "- Firmas: 25% (32 de 130).\n"
             "- Se vota entre el 4 y el 10 día.\n")
    limpio, n = _quitar_no_verificable(texto, fuentes)
    assert n == 1 and "72 horas" in limpio and "32 de 130" not in limpio


def test_filtro_acepta_art_abreviado_si_la_norma_dice_articulo():
    from services.orchestrator import _canon, _quitar_no_verificable
    fuentes = _canon('{"articulo": "132", "texto": "artículo 132. la cámara de diputados hace efectiva…"}')
    limpio, n = _quitar_no_verificable("- [Constitución, Art. 132](https://x) (pág. 104)\n", fuentes)
    assert n == 0 and "Art. 132" in limpio
