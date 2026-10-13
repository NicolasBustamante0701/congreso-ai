"""
Clasificación de errores del proveedor LLM.

El caso que motivó estos tests: con una GEMINI_API_KEY inválida la app decía
"Hubo un problema al conectar. Intentá de nuevo." — indistinguible de un
problema de red — y además reintentaba, porque Gemini responde 400 (no 401) a
una key mala y eso caía en la heurística de "tool_call malformado".
"""
from services import llm

# Textos reales capturados del SDK con keys inválidas (22/08/2026).
ERROR_GEMINI = (
    "Error code: 400 - [{'error': {'code': 400, 'message': "
    "'Please pass a valid API key', 'status': 'INVALID_ARGUMENT'}}]"
)
ERROR_GROQ = (
    "Error code: 401 - {'error': {'message': 'Invalid API Key', "
    "'type': 'invalid_request_error', 'code': 'invalid_api_key'}}"
)
ERROR_TOOL_MALFORMADO = (
    "Error code: 400 - {'error': {'message': 'tool_use_failed', "
    "'failed_generation': '<tool>...'}}"
)
ERROR_RATE_LIMIT = "Error code: 429 - rate limit reached, please try again in 2.5s"


def test_detecta_key_invalida_en_ambos_proveedores():
    assert llm.is_auth_error(ERROR_GEMINI)
    assert llm.is_auth_error(ERROR_GROQ)


def test_no_confunde_otros_errores_con_auth():
    assert not llm.is_auth_error(ERROR_TOOL_MALFORMADO)
    assert not llm.is_auth_error(ERROR_RATE_LIMIT)


def test_key_invalida_no_se_toma_por_tool_call_malformado():
    """
    El 400 de Gemini por key inválida no debe disparar el reintento sin tools:
    son dos llamadas fallidas en vez de una y el usuario igual no se entera de
    cuál era el problema.
    """
    assert not llm.is_tool_format_error(ERROR_GEMINI)
    assert llm.is_tool_format_error(ERROR_TOOL_MALFORMADO)


def test_mensaje_de_auth_dice_qué_variable_completar():
    msg = llm.friendly_error(ERROR_GEMINI)
    assert ".env" in msg
    # El nombre de la variable depende del proveedor activo en config.
    assert any(v in msg for v in llm._ENV_KEY.values())
    assert "problema al conectar" not in msg.lower()


def test_rate_limit_sigue_teniendo_su_propio_mensaje():
    msg = llm.friendly_error(ERROR_RATE_LIMIT)
    assert "espera" in msg.lower() or "límite" in msg.lower()


# ── Streaming con respaldo de proveedor ─────────────────────────────────────
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402


def _fake_deltas(comportamiento):
    """
    comportamiento[provider] (o [modelo], si está) = lista de deltas, o
    excepción a lanzar (tras deltas opcionales).
    """
    async def fake(provider, messages, *, role, max_tokens, temperature, meta=None,
                   model=None):
        for item in comportamiento.get(model, comportamiento.get(provider, [])):
            if isinstance(item, Exception):
                raise item
            yield item
    return fake


async def _recolectar(**kw):
    return [ev async for ev in llm.stream([{"role": "user", "content": "hola"}], **kw)]


@pytest.fixture
def cadena(monkeypatch):
    monkeypatch.setattr(llm, "provider_chain", lambda: ["gemini", "groq"])
    monkeypatch.setattr(llm, "model_for", lambda p, r: f"{p}-model")
    # Sin modelos de respaldo salvo en los tests que los prueban: con los de
    # config, un timeout de Gemini probaría primero el otro modelo de Gemini.
    monkeypatch.setitem(llm.PROVIDERS, "gemini",
                        {**llm.PROVIDERS["gemini"], "modelos_respaldo": []})


