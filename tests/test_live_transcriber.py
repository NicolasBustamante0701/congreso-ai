"""Piezas puras del transcriptor: solape entre ventanas y detección de silencio."""
import array

import live_transcriber as lt


def test_merge_overlap_quita_lo_repetido():
    prev = "el congresista pidió la palabra para"
    nuevo = "la palabra para referirse al presupuesto"
    assert lt._merge_overlap(prev, nuevo) == "referirse al presupuesto"


def test_merge_overlap_sin_coincidencia_deja_todo():
    assert lt._merge_overlap("uno dos tres", "cuatro cinco") == "cuatro cinco"


def test_merge_overlap_con_una_sola_palabra_en_comun_no_recorta():
    # Una palabra suelta ("el") no alcanza para confiar en el solape.
    assert lt._merge_overlap("dijo el", "el ministro") == "el ministro"


def _pcm(amplitud, n=16000):
    return array.array("h", [amplitud if i % 2 else -amplitud for i in range(n)]).tobytes()


def test_silencio_no_va_a_whisper():
    assert lt._is_silence(_pcm(10))
    assert not lt._is_silence(_pcm(5000))
