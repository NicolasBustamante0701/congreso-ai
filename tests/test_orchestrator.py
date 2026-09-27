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
    RESUMEN_DIAS,
    RESUMEN_TOOLS,
    ChatOrchestrator,
    _detecta_proyectos_por_dias,
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
    assert len(events) == len(RESUMEN_TOOLS)  # un sse.status por herramienta

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
    mock_run_tool.assert_awaited_once_with("buscar_proyectos", {"dias": 15})


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

    assert result == {"items": [1, 2, 3]}
    assert fake_tool.await_count == 2


@pytest.mark.asyncio
async def test_run_tool_solo_reintenta_una_vez():
    """Si falla dos veces seguidas, se rinde — no reintenta indefinidamente."""
    fake_tool = AsyncMock(side_effect=TimeoutError("boom"))
    with patch("services.orchestrator.TOOL_MAP", {"buscar_proyectos": fake_tool}), \
         patch("services.orchestrator.asyncio.sleep", new=AsyncMock()):
        result = await ChatOrchestrator._run_tool("buscar_proyectos", {"dias": 15})

    assert result["sin_datos"] is True
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
    ([],                    2500),   # conversación libre
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
    assert result["sin_datos"] is True


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

    async def fake_stream(msgs, max_tokens):
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
