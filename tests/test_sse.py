"""vigilar_desconexion: corta y cierra el generador cuando el cliente se va."""
import asyncio

from services import sse


class FakeRequest:
    def __init__(self, desconectar_tras: int):
        self.llamadas = 0
        self.desconectar_tras = desconectar_tras

    async def is_disconnected(self):
        self.llamadas += 1
        return self.llamadas > self.desconectar_tras


async def test_reemite_todo_si_el_cliente_sigue():
    async def gen():
        for i in range(3):
            yield f"ev{i}"

    out = [ev async for ev in sse.vigilar_desconexion(FakeRequest(99), gen())]
    assert out == ["ev0", "ev1", "ev2"]


async def test_cierra_el_generador_al_desconectarse():
    cerrado = asyncio.Event()

    async def gen():
        try:
            for i in range(100):
                yield f"ev{i}"
        finally:
            cerrado.set()

    out = [ev async for ev in sse.vigilar_desconexion(FakeRequest(1), gen())]
    assert out == ["ev0", "ev1"]
    assert cerrado.is_set()


async def test_manda_ping_mientras_espera():
    async def gen():
        await asyncio.sleep(0.25)
        yield "tarde"

    out = [ev async for ev in sse.vigilar_desconexion(FakeRequest(99), gen(), ping_cada=0.1)]
    assert sse.PING in out and out[-1] == "tarde"
