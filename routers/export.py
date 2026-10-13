"""Exportación de respuestas a documentos descargables."""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import Response

from services.docx import export_filename, markdown_to_docx

router = APIRouter()

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


@router.post("/export/docx")
async def export_docx(request: Request):
    body = await request.json()
    content = await asyncio.to_thread(markdown_to_docx, body.get("content", ""))
    return Response(
        content=content,
        media_type=DOCX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{export_filename()}"'},
    )
