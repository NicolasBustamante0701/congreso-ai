"""
Conversión de Markdown a .docx.

Soporta encabezados, listas, tablas, negrita/cursiva e hipervínculos — el
subconjunto que genera el modelo en sus respuestas.
"""
import io
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from config import ahora_lima

GREY = RGBColor(0x88, 0x88, 0x88)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
HEADER_FILL = "1a1a1a"

# Divide un párrafo en tokens de link / negrita / cursiva conservando el resto.
INLINE_RE = re.compile(r'(\[[^\]]+\]\([^)]+\)|\*\*[^*]+\*\*|\*[^*]+\*)')
LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
HYPERLINK_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"


def _strip_inline(text: str) -> str:
    """Quita el marcado inline — para celdas de tabla, que van en texto plano."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    return re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)


def _add_hyperlink(paragraph, text: str, url: str):
    """Añade un run con hipervínculo real (python-docx no lo soporta nativamente)."""
    r_id = paragraph.part.relate_to(url, HYPERLINK_REL, is_external=True)
    hyperlink = OxmlElement('w:hyperlink')
    hyperlink.set(qn('r:id'), r_id)

    run = OxmlElement('w:r')
    r_pr = OxmlElement('w:rPr')
    r_style = OxmlElement('w:rStyle')
    r_style.set(qn('w:val'), 'Hyperlink')
    r_pr.append(r_style)
    run.append(r_pr)

    t = OxmlElement('w:t')
    t.text = text
    run.append(t)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def _fill_inline(paragraph, text: str):
    """Escribe `text` en el párrafo respetando links, negrita y cursiva."""
    for tok in INLINE_RE.split(text):
        link = LINK_RE.match(tok)
        if link:
            _add_hyperlink(paragraph, link.group(1), link.group(2))
        elif tok.startswith('**') and tok.endswith('**'):
            paragraph.add_run(tok[2:-2]).bold = True
        elif tok.startswith('*') and tok.endswith('*'):
            paragraph.add_run(tok[1:-1]).italic = True
        else:
            paragraph.add_run(tok)


def _is_table_row(s: str) -> bool:
    # El modelo a veces omite el "|" final ("| a | b"), que GFM acepta igual.
    return s.strip().startswith('|') and s.count('|') >= 2


def _is_separator_row(s: str) -> bool:
    return _is_table_row(s) and bool(re.match(r'^\|?[\s\-|:]+\|?$', s.strip())) and '-' in s


def _parse_row(s: str) -> list:
    """
    Celdas de una fila. Respeta "\\|" (pipe escapado dentro de una celda):
    antes partía la celda en dos y corría todas las columnas siguientes.
    """
    s = s.strip()
    if s.startswith('|'):
        s = s[1:]
    if s.endswith('|') and not s.endswith('\\|'):
        s = s[:-1]
    celdas = re.split(r'(?<!\\)\|', s)
    return [c.replace('\\|', '|').strip() for c in celdas]


# Ancho de un carácter en las celdas, aproximado (Calibri 11).
_ANCHO_CARACTER_IN = 0.075
_MAX_CORTA = 24   # mismo criterio que marcarColumnasCortas() en static/app.js


def _anchos_por_contenido(doc, tbl, filas: list):
    """
    Columnas cortas (número, fecha, hora: ninguna celda pasa de 24 caracteres)
    con el ancho justo y el resto de la página para las largas. Con anchos
    iguales, en un cuadro de proyectos el título quedaba en un tercio de la
    hoja y el número del proyecto ocupaba otro tercio.
    """
    sec = doc.sections[-1]
    total = (sec.page_width - sec.left_margin - sec.right_margin) / 914400   # EMU → pulgadas
    largos = [max(len(_strip_inline(f[c])) for f in filas) for c in range(len(filas[0]))]
    cortas = {c: min(1.6, max(0.6, n * _ANCHO_CARACTER_IN + 0.25))
              for c, n in enumerate(largos) if n <= _MAX_CORTA}
    resto = len(largos) - len(cortas)
    if not cortas or not resto:
        return
    libre = max(total - sum(cortas.values()), 1.5 * resto)
    anchos = [cortas.get(c, libre * largos[c] / sum(largos[i] for i in range(len(largos)) if i not in cortas))
              for c in range(len(largos))]
    tbl.autofit = False
    for fila in tbl.rows:
        for c, celda in enumerate(fila.cells):
            celda.width = Inches(anchos[c])


def _add_table(doc, rows: list):
    # Todas las filas con el ancho del encabezado: una fila con más celdas
    # reventaba con IndexError (y el export entero fallaba); una con menos
    # dejaba celdas sin tocar. Las de más se juntan en la última columna para
    # no perder texto.
    ncols = len(rows[0])
    norm = []
    for row in rows:
        if len(row) > ncols:
            row = row[:ncols - 1] + [" | ".join(row[ncols - 1:])]
        norm.append(row + [""] * (ncols - len(row)))

    tbl = doc.add_table(rows=len(norm), cols=ncols)
    tbl.style = 'Table Grid'
    _anchos_por_contenido(doc, tbl, norm)
    for r_idx, row in enumerate(norm):
        for c_idx, cell_text in enumerate(row):
            cell = tbl.cell(r_idx, c_idx)
            para = cell.paragraphs[0]
            if r_idx != 0:
                # Celdas del cuerpo con links y negritas reales: la primera
                # columna de las tablas de proyectos es el link al expediente.
                _fill_inline(para, cell_text)
                continue
            # Fila de encabezado: negrita, texto blanco, fondo oscuro.
            run = para.add_run(_strip_inline(cell_text))
            run.bold = True
            run.font.color.rgb = WHITE
            shd = OxmlElement('w:shd')
            shd.set(qn('w:val'), 'clear')
            shd.set(qn('w:color'), 'auto')
            shd.set(qn('w:fill'), HEADER_FILL)
            cell._tc.get_or_add_tcPr().append(shd)
    doc.add_paragraph()


def _add_banner(doc, text: str):
    para = doc.add_paragraph()
    run = para.add_run(text)
    run.font.size = Pt(8)
    run.font.color.rgb = GREY
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER


def markdown_to_docx(md: str) -> bytes:
    """Convierte Markdown a un .docx y devuelve sus bytes."""
    doc = Document()

    for section in doc.sections:
        section.left_margin = Inches(1.2)
        section.right_margin = Inches(1.2)
        section.top_margin = Inches(1.0)
        section.bottom_margin = Inches(1.0)

    lines = md.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()

        # Bloque de tabla: encabezado + separador + filas.
        if (_is_table_row(line) and i + 1 < len(lines)
                and _is_separator_row(lines[i + 1].rstrip())):
            rows = [_parse_row(line)]
            i += 2  # saltar encabezado y separador
            while i < len(lines) and _is_table_row(lines[i].rstrip()):
                rows.append(_parse_row(lines[i].rstrip()))
                i += 1
            _add_table(doc, rows)
            continue

        if line.startswith('### '):
            doc.add_heading(_strip_inline(line[4:]), level=3)
        elif line.startswith('## '):
            doc.add_heading(_strip_inline(line[3:]), level=2)
        elif line.startswith('# '):
            doc.add_heading(_strip_inline(line[2:]), level=1)
        elif line.startswith('---'):
            doc.add_paragraph('─' * 60)
        elif re.match(r'^[-*]\s+', line):
            para = doc.add_paragraph(style='List Bullet')
            _fill_inline(para, re.sub(r'^[-*]\s+', '', line))
        elif re.match(r'^\d+\.\s+', line):
            _fill_inline(doc.add_paragraph(style='List Number'), re.sub(r'^\d+\.\s+', '', line))
        elif line == '':
            doc.add_paragraph()
        else:
            _fill_inline(doc.add_paragraph(), line)
        i += 1

    doc.add_paragraph()
    _add_banner(
        doc,
        f"Generado por Diana — Sistema de Monitoreo Parlamentario · "
        f"{ahora_lima().strftime('%d/%m/%Y')}",
    )

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def export_filename() -> str:
    return f"Resumen-Congreso-{ahora_lima().strftime('%Y-%m-%d')}.docx"
