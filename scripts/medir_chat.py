"""
Línea base de rendimiento del chat: corre consultas fijas contra un server
real y deja los tiempos en un CSV.

    .venv/bin/python scripts/medir_chat.py --etiqueta antes
    .venv/bin/python scripts/medir_chat.py --etiqueta despues --repeticiones 2

Levanta su propio server en otro puerto (no choca con la app abierta) y usa
las keys del .env: los tiempos incluyen la espera real del proveedor de IA y
de los portales del Congreso, que varía entre corridas. Para comparar,
correr las dos versiones seguidas y mirar la mediana, no una corrida suelta.

Columnas: primer_evento (cualquier cosa en pantalla), primer_texto (primer
token de la respuesta), total, y las herramientas que usó (del log
CHAT_METRICA del server).
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parent.parent
CONSULTAS = [
    "hola",
    "proyectos de ley sobre agua de los últimos 30 días",
    "¿en qué estado está el proyecto 10000?",
    "agenda del pleno de esta semana",
    "¿qué comisiones hay en el Senado?",
]


def esperar(url, segundos=60):
    fin = time.monotonic() + segundos
    while time.monotonic() < fin:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.4)
    return False


def medir(base, consulta):
    inicio = time.monotonic()
    primer_evento = primer_texto = None
    error = ""
    chars = 0
    with httpx.stream("POST", f"{base}/chat", timeout=180,
                      json={"messages": [{"role": "user", "content": consulta}]}) as r:
        for linea in r.iter_lines():
            if not linea.startswith("data: "):
                continue
            dato = linea[6:]
            if dato == "[DONE]":
                break
            ahora = time.monotonic() - inicio
            primer_evento = primer_evento or ahora
            ev = json.loads(dato)
            if "text" in ev:
                primer_texto = primer_texto or ahora
                chars += len(ev["text"])
            if "error" in ev:
                error = ev["error"][:120]
    return primer_evento, primer_texto, time.monotonic() - inicio, chars, error


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--etiqueta", required=True)
    ap.add_argument("--repeticiones", type=int, default=1)
    ap.add_argument("--puerto", type=int, default=8741)
    a = ap.parse_args()

    log_dir = RAIZ / "scripts" / "out"
    log_dir.mkdir(exist_ok=True)
    env = {**os.environ, "PORT": str(a.puerto), "DIANA_LOG_DIR": str(log_dir)}
    srv = subprocess.Popen([sys.executable, str(RAIZ / "server.py")], cwd=RAIZ, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{a.puerto}"
    salida = log_dir / f"medicion-{a.etiqueta}.csv"
    try:
        t0 = time.monotonic()
        if not esperar(f"{base}/status"):
            sys.exit("el server no levantó")
        arranque = time.monotonic() - t0
        print(f"arranque del server: {arranque:.2f}s")
        with open(salida, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["etiqueta", "consulta", "rep", "primer_evento_s", "primer_texto_s",
                        "total_s", "chars", "error"])
            for rep in range(a.repeticiones):
                for q in CONSULTAS:
                    pe, pt, tot, chars, err = medir(base, q)
                    fila = [a.etiqueta, q, rep, f"{pe or 0:.2f}", f"{pt or 0:.2f}",
                            f"{tot:.2f}", chars, err]
                    w.writerow(fila)
                    print(" | ".join(map(str, fila[1:])))
        print(f"→ {salida}")
    finally:
        srv.terminate()
        srv.wait(10)


if __name__ == "__main__":
    main()
