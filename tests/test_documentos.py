"""PDFs completos: guardar por página y recuperar las que responden una pregunta."""
import pytest

from services import db, documentos


@pytest.fixture(autouse=True)
def db_temporal(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "diana.db")


PAGINAS = ["Portada"] + [f"Texto de relleno {i}. Ver artículo 90 más adelante." for i in range(2, 60)] + [
    "TÍTULO IV\nArtículo 90°.- El Poder Legislativo reside en el Congreso, que consta de dos cámaras.",
    "continúa: el Senado y la Cámara de Diputados.",
    "Artículo 120°.- Son nulos los actos del Presidente sin refrendo ministerial.",
]


def test_articulo_exacto_aunque_este_lejos():
    doc = documentos.guardar("Constitución", "prueba", PAGINAS)
    pags = documentos.paginas_relevantes(doc, "¿Qué dice el artículo 90 sobre el Poder Legislativo?")
    nums = [n for n, _ in pags]
    assert nums[:2] == [60, 61]          # el encabezado y la página siguiente
    assert 2 not in nums or nums.index(60) == 0   # una simple mención no gana


def test_busqueda_por_palabras():
    doc = documentos.guardar("Constitución", "prueba", PAGINAS)
    nums = [n for n, _ in documentos.paginas_relevantes(doc, "refrendo ministerial")]
    assert nums == [62]


def test_mismo_documento_no_se_duplica():
    a = documentos.guardar("x", "a", PAGINAS)
    b = documentos.guardar("x", "b", PAGINAS)
    assert a == b and documentos.info(a)["paginas"] == len(PAGINAS)


def test_pagina_pedida_por_numero():
    doc = documentos.guardar("x", "a", PAGINAS)
    assert [n for n, _ in documentos.paginas_relevantes(doc, "resumime la página 62")][0] == 62
