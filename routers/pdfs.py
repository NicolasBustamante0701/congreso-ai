"""Rutas de documentos: catálogo de PDFs, miniaturas, carga y subida."""
import asyncio

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

from config import logger, static_file
from scraper import fetch_destacados, fetch_proyectos
from services import pdf as pdf_service

router = APIRouter()

REFERENCIAS_PDF = [
    {
        "titulo": "Reglamento del Congreso de la República (setiembre 2025)",
        "enlace": "https://www3.congreso.gob.pe/Docs/constitucion/reglamento/reglamento%20setiembre-2025.pdf",
        "tipo": "Referencia",
    },
    {
        "titulo": "Constitución Política del Perú (dic. 2024)",
        "enlace": "https://www3.congreso.gob.pe/Docs/files/constitucion/constitucion-12-2024.pdf",
        "tipo": "Referencia",
    },
    {
        "titulo": "Manual de Técnica Legislativa — 3ra edición",
        "enlace": "https://www3.congreso.gob.pe/Docs/dgp/files/manual-tecnica-legislativa-3raedicion.pdf",
        "tipo": "Referencia",
    },
]


@router.get("/pdfs", response_class=HTMLResponse)
async def pdfs_page():
    return static_file("pdfs.html")


@router.get("/congreso-pdfs")
async def congreso_pdfs():
    """PDFs rápidos: destacados/citaciones de Congreso, Senado y Diputados + referencias fijas."""
    pdfs = []
    try:
        data = await fetch_destacados()
        for camara, info in data.get("camaras", {}).items():
            for clave, tipo in (("destacados", "Destacado"), ("citaciones", "Citación")):
                for item in info.get(clave, []):
                    url = item.get("enlace", "")
                    if url.lower().endswith(".pdf"):
                        pdfs.append({"titulo": item["titulo"], "enlace": url,
                                     "tipo": f"{tipo} — {camara}"})

        # Cuando ninguna cámara publica destacados ni citaciones, el scraper
        # devuelve los documentos que sí están disponibles (agendas del Pleno).
        # Sin esto el panel se quedaba solo con las 3 referencias fijas.
        for item in data.get("documentos_disponibles", []):
            url = item.get("enlace", "")
            if url.lower().endswith(".pdf"):
                pdfs.append({
                    "titulo": item.get("titulo", ""),
                    "enlace": url,
                    "tipo": item.get("tipo", "Documento"),
                })
    except Exception as e:
        logger.warning("fetch_destacados falló en /congreso-pdfs: %s", e)

    seen = {p["enlace"] for p in pdfs}
    for ref in REFERENCIAS_PDF:
        if ref["enlace"] not in seen:
            seen.add(ref["enlace"])
            pdfs.append(ref)
    return {"pdfs": pdfs}


@router.get("/congreso-proyectos")
async def congreso_proyectos():
    """Proyectos SPLEY recientes — el frontend los carga en segundo plano."""
    try:
        data = await fetch_proyectos(limit=15)
    except Exception as e:
        logger.warning("fetch_proyectos falló en /congreso-proyectos: %s", e)
        return {"pdfs": []}

    proyectos = []
    for item in data.get("items", []):
        enlace = item.get("enlace", "")
        if not enlace:
            continue
        numero = item.get("numero", "")
        titulo = item.get("sumilla", numero)
        proyectos.append({
            "titulo": f"[{numero}] {titulo[:100]}" if numero else titulo[:110],
            "enlace": enlace,
            "tipo": "Proyecto de Ley",
        })
    return {"pdfs": proyectos}


@router.get("/pdf-thumbnail")
async def pdf_thumbnail(url: str = Query(...)):
    try:
        r = await pdf_service.download(url, timeout=20)
        if not pdf_service.looks_like_pdf(r):
            return Response(status_code=404, content=b"not a pdf")
        png = await asyncio.to_thread(pdf_service.render_thumbnail, r.content)
        return Response(content=png, media_type="image/png")
    except pdf_service.UrlNoPermitida as e:
        return Response(status_code=400, content=str(e).encode())
    except Exception as e:
        logger.warning("pdf_thumbnail falló para %s: %s", url, e)
        return Response(status_code=502, content=b"no se pudo generar la miniatura")


@router.post("/load-pdf-url")
async def load_pdf_url(request: Request):
    body = await request.json()
    url = body.get("url", "")
    try:
        r = await pdf_service.download(url)
        if not pdf_service.looks_like_pdf(r):
            return JSONResponse({"ok": False, "error": f"La URL no devolvió un PDF (HTTP {r.status_code})."})
        text, pages = await asyncio.to_thread(pdf_service.extract_text, r.content)
        return {"ok": True, "pages": pages, "text": text, "filename": url.split("/")[-1]}
    except pdf_service.UrlNoPermitida as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning("load_pdf_url falló para %s: %s", url, e)
        return {"ok": False, "error": f"No se pudo leer el PDF: {e}"}


@router.post("/upload-pdf")
async def upload_pdf(file: UploadFile = File(...)):
    data = await file.read(pdf_service.MAX_PDF_BYTES + 1)
    if len(data) > pdf_service.MAX_PDF_BYTES:
        mb = pdf_service.MAX_PDF_BYTES // (1024 * 1024)
        return JSONResponse({"ok": False, "error": f"El PDF supera {mb} MB."}, status_code=413)
    try:
        text, pages = await asyncio.to_thread(pdf_service.extract_text, data)
        return {"ok": True, "pages": pages, "text": text, "filename": file.filename}
    except Exception as e:
        logger.warning("upload_pdf falló para %s: %s", file.filename, e)
        return {"ok": False, "error": f"No se pudo leer el PDF: {e}"}
