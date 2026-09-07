"""
Catálogo de herramientas que el modelo puede invocar en la Fase 1 del chat.

TOOLS        — schemas en formato function-calling de OpenAI (el que
               aceptan por igual Gemini, Groq y OpenAI).
TOOL_MAP     — nombre de herramienta → coroutine que la ejecuta.
STATUS_LABELS— texto de progreso que ve el usuario mientras corre cada una.
"""
from config import logger
from scraper import (
    fetch_agenda,
    fetch_agenda_camaras,
    fetch_agenda_comisiones,
    fetch_agenda_pleno,
    fetch_comisiones,
    fetch_congresista,
    fetch_destacados,
    fetch_estado_proyecto,
    fetch_expediente,
    fetch_formula_legal,
    fetch_interpelaciones,
    fetch_proyectos,
    fetch_sesiones,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "buscar_proyectos",
            "description": (
                "Busca proyectos de ley en SPLEY por tema (materia), autor, comisión, "
                "número o rango de fechas (dias=N para últimos N días)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "materia": {
                        "type": "string",
                        "description": "Tema o materia a buscar (ej: 'educacion', 'salud', 'transporte', 'mineria')"
                    },
                    "autor": {
                        "type": "string",
                        "description": "Apellido o nombre del congresista autor del proyecto"
                    },
                    "comision": {
                        "type": "string",
                        "description": "Nombre de la comisión parlamentaria"
                    },
                    "numero": {
                        "type": "string",
                        "description": "Número del proyecto de ley (ej: '14860/2025-CR' o solo '14860')"
                    },
                    "legislatura": {
                        "type": "string",
                        "description": "Período legislativo (default: '2021-2026')"
                    },
                    "dias": {
                        "type": "integer",
                        "description": "Filtrar proyectos presentados en los últimos N días calendario (ej: 15 para los últimos 15 días)"
                    },
                    # Sin esto el modelo no tenía forma de pedir más resultados:
                    # el prompt le ordenaba "volvé a llamar con limit >=
                    # total_disponible" cuando la respuesta venía truncada, pero
                    # `limit` no existía en el esquema. No podía obedecer, así
                    # que terminaba pidiéndole permiso al usuario para reintentar
                    # en vez de traer el listado completo.
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Cuántos proyectos devolver. Default 20, o 100 si se usa `dias`. "
                            "Si una respuesta previa vino con truncado=true, volvé a llamar "
                            "con limit >= total_disponible para tener el listado entero."
                        )
                    },
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_sesiones",
            "description": "Sesiones PASADAS de comisiones del Congreso (debates, votaciones, reuniones ya ocurridas).",
            "parameters": {
                "type": "object",
                "properties": {
                    "comision": {
                        "type": "string",
                        "description": "Nombre de la comisión"
                    },
                    "fecha": {
                        "type": "string",
                        "description": "Fecha en formato YYYY-MM-DD"
                    },
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_agenda",
            "description": "Agenda parlamentaria general: convocatorias, fechas y horarios de próximas sesiones.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_destacados",
            "description": (
                "Secciones DESTACADO y CITACIONES (con links de descarga) de Congreso/Senado/"
                "Diputados, más documentos oficiales descargables. Úsala para pedidos de "
                "documentos, citaciones o descargas — nunca respondas que no puedes darlas."
            ),
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_congresista",
            "description": "Perfil de un congresista: proyectos presentados, estado de cada uno, y noticias recientes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {
                        "type": "string",
                        "description": "Nombre o apellido del congresista (ej: 'Montoya', 'Patricia Chirinos')"
                    }
                },
                "required": ["nombre"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "rastrear_proyecto",
            "description": "Estado de una línea de un proyecto puntual por número — solo el status, no el trámite detallado (para eso, fetch_expediente).",
            "parameters": {
                "type": "object",
                "properties": {
                    "numero": {
                        "type": "string",
                        "description": "Número del proyecto (ej: '1234/2024-CR' o simplemente '1234')"
                    }
                },
                "required": ["numero"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_expediente",
            "description": (
                "Expediente COMPLETO de un proyecto (5 pestañas: seguimiento, acumulados, "
                "documentación anexa, secciones, opinión ciudadana). Usar para trámite, "
                "comisiones, adjuntos o predictamen de un proyecto. Si ya viene el número, "
                "llamar DIRECTO — no hace falta buscar_proyectos/rastrear_proyecto antes. "
                "Un solo llamado cubre AMBAS cámaras (no repetir por Senado/Diputados)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "numero_proyecto": {
                        "type": "string",
                        "description": "Número del proyecto de ley. Acepta formato oficial completo ('14864/2025-CR') o solo el correlativo ('14864')."
                    },
                    "periodo": {
                        "type": "string",
                        "description": "Periodo parlamentario, ej. '2021' para el periodo 2021-2026. Por defecto usa el periodo vigente."
                    }
                },
                "required": ["numero_proyecto"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_agenda_comisiones",
            "description": (
                "Sesiones de comisiones próximas SIN distinguir cámara (junta Senado+Diputados). "
                "Usar SOLO si el usuario no nombra una cámara puntual — si nombra Senado, "
                "Diputados o Pleno, usar fetch_agenda_camaras en su lugar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dias": {
                        "type": "integer",
                        "description": "Cantidad de días hacia adelante a consultar. Por defecto 2."
                    },
                    "comision": {
                        "type": "string",
                        "description": "Opcional. Filtrar por nombre (o parte del nombre) de una comisión específica, ej. 'Energía y Minas'."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_agenda_pleno",
            "description": "Estructura real (por secciones e ítems) de la Agenda del Pleno vigente: qué se va a debatir.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_comisiones",
            "description": (
                "Cuadro de comisiones ordinarias del Senado y la Cámara de Diputados "
                "(cada cámara tiene el suyo, no un total combinado) más accesos "
                "oficiales. Llamar UNA sola vez por turno (sin 'camara', ya trae "
                "ambas) — nunca respondas que no tienes acceso."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "camara": {
                        "type": "string",
                        "description": "Opcional. 'senado' o 'diputados' para priorizar los enlaces de esa cámara en la respuesta. Omitir para incluir ambas — no combines esto con múltiples llamadas."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "leer_formula_legal",
            "description": (
                "Texto real de un proyecto (fórmula legal, articulado) para resumir o analizar "
                "el contenido — ej. qué ley modifica. fetch_expediente NO trae este texto."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "numero_proyecto": {
                        "type": "string",
                        "description": "Número del proyecto de ley, ej. '14864/2025-CR' o '14864'."
                    }
                },
                "required": ["numero_proyecto"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "responder_directo",
            "description": "Sin datos externos: saludos, seguimiento de algo ya respondido, o conceptos que ya sabes.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_agenda_camaras",
            "description": (
                "Sesiones del Senado, Diputados, Pleno o comisiones — SÍ distingue cámara "
                "(camara='senado'/'diputados'/'pleno'/'comision'). Usar siempre que el usuario "
                "nombre una cámara puntual, en vez de fetch_agenda_comisiones."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dias": {
                        "type": "integer",
                        "description": "Días hacia adelante a consultar (default: 2)."
                    },
                    "camara": {
                        "type": "string",
                        "description": "Filtrar por cámara: 'senado', 'diputados', 'pleno', 'comision'. Omitir para ver todas."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_interpelaciones",
            "description": (
                "Mociones de interpelación a ministros, del Sistema oficial de Mociones "
                "de Orden del Día del Congreso, con su estado real de trámite. Solo "
                "devuelve mociones efectivamente PRESENTADAS: si un medio anunció una y "
                "no está acá, no fue presentada."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ministro": {
                        "type": "string",
                        "description": "Opcional. Filtrar por nombre del ministro o de la cartera, ej. 'Interior' o 'Ministro de Salud'."
                    }
                }
            }
        }
    }
]

async def _responder_directo():
    return {"nota": "Responde directamente con tu conocimiento, sin datos externos."}


TOOL_MAP = {
    "buscar_proyectos":        lambda args: fetch_proyectos(**args),
    "buscar_sesiones":         lambda args: fetch_sesiones(**args),
    "buscar_agenda":           lambda args: fetch_agenda(),
    "buscar_destacados":       lambda args: fetch_destacados(),
    "buscar_congresista":      lambda args: fetch_congresista(**args),
    "rastrear_proyecto":       lambda args: fetch_estado_proyecto(**args),
    "fetch_expediente":        lambda args: fetch_expediente(
                                   numero=args.get("numero_proyecto") or args.get("numero", "")
                               ),
    "fetch_agenda_comisiones": lambda args: fetch_agenda_comisiones(**{k: v for k, v in args.items() if k in ("dias", "comision")}),
    "fetch_agenda_pleno":      lambda args: fetch_agenda_pleno(),
    "fetch_comisiones":        lambda args: fetch_comisiones(**{k: v for k, v in args.items() if k in ("camara",)}),
    "fetch_agenda_camaras":    lambda args: fetch_agenda_camaras(**{k: v for k, v in args.items() if k in ("dias", "camara")}),
    "fetch_interpelaciones":   lambda args: fetch_interpelaciones(**{k: v for k, v in args.items() if k in ("ministro",)}),
    "leer_formula_legal":      lambda args: fetch_formula_legal(**{k: v for k, v in args.items() if k in ("numero_proyecto",)}),
    "responder_directo":       lambda args: _responder_directo(),
}

STATUS_LABELS = {
    "buscar_proyectos":        "Buscando proyectos de ley en SPLEY...",
    "buscar_sesiones":         "Consultando sesiones del Congreso...",
    "buscar_agenda":           "Obteniendo agenda parlamentaria...",
    "buscar_destacados":       "Buscando destacados, citaciones y documentos...",
    "buscar_congresista":      "Consultando perfil del congresista...",
    "rastrear_proyecto":       "Rastreando estado del proyecto...",
    "fetch_expediente":        "Consultando el expediente completo en SPLEY (5 pestañas)...",
    "fetch_agenda_comisiones": "Revisando agenda de comisiones...",
    "fetch_agenda_pleno":      "Cargando la Agenda del Pleno...",
    "fetch_comisiones":        "Consultando el cuadro de comisiones...",
    "fetch_agenda_camaras":    "Revisando agenda del Congreso bicameral...",
    "fetch_interpelaciones":   "Buscando mociones de interpelación...",
    "leer_formula_legal":      "Leyendo el texto real del proyecto...",
    "responder_directo":       "Pensando...",
}
