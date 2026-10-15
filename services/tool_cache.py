"""
Caché en memoria de resultados de herramientas, con vencimiento por tipo.

Pedir dos veces lo mismo en la misma conversación ("¿y el 14864?" → ficha →
"¿qué comisiones tiene?") volvía a pegarle al Congreso cada vez: el
expediente son 5 requests a SPLEY. Lo que se guarda y por cuánto:

- Expediente y fórmula legal: 30 min. Cambian pocas veces al día.
- Comisiones: 24 h. Son datos fijos del Reglamento, sin red.
- Todo lo demás (búsquedas, agendas, destacados): 5 min. Puede cambiar en
  el día y una agenda vieja presentada como actual hace daño.

Solo se guardan resultados buenos: ni errores ni "sin_datos", que pueden
ser un hipo de la fuente. Cada resultado servido desde acá lleva
"consultado" (hora de Lima en que se pidió de verdad) para que la respuesta
pueda decir de cuándo es el dato.
"""
import copy
import json
import time

from config import ahora_lima

TTL_SEGUNDOS = {
    "fetch_expediente": 30 * 60,
    "leer_formula_legal": 30 * 60,
    "fetch_comisiones": 6 * 3600,
    "fetch_miembros_comision": 6 * 3600,
    "fetch_mesa_directiva": 6 * 3600,
}
TTL_DEFAULT = 5 * 60
MAX_ENTRADAS = 200

_cache: dict[str, tuple[float, object]] = {}


def _clave(name: str, args: dict) -> str:
    return name + ":" + json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)


def get(name: str, args: dict):
    k = _clave(name, args)
    entrada = _cache.get(k)
    if entrada is None:
        return None
    vence, valor = entrada
    if time.monotonic() >= vence:
        _cache.pop(k, None)
        return None
    valor = copy.deepcopy(valor)
    if isinstance(valor, dict):
        valor["desde_cache"] = True
    return valor


def put(name: str, args: dict, valor):
    """Guarda y devuelve el valor con la hora de consulta agregada."""
    if isinstance(valor, dict):
        valor = {**valor, "consultado": ahora_lima().strftime("%d/%m/%Y %H:%M")}
    if len(_cache) >= MAX_ENTRADAS:
        # El más próximo a vencer sale primero: sin LRU, pero acotado.
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
    _cache[_clave(name, args)] = (time.monotonic() + TTL_SEGUNDOS.get(name, TTL_DEFAULT),
                                  copy.deepcopy(valor))
    return valor


def clear():
    _cache.clear()
