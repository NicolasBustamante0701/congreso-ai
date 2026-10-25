"""Rutas de documentos: catálogo de PDFs, miniaturas, carga y subida."""
import asyncio

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

from config import logger, static_file
from scraper import fetch_destacados, fetch_proyectos
from services import documentos, tool_cache
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
        # Misma caché que la herramienta del chat: sin ella cada apertura de
        # Documentos tardaba ~30 s (la agenda del Pleno se baja y se lee entera).
        data = tool_cache.get("buscar_destacados", {})
        if data is None:
            data = await fetch_destacados()
            if not data.get("sin_datos"):
                tool_cache.put("buscar_destacados", {}, data)
        # Congreso y Senado publican el mismo PDF en sus dos portadas: va una
        # sola vez, con las cámaras que lo publicaron.
        por_enlace = {}
        for camara, info in data.get("camaras", {}).items():
            for clave, tipo in (("destacados", "Destacado"), ("citaciones", "Citación")):
                for item in info.get(clave, []):
                    url = item.get("enlace", "")
                    if not url.lower().endswith(".pdf"):
                        continue
                    if url in por_enlace:
                        if camara not in por_enlace[url]["camaras"]:
                            por_enlace[url]["camaras"].append(camara)
                        continue
                    por_enlace[url] = {"titulo": item["titulo"], "enlace": url,
                                       "tipo": tipo, "camaras": [camara]}
                    pdfs.append(por_enlace[url])

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
        # SPLEY trae el título en `titulo` (antes `sumilla`): sin esto la
        # lista mostraba solo el número, dos veces.
        titulo = item.get("titulo") or item.get("sumilla") or numero
        proyectos.append({
            "titulo": titulo,
            "numero": numero,
            "fecha": item.get("fecha_presentacion", ""),
            "camaras": [item["camara"]] if item.get("camara") else [],
            "enlace": enlace,
            "tipo": "Proyecto de Ley",
        })
    return {"pdfs": proyectos}


def _guardar_completo(nombre: str, origen: str, data: bytes) -> str | None:
    """
    Guarda todas las páginas para preguntas posteriores (services/documentos).
    Si falla, el PDF igual se puede usar con lo que entró al chat.
    """
    try:
        return documentos.guardar(nombre, origen, pdf_service.extract_pages(data))
    except Exception as e:
        logger.warning("no se pudo guardar el PDF completo %s: %s", nombre, e)
        return None


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
        doc_id = await asyncio.to_thread(_guardar_completo, url.split("/")[-1], url, r.content)
        return {"ok": True, "pages": pages, "text": text, "filename": url.split("/")[-1], "doc_id": doc_id}
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
        doc_id = await asyncio.to_thread(_guardar_completo, file.filename or "documento.pdf", "archivo local", data)
        return {"ok": True, "pages": pages, "text": text, "filename": file.filename, "doc_id": doc_id}
    except Exception as e:
        logger.warning("upload_pdf falló para %s: %s", file.filename, e)
        return {"ok": False, "error": f"No se pudo leer el PDF: {e}"}
