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
