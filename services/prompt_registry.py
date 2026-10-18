"""
Prompts cargados desde prompts/*.md al arranque.

Un solo punto de import para todo el prompt del sistema: los routers y el
orquestador piden constantes de aquí en vez de leer archivos por su cuenta.
"""
import re
from datetime import datetime

from config import load_prompt
from scraper import estado_legislativo_texto

RESUMEN_PROMPT = load_prompt("resumen")
ROUTER_PROMPT  = load_prompt("router")
SYSTEM_BASE    = load_prompt("system_base")

# System prompt compacto para Fase 3 con tool results — ahorra ~800 tokens
# vs SYSTEM_BASE. Lleva un placeholder {hoy}.
SYSTEM_MINI = load_prompt("system_mini")

# Largo de la respuesta que elige el usuario (Ajustes o el selector del chat).
# "media" no agrega nada: es el comportamiento de siempre.
EXTENSIONES = ("corta", "media", "larga")
NOTA_EXTENSION = {
    m.group(1): m.group(2).strip()
    for m in re.finditer(r"(?ms)^## (corta|larga)\n(.*?)(?=^## (?:corta|larga)\n|\Z)",
                         load_prompt("extension_respuestas"))
}

LIVE_ANALYSIS_PROMPT = load_prompt("live_analysis")

# Bloques de formato inyectados en Fase 3 según la herramienta usada.
WORKFLOWS = {
    "fetch_expediente":        load_prompt("workflow_expediente"),
    "rastrear_proyecto":       load_prompt("workflow_rastrear_proyecto"),
    "leer_formula_legal":      load_prompt("workflow_formula_legal"),
    "fetch_agenda_comisiones": load_prompt("workflow_agenda_comisiones"),
    "fetch_agenda_pleno":      load_prompt("workflow_agenda_pleno"),
    "fetch_interpelaciones":   load_prompt("workflow_interpelaciones"),
    "fetch_agenda_camaras":    load_prompt("workflow_agenda_camaras"),
    "buscar_proyectos":        load_prompt("workflow_proyectos"),
    "fetch_comisiones":        load_prompt("workflow_comisiones"),
    "fetch_miembros_comision": load_prompt("workflow_miembros_comision"),
    "consultar_normas":        load_prompt("workflow_normas"),
    "buscar_destacados":       load_prompt("workflow_destacados"),
}

# Flujos que dependen de un PDF/transcript cargado, no de una herramienta.
WORKFLOW_PDF_FORMULA = load_prompt("workflow_pdf")
WORKFLOW_SESION      = load_prompt("workflow_sesion")

# Nota de fecha que se añade a SYSTEM_BASE en cada request para evitar
# alucinaciones temporales y respuestas secas cuando no hay sesiones.
#
# {estado_legislativo} se calcula con el calendario real del Congreso (ver
# scraper.estado_legislativo) — antes esto le pedía al modelo "explicar el
# posible motivo... según la fecha", es decir, ADIVINAR si era receso o
# feriado. Ahora se lo decimos con certeza, calculado, no inventado.
FECHA_NOTA = (
    "\n\n**Fecha actual: {hoy}** — Solo muestra sesiones o eventos a partir de hoy. "
    "{estado_legislativo} "
    "Si una herramienta no devuelve sesiones reales, NO digas simplemente 'no hay sesiones'. "
    "En cambio: (1) explica el motivo real usando el estado legislativo de arriba — "
    "nunca lo adivines, ya lo sabes con certeza — (2) sugiere alternativas concretas "
    "como revisar la agenda de la semana siguiente, consultar proyectos de ley en "
    "trámite, o revisar los destacados y citaciones. Sé directo y útil, no te limites "
    "a dar una negativa seca."
)


def system_con_fecha(hoy: str) -> str:
    """SYSTEM_BASE + la nota de fecha del día + el estado legislativo real."""
    hoy_date = datetime.strptime(hoy, "%d/%m/%Y").date()
    return SYSTEM_BASE + FECHA_NOTA.format(hoy=hoy, estado_legislativo=estado_legislativo_texto(hoy_date))


def resumen_con_fechas(hoy: str, desde: str) -> str:
    """
    RESUMEN_PROMPT con la ventana semanal resuelta + el estado legislativo real.

    Sin la ventana, el prompt llegaba con un literal "[fecha actual]" y el
    modelo no tenía forma de saber qué era "esta semana": las noticias
    indexadas y las agendas anteriores que devuelven las herramientas entraban
    al informe como si fueran de la semana en curso. Sin el estado
    legislativo, el "sin actividad" del informe le pedía al modelo adivinar
    el motivo en vez de decir el real.
    """
    hoy_date = datetime.strptime(hoy, "%d/%m/%Y").date()
    return RESUMEN_PROMPT.format(
        hoy=hoy, desde=desde,
        estado_legislativo=estado_legislativo_texto(hoy_date),
    )


def build_sesion_prompt(titulo: str, texto: str) -> str:
    """Prompt de resumen de sesión — compartido por /sesiones/resumir y resumir-texto."""
    return f"""Analiza este transcript de la sesión del Congreso del Perú titulada: "{titulo}".

Genera un resumen estructurado con este formato EXACTO:

## Resumen — {titulo}

### Temas tratados
[Tabla: Tema | Descripción | Resultado/Estado]

### Proyectos o normas mencionados
[Tabla: Número/Nombre | Tema | Posición mayoritaria]

### Votaciones o acuerdos
[Tabla: Asunto | A favor | En contra | Resultado]

### Puntos destacados
[Lista de los 3-5 momentos más relevantes de la sesión]

---
Transcript de la sesión:
{texto[:40000]}"""
