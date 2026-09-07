"""Ficha del expediente de una proposición legislativa.

El expediente ya lo raspaba `scraper.fetch_expediente`, pero solo estaba
expuesto como herramienta del LLM (services/tools.py): la única forma de verlo
era pidiéndoselo al chat, que lo devolvía como tabla markdown. Acá se expone
por HTTP para que el frontend pueda pintar la ficha igual que el portal.
"""
from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from config import logger, static_file
from scraper import fetch_expediente

router = APIRouter()


@router.get("/expediente-view", response_class=HTMLResponse)
async def expediente_view():
    return static_file("expediente.html")


@router.get("/expediente")
async def expediente(numero: str = Query(..., description="Número de la proposición, ej. 00088-2026-2031-CD")):
    """
    Ficha completa: general, seguimientos con adjuntos, acumulados,
    documentación anexa y opinión ciudadana.

    El número va como query param y no como parte del path a propósito: los
    números viejos traen barra (`14864/2025-CR`) y romperían el ruteo.
    """
    numero = (numero or "").strip()
    if not numero:
        return {"error": "Falta el número de la proposición."}

    try:
        data = await fetch_expediente(numero)
    except Exception as e:
        logger.warning("fetch_expediente falló para %s: %s", numero, e)
        return {"error": f"No se pudo consultar el expediente de {numero}."}

    return data
