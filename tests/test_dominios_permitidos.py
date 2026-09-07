"""
La lista blanca de dominios del scraper.

Varios enlaces que el scraper sigue no son constantes: salen del HTML o del
JSON del propio Congreso (adjuntos de expediente, síntesis de agenda, agenda
del Pleno). Estos tests fijan qué se puede seguir y qué no, con foco en los
dominios que *se parecen* al oficial, que son los que un chequeo hecho con
`in` o `startswith` dejaría pasar.
"""
import pytest

from scraper import _url_permitida

OFICIALES = [
    "https://www.congreso.gob.pe/home/",
    "https://api.congreso.gob.pe/spley-portal-service/proyecto-ley",
    "https://wb2server.congreso.gob.pe/spley-portal/#/expediente",
    "https://senado.congreso.gob.pe/",
    "https://diputados.congreso.gob.pe/",
    "https://comunicaciones.congreso.gob.pe/agenda/2026/9/7/",
    "https://www2.congreso.gob.pe/Sicr/TraDocEstProc/",
    "https://congreso.gob.pe/",            # dominio pelado, sin subdominio
    "http://www.congreso.gob.pe/algo",     # http tambien vale: el sitio mezcla
]

EXTERNOS = [
    "https://google.com/",
    "https://www.youtube.com/watch?v=abc",
    "https://congreso.gob.pe.evil.com/x",   # sufijo falso: el host real es evil.com
    "https://notcongreso.gob.pe/x",         # termina igual pero no es subdominio
    "https://congreso.gob.pe.br/x",
    "https://evil.com/?u=https://congreso.gob.pe",  # el oficial va en el query
    "https://elcomercio.pe/politica/nota",
    "",
    "no-es-una-url",
    "javascript:alert(1)",
]


@pytest.mark.parametrize("url", OFICIALES)
def test_deja_pasar_los_del_congreso(url):
    assert _url_permitida(url), f"debería permitir {url}"


@pytest.mark.parametrize("url", EXTERNOS)
def test_bloquea_todo_lo_demas(url):
    assert not _url_permitida(url), f"NO debería permitir {url}"
