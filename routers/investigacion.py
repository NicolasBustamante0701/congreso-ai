"""Búsqueda unificada, proyectos seguidos y alertas."""
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from services import busqueda, seguimiento

router = APIRouter()


@router.get("/buscar")
async def buscar(q: str = Query(..., min_length=2, max_length=200)):
    return await busqueda.buscar(q)


@router.get("/seguimiento")
async def listar():
    return {"proyectos": seguimiento.listar(), "revision_horas": seguimiento.REVISION_HORAS}


@router.post("/seguimiento")
async def seguir(request: Request):
    numero = str((await request.json()).get("numero") or "").strip()
    if not numero:
        return JSONResponse({"error": "Falta el número del proyecto."}, status_code=400)
    try:
        return {"proyecto": await seguimiento.seguir(numero)}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@router.delete("/seguimiento/{numero}")
async def dejar(numero: str):
    if not seguimiento.dejar(numero):
        return JSONResponse({"error": "Ese proyecto no estaba en seguimiento."}, status_code=404)
    return {"ok": True}


@router.post("/seguimiento/revisar")
async def revisar(numero: str | None = Query(None)):
    if numero:
        try:
            return await seguimiento.revisar(numero)
        except KeyError:
            return JSONResponse({"error": "Ese proyecto no está en seguimiento."}, status_code=404)
    return await seguimiento.revisar_todos("manual")


@router.get("/alertas")
async def alertas(sin_leer: bool = Query(False)):
    lista = seguimiento.alertas(solo_sin_leer=sin_leer)
    return {"alertas": lista, "sin_leer": sum(1 for a in lista if not a["leida"])
            if not sin_leer else len(lista)}


@router.post("/alertas/leidas")
async def leidas(numero: str | None = Query(None)):
    return {"marcadas": seguimiento.marcar_leidas(numero)}