async def test_si_el_activo_se_cuelga_responde_el_de_respaldo(cadena, monkeypatch):
    monkeypatch.setattr(llm, "_deltas", _fake_deltas({
        "gemini": [TimeoutError("timed out")],
        "groq": ["Hola", " mundo"],
    }))
    evs = await _recolectar()
    assert any(k == "status" and "Groq" in p for k, p in evs)
    assert [p for k, p in evs if k == "text"] == ["Hola", " mundo"]
    assert not any(k == "error" for k, _ in evs)


async def test_corte_a_mitad_no_cambia_de_proveedor(cadena, monkeypatch):
    monkeypatch.setattr(llm, "_deltas", _fake_deltas({
        "gemini": ["Hola", ConnectionError("reset")],
        "groq": ["NO DEBERÍA LLEGAR"],
    }))
    evs = await _recolectar()
    assert evs[0] == ("text", "Hola")
    assert evs[-1][0] == "error"
    assert all(p != "NO DEBERÍA LLEGAR" for _, p in evs)


async def test_rate_limit_largo_con_respaldo_no_espera(cadena, monkeypatch):
    async def no_dormir(s):
        raise AssertionError("no debería esperar con un respaldo disponible")
    monkeypatch.setattr(llm.asyncio, "sleep", no_dormir)
    monkeypatch.setattr(llm, "_deltas", _fake_deltas({
        "gemini": [Exception("Error code: 429 - rate limit, please retry in 45s")],
        "groq": ["ok"],
    }))
    evs = await _recolectar()
    assert ("text", "ok") in evs


async def test_todos_fallan_reporta_el_error_del_principal(cadena, monkeypatch):
    monkeypatch.setattr(llm, "_deltas", _fake_deltas({
        "gemini": [Exception(ERROR_GEMINI)],
        "groq": [Exception("Error code: 402 - payment required, quota")],
    }))
    evs = await _recolectar()
    assert evs[-1][0] == "error"
    assert "GEMINI_API_KEY" in evs[-1][1]


async def test_primer_token_lento_pasa_al_respaldo(cadena, monkeypatch):
    import asyncio

    async def fake(provider, messages, *, role, max_tokens, temperature, meta=None,
                   model=None):
        if provider == "gemini":
            await asyncio.sleep(10)
        yield f"desde {provider}"

    monkeypatch.setattr(llm, "FIRST_TOKEN_TIMEOUT", 0.05)
    monkeypatch.setattr(llm, "_deltas", fake)
    evs = await _recolectar()
    assert ("text", "desde groq") in evs
    assert not any(p == "desde gemini" for _, p in evs)


def test_cuenta_sin_saldo_no_es_rate_limit():
    err = ("Error code: 402 - {'message': 'Payment required to access this resource.', "
           "'type': 'payment_required_error', 'param': 'quota', 'code': 'payment_required'}")
    assert not llm.is_rate_limit(err)


async def test_respuesta_cortada_por_largo_lo_avisa(cadena, monkeypatch):
    async def fake(provider, messages, *, role, max_tokens, temperature, meta=None,
                   model=None):
        yield "- **28/"
        meta["cortada"] = True

    monkeypatch.setattr(llm, "_deltas", fake)
    evs = await _recolectar()
    assert "se cortó por largo" in evs[-1][1]


# Textos reales del log del 13/10/2026, cuando P6 y P13 no tuvieron respuesta.
ERROR_GEMINI_503 = (
    "Error code: 503 - [{'error': {'code': 503, 'message': 'This model is currently "
    "experiencing high demand. Spikes in demand are usually temporary. Please try "
    "again later.', 'status': 'UNAVAILABLE'}}]"
)
ERROR_GROQ_413 = (
    "Error code: 413 - {'error': {'message': 'Request too large for model "
    "`openai/gpt-oss-120b` in organization `org_x` service tier `on_demand` on tokens "
    "per minute (TPM): Limit 8000, Requested 24393, please reduce your message size "
    "and try again.'}}"
)


