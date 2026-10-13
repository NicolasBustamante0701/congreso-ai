"""
OCR de páginas escaneadas — sin red: el motor se reemplaza por uno falso.
"""
import fitz
import pytest

from services import ocr


def _pdf(paginas: int) -> bytes:
    doc = fitz.open()
    for _ in range(paginas):
        doc.new_page()
    return doc.tobytes()


@pytest.fixture
def motor(monkeypatch, tmp_path):
    """Motor falso que falla la primera vez con las páginas de `fallan`."""
    monkeypatch.setattr(ocr, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(ocr, "PAUSA_REINTENTO", 0)
    estado = {"llamadas": [], "fallan": set()}

    async def transcribir(imagenes):
        nums = [n for n, _ in imagenes]
        estado["llamadas"].append(nums)
        if estado["fallan"] & set(nums):
            estado["fallan"] -= set(nums)
            raise RuntimeError("503")
        return "\n\n".join(f"[Página {n}]\ntexto {n}" for n in nums)

    monkeypatch.setattr(ocr, "_transcribir", transcribir)
    return estado


async def test_reintenta_las_paginas_que_fallaron(motor):
    motor["fallan"] = {1}
    texto, sin_leer = await ocr.transcribir_paginas(_pdf(3), [1, 2, 3])
    assert sin_leer == []
    assert all(f"texto {n}" in texto for n in (1, 2, 3))
    assert texto.index("texto 1") < texto.index("texto 3")


async def test_usa_la_cache_la_segunda_vez(motor):
    data = _pdf(2)
    await ocr.transcribir_paginas(data, [1, 2])
    llamadas = len(motor["llamadas"])
    texto, _ = await ocr.transcribir_paginas(data, [1, 2])
    assert len(motor["llamadas"]) == llamadas
    assert "texto 2" in texto


async def test_respeta_el_tope_de_paginas(motor, monkeypatch):
    monkeypatch.setattr(ocr, "MAX_PAGINAS", 2)
    _, sin_leer = await ocr.transcribir_paginas(_pdf(4), [1, 2, 3, 4])
    assert sin_leer == [3, 4]


@pytest.mark.skipif(not ocr._vision_disponible(), reason="solo macOS con PyObjC")
def test_vision_lee_texto_de_una_imagen():
    doc = fitz.open()
    pagina = doc.new_page()
    pagina.insert_text((72, 100), "Artículo 1.- Objeto de la Ley", fontsize=20)
    import base64
    png = pagina.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes("png")
    texto = ocr._ocr_vision(base64.b64encode(png).decode())
    assert "Objeto de la Ley" in texto


def test_corrige_numerales_romanos_del_ocr():
    texto = ("a) EI artículo Il del Título Preliminar del Código Penal\n"
             "b) El artículo Ill del Título Preliminar\nIl Congreso")
    limpio = ocr.corregir_confusiones(texto)
    assert "El artículo II del Título Preliminar del Código Penal" in limpio
    assert "artículo III del" in limpio
