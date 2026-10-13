"""Extracción de texto de PDFs: marcas de página y páginas sin texto."""
import fitz

from services import pdf as pdf_service


def _pdf(paginas):
    doc = fitz.open()
    for texto in paginas:
        page = doc.new_page()
        if texto:
            page.insert_text((72, 72), texto)
    return doc.tobytes()


def test_cada_pagina_lleva_su_numero():
    texto, n = pdf_service.extract_text(_pdf(["Artículo 1", "Artículo 2"]))
    assert n == 2
    assert "[Página 1]\nArtículo 1" in texto
    assert "[Página 2]\nArtículo 2" in texto


def test_avisa_las_paginas_sin_texto():
    texto, n = pdf_service.extract_text(_pdf(["Portada", "", "Anexo"]))
    assert n == 3
    assert "[Página 2]" not in texto
    assert "sin texto extraíble" in texto and ": 2." in texto


def test_el_recorte_dice_hasta_que_pagina_llego(monkeypatch):
    monkeypatch.setattr(pdf_service, "MAX_TEXT_CHARS", 45)   # corta dentro de la pág. 2
    texto, _ = pdf_service.extract_text(_pdf(["a" * 20, "b" * 20, "c" * 20]))
    assert "se leyó hasta la página 2 de 3" in texto
