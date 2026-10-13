"""
Seguimiento de proyectos y búsqueda unificada.

SPLEY está SIMULADO (fake de fetch_expediente / fetch_proyectos): se prueba
qué cuenta como cambio y qué se guarda, no la conexión real con el Congreso.
"""
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from services import busqueda, db, seguimiento

NUM = "00100-2026-2031-CD"


@pytest.fixture(autouse=True)
def db_temporal(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "diana.db")


def exp(estado="PRESENTADO", pasos=(("31/08/2026", "PRESENTADO", ""),), docs=("u1",)):
    return {"numero": NUM, "titulo": "Ley de prueba", "estado": estado, "enlace_expediente": "https://wb2server.congreso.gob.pe/x",
            "seguimiento": [{"fecha": f, "estado": e, "comision": c, "detalle": "texto largo"} for f, e, c in pasos],
            "todos_los_adjuntos": [{"url": u} for u in docs]}


def test_cambio_de_formato_no_es_cambio():
    a = seguimiento.huella(exp())
    b = exp()
    b["seguimiento"][0]["detalle"] = "OTRO TEXTO, REFORMATEADO"
    b["seguimiento"] = list(reversed(b["seguimiento"]))
    assert seguimiento.diferencias(a, seguimiento.huella(b)) == []


def test_detecta_estado_paso_y_documento_nuevos():
    a = seguimiento.huella(exp())
    b = seguimiento.huella(exp("EN COMISIÓN", pasos=(("31/08/2026", "PRESENTADO", ""),
                                                      ("01/09/2026", "EN COMISIÓN", "Defensa")), docs=("u1", "u2")))
    tipos = [t for t, _ in seguimiento.diferencias(a, b)]
    assert tipos == ["estado", "seguimiento", "documento"]


async def test_seguir_no_alerta_y_revisar_si():
    with patch.object(seguimiento, "_fetch", AsyncMock(return_value=exp())):
        await seguimiento.seguir(NUM)
    assert seguimiento.alertas() == []                      # la línea base no alerta
    nuevo = exp("EN COMISIÓN", pasos=(("31/08/2026", "PRESENTADO", ""), ("01/09/2026", "EN COMISIÓN", "Defensa")))
    with patch.object(seguimiento, "_fetch", AsyncMock(return_value=nuevo)):
        r1 = await seguimiento.revisar(NUM)
        r2 = await seguimiento.revisar(NUM)                 # misma info: nada nuevo
    assert r1["nuevas"] == 2 and r2["nuevas"] == 0
    assert seguimiento.listar()[0]["sin_leer"] == 2
    assert seguimiento.marcar_leidas() == 2


async def test_fuente_caida_no_es_sin_cambios():
    with patch.object(seguimiento, "_fetch", AsyncMock(return_value=exp())):
        await seguimiento.seguir(NUM)
    with patch.object(seguimiento, "_fetch", AsyncMock(side_effect=TimeoutError())):
        r = await seguimiento.revisar(NUM)
    assert r["ok"] is False
    assert seguimiento.listar()[0]["ultimo_error"].startswith("TimeoutError")


async def test_revisar_todos_no_corre_dos_veces_a_la_vez():
    async with seguimiento._lock:
        assert await seguimiento.revisar_todos() == {"en_curso": True}


def test_migracion_v1_a_v2_conserva_las_transcripciones(tmp_path):
    con = sqlite3.connect(db.DB_PATH)
    con.executescript(db._MIGRACIONES[1])
    con.execute("PRAGMA user_version = 1")
    con.execute("INSERT INTO sesion VALUES ('v','t','u',0,'detenida','a','a',NULL,NULL)")
    con.commit(); con.close()
    with db.conexion() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.VERSION_ESQUEMA
        assert c.execute("SELECT COUNT(*) FROM sesion").fetchone()[0] == 1
        c.execute("SELECT * FROM seguimiento").fetchall()   # la tabla nueva existe


async def test_busqueda_informa_la_fuente_caida():
    with patch("scraper.fetch_proyectos", AsyncMock(side_effect=RuntimeError("SPLEY caído"))), \
         patch("scraper.fetch_agenda_camaras", AsyncMock(return_value={"sesiones": [
             {"tema": "Sesión sobre el canon minero", "lugar": "", "organiza": "", "camara": "Senado"}]})):
        r = await busqueda.buscar("canon minero")
    assert r["fuentes"]["proyectos"]["estado"] == "error"
    assert r["fuentes"]["agenda"]["total"] == 1
    assert r["fuentes"]["transcripciones"]["estado"] == "ok"


async def test_busqueda_reconoce_numero_de_proyecto():
    r = await busqueda.buscar("00571-2026-2031-CD")
    assert r["numero"] == "00571-2026-2031-CD" and "proyectos" not in r["fuentes"]
