"""
Configuración compartida de tests.

Los tests del scraper no tocan la red: VCR graba las respuestas reales del
Congreso en tests/cassettes/*.yaml la primera vez y las reproduce después.
Para regrabar una cassette, borrá el .yaml y volvé a correr los tests.
"""
import json
import os
import tempfile
from pathlib import Path

import pytest

# La app no debe exigir una key real para importarse.
os.environ.setdefault("GROQ_API_KEY", "test-key-mock")
# Que los tests no escriban en el log real de la app.
os.environ["DIANA_LOG_DIR"] = str(Path(tempfile.gettempdir()) / "diana-tests-logs")

TESTS_DIR = Path(__file__).parent
CASSETTES_DIR = TESTS_DIR / "cassettes"
SNAPSHOTS_DIR = TESTS_DIR / "snapshots"


# SPLEY ignora el parámetro `size` y devuelve el catálogo completo (~8 MB) en
# cada llamada. Sin recortarlo cada cassette pesaría 9 MB, así que al grabar
# nos quedamos con los primeros proyectos — suficiente para los tests y deja
# el JSON válido.
SPLEY_KEEP_PROYECTOS = 10


def _trim_spley_response(response):
    """Recorta data.proyectos al grabar, para no commitear cassettes de 9 MB."""
    body = response.get("body", {}).get("string")
    if not body:
        return response
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return response
    if '"proyectos"' not in body:
        return response

    try:
        payload = json.loads(body)
        proyectos = payload["data"]["proyectos"]
    except (ValueError, KeyError, TypeError):
        return response

    if len(proyectos) > SPLEY_KEEP_PROYECTOS:
        payload["data"]["proyectos"] = proyectos[:SPLEY_KEEP_PROYECTOS]
        response["body"]["string"] = json.dumps(payload, ensure_ascii=False)
    return response


@pytest.fixture(scope="module")
def vcr_config():
    # En local "once" graba la cassette si falta. En CI usamos "none": si falta
    # una cassette el test falla en vez de salir a internet a mitad del build.
    record_mode = "none" if os.getenv("CI") else "once"
    return {
        "cassette_library_dir": str(CASSETTES_DIR),
        "record_mode": record_mode,
        # SPLEY usa POST con filtros en el body: sin body dos búsquedas
        # distintas al mismo path se confundirían entre sí.
        "match_on": ["method", "scheme", "host", "port", "path", "query", "body"],
        "filter_headers": ["authorization", "cookie", "set-cookie", "user-agent"],
        "decode_compressed_response": True,
        "before_record_response": _trim_spley_response,
    }


@pytest.fixture(autouse=True)
def _reset_periodos_cache():
    """
    scraper._periodos() cachea los periodos parlamentarios por proceso. Sin
    limpiarlo, el primer test graba el GET a /periodo-parlamentario y los demás
    lo saltan por caché: sus cassettes quedan sin esa request y el replay pasa a
    depender del orden de ejecución. Reseteando, cada cassette es autocontenida.
    """
    import scraper
    scraper._periodos_cache = None
    yield
    scraper._periodos_cache = None


def assert_snapshot(name: str, data, ignorar: tuple = ()):
    """
    Compara `data` con tests/snapshots/<name>.json.

    Si el snapshot no existe lo escribe y pasa el test — así la primera
    corrida graba la referencia. Los cambios posteriores fallan y hay que
    revisarlos a mano (o borrar el .json para regrabar).

    `ignorar`: claves de primer nivel que se excluyen de la comparación. Sirve
    para los campos que el scraper calcula contra la fecha de HOY (ej.
    "advertencia_desactualizado", que lleva un "hace N días"): congelarlos en
    un JSON hace que el test solo pueda pasar el día en que se grabó. Lo que
    dependa del reloj se verifica aparte, en su propio assert.
    """
    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    path = SNAPSHOTS_DIR / f"{name}.json"
    actual = json.loads(json.dumps(data, ensure_ascii=False, default=str))
    if ignorar and isinstance(actual, dict):
        actual = {k: v for k, v in actual.items() if k not in ignorar}

    if not path.exists():
        path.write_text(
            json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        pytest.skip(f"Snapshot '{name}' grabado por primera vez — revisalo y commiteá.")

    expected = json.loads(path.read_text(encoding="utf-8"))
    assert actual == expected, (
        f"El scraper devuelve algo distinto al snapshot '{name}'.\n"
        f"Si el cambio es intencional, borrá tests/snapshots/{name}.json y regrabá."
    )