async def test_modelo_saturado_prueba_el_modelo_de_respaldo(cadena, monkeypatch):
    monkeypatch.setitem(llm.PROVIDERS, "gemini",
                        {**llm.PROVIDERS["gemini"], "modelos_respaldo": ["gemini-backup"]})
    monkeypatch.setattr(llm, "_deltas", _fake_deltas({
        "gemini": [Exception(ERROR_GEMINI_503)],
        "gemini-backup": ["desde el respaldo"],
        "groq": ["NO DEBERÍA LLEGAR"],
    }))
    evs = await _recolectar()
    assert ("text", "desde el respaldo") in evs
    assert not any(k == "error" for k, _ in evs)


async def test_pedido_demasiado_grande_reintenta_recortado(cadena, monkeypatch):
    vistos = []

    async def fake(provider, messages, *, role, max_tokens, temperature, meta=None,
                   model=None):
        if provider == "gemini":
            raise Exception(ERROR_GEMINI_503)
        vistos.append((len(messages[-1]["content"]), max_tokens))
        if len(vistos) == 1:
            raise Exception(ERROR_GROQ_413)
        yield "respuesta recortada"

    monkeypatch.setitem(llm.PROVIDERS, "gemini",
                        {**llm.PROVIDERS["gemini"], "modelos_respaldo": []})
    monkeypatch.setattr(llm, "_deltas", fake)
    evs = [ev async for ev in llm.stream(
        [{"role": "user", "content": "x" * 80_000}], max_tokens=12_000)]
    assert ("text", "respuesta recortada") in evs
    (largo1, tope1), (largo2, tope2) = vistos
    # 413 con Limit 8000 / Requested 24393 y max_tokens=12000: entrada ~12 400
    # tokens que tienen que bajar a ~5 700 junto con una salida de 2000.
    assert largo2 < largo1 / 2 and tope2 <= 2000


def test_mensajes_de_saturado_y_demasiado_grande():
    assert "saturado" in llm.friendly_error(ERROR_GEMINI_503)
    assert "más acotada" in llm.friendly_error(ERROR_GROQ_413)
    assert "problema al conectar" not in llm.friendly_error(ERROR_GEMINI_503).lower()


async def test_respuesta_vacia_no_cuenta_como_respuesta(cadena, monkeypatch):
    monkeypatch.setitem(llm.PROVIDERS, "gemini",
                        {**llm.PROVIDERS["gemini"], "modelos_respaldo": []})
    monkeypatch.setattr(llm, "_deltas", _fake_deltas({
        "gemini": [],
        "groq": ["desde groq"],
    }))
    evs = await _recolectar()
    assert ("text", "desde groq") in evs


class _RouterFalso:
    """Cliente falso: responde según el modelo, o lanza la excepción indicada."""

    def __init__(self, por_modelo, llamados):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self._por_modelo, self._llamados = por_modelo, llamados

    async def _create(self, *, model, **kw):
        self._llamados.append(model)
        r = self._por_modelo[model]
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(choices=[r])


async def test_router_sin_tool_call_prueba_el_modelo_grande(monkeypatch):
    # Visto el 13/10/2026: gemini-flash-lite colgado y gpt-oss-20b devolviendo
    # "did not call a tool"; el router se rendía y Diana respondía sin datos.
    llamados = []
    cliente = _RouterFalso({
        "gemini-router": TimeoutError("timed out"),
        "groq-router": Exception("Error code: 400 - Tool choice is required, but model "
                                 "did not call a tool, 'code': 'tool_use_failed'"),
        "groq-main": "elección",
    }, llamados)
    monkeypatch.setattr(llm, "provider_chain", lambda: ["gemini", "groq"])
    monkeypatch.setattr(llm, "model_for",
                        lambda p, r: f"{p}-{'router' if r == 'router' else 'main'}")
    monkeypatch.setitem(llm.PROVIDERS, "gemini",
                        {**llm.PROVIDERS["gemini"], "modelos_respaldo": []})
    monkeypatch.setattr(llm, "get_client", lambda p: cliente)
    elegido = await llm.complete_router([], tools=[])
    assert elegido == "elección"
    assert llamados == ["gemini-router", "groq-router", "groq-main"]
