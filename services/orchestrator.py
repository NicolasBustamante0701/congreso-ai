"""
ChatOrchestrator — las tres fases del endpoint /chat.

  Fase 1 (router, 8B)  elige qué herramienta hace falta, o ninguna.
  Fase 2               ejecuta las herramientas elegidas y recorta sus resultados.
  Fase 3 (main, 70B)   redacta la respuesta final en streaming.

Hay además un atajo: analizar un PDF ya cargado o una sesión de YouTube no
necesita scraping, así que va directo a Fase 3 con el workflow correspondiente.

Todos los métodos públicos son generadores asíncronos que emiten strings SSE
ya formateados (ver services/sse.py).
"""
import asyncio
import json
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

from config import MAIN_MODEL, ROUTER_MODEL, logger
from scraper import fetch_transcript_youtube
from services import llm, sse
from services.prompt_registry import (
    ROUTER_PROMPT,
    SYSTEM_MINI,
    WORKFLOW_PDF_FORMULA,
    WORKFLOW_SESION,
    WORKFLOWS,
    resumen_con_fechas,
    system_con_fecha,
)
from services.tools import STATUS_LABELS, TOOL_MAP, TOOLS

# Un tool result de 7k chars ≈ 2000 tokens. Más que eso dispara el TPM.
MAX_TOOL_RESULT_CHARS = 7000

# Tope propio para las herramientas que devuelven listados largos.
#
# Con 7000 el JSON de un "últimos 15 días" (89 proyectos, ~400 caracteres cada
# uno entre título, sumilla, autores y enlace) se cortaba a la mitad: el modelo
# recibía un JSON inválido terminado en "[recortado]" y no podía clasificar por
# temas ni citar los enlaces de los que no llegó a ver. Subirlo para todas las
# herramientas no hace falta —las demás devuelven objetos chicos— y encarece
# cada pedido, así que el margen extra va solo donde el listado lo justifica.
# Medido: 89 proyectos (una consulta real de "últimos 15 días") ocupan ~49 mil
# caracteres ya con el payload aligerado — sin sumilla duplicada, sin campos
# vacíos y con los autores acotados a tres. 60 mil deja margen para una ventana
# algo más cargada sin volver a cortar por la mitad.
MAX_TOOL_RESULT_CHARS_POR_TOOL = {
    "buscar_proyectos": 60000,
}


def _recortar_resultado(nombre: str, texto: str) -> str:
    """Aplica el tope de caracteres que le corresponda a esa herramienta."""
    tope = MAX_TOOL_RESULT_CHARS_POR_TOOL.get(nombre, MAX_TOOL_RESULT_CHARS)
    if len(texto) <= tope:
        return texto
    return texto[:tope] + '... [recortado]"}'

# Ventana del resumen semanal, en días hacia atrás desde hoy — lo que cuenta
# como "esta semana" para agenda de sesiones y el corte estricto del informe.
RESUMEN_DIAS = 7

# Ventana más amplia, SOLO para proyectos de ley: un proyecto presentado hace
# 10-13 días (fuera de la semana estricta) casi siempre sigue en su mismo
# estado inicial ("PRESENTADO", sin resolver) — mostrarlo como contexto
# vigente es de bajo riesgo. No se aplica a interpelaciones/noticias: esas sí
# pueden quedar obsoletas (resueltas, archivadas) en ese mismo lapso sin forma
# de saberlo desde los datos, así que ahí se mantiene el corte estricto de 7
# días (ver REGLA DE FECHA en resumen.md).
RESUMEN_CONTEXTO_DIAS = 15

# Herramientas fijas del resumen semanal — no se le deja la elección al
# router. Dejado a su criterio con tool_choice="required" terminaba llamando
# 7-8 herramientas de golpe (fetch_interpelaciones, fetch_comisiones,
# fetch_comisiones, buscar_agenda...) y reventaba el límite del proveedor,
# tumbando la función entera. Pasó con Groq y su TPM de 6k; con Gemini el techo
# es de requests por día, así que 7-8 llamadas por resumen queman la cuota
# igual de rápido y la lista fija sigue haciendo falta.
#
# fetch_interpelaciones sí se agregó a la lista fija: es la fuente de la
# noticia política más caliente de la semana (mociones contra ministros) y
# sin ella el resumen puede terminar hablando solo de trámite institucional
# mientras ignora lo más relevante — pasó en producción. Las otras 3
# (fetch_comisiones y buscar_agenda) siguen afuera: no aportan
# nada nuevo que buscar_destacados/fetch_agenda_pleno no cubran ya para un
# resumen semanal, así que agregarlas sería puro peso extra sin beneficio.
RESUMEN_TOOLS = (
    ("buscar_proyectos",       {"dias": RESUMEN_CONTEXTO_DIAS}),
    ("buscar_destacados",      {}),
    ("fetch_agenda_pleno",     {}),
    ("fetch_agenda_camaras",   {"dias": RESUMEN_DIAS}),
    ("fetch_interpelaciones",  {}),
)

# Marcadores que indican que ya se mostró un expediente completo en el hilo.
EXPEDIENTE_MARKERS = (
    "FICHA DEL PROYECTO",
    "COMISIONES A LAS QUE FUE DERIVADO",
    "ACTOS DE TRABAJO POR COMISIÓN",
    "MI LECTURA",
)

PALABRAS_ANALISIS = (
    "analiza", "analizar", "resume", "resumir", "resumen", "fórmula legal",
    "formula legal", "qué propone", "que propone", "qué modifica", "que modifica",
    "artículo", "articulo", "deroga", "explica",
)

YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|live/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)

# "proyectos de ley de los últimos N días" / "novedades de proyectos de
# ley..." — override determinístico del router. Probado en vivo, dos veces:
# el router (8B) es no determinístico para pedidos de "proyectos de ley
# recientes" — a veces sí llama a buscar_proyectos, a veces no llama a NADA
# y el modelo grande termina inventando una tabla completa de proyectos
# falsos (números, títulos, fechas, todo). La regla anti-invención del
# prompt ayuda pero no es 100% confiable — pasó en dos rondas de prueba
# distintas, con dos frases distintas ("...últimos 15 días..." y
# "...novedades de proyectos de ley en DIPUTADOS..."). Por eso la detección
# cubre ambos casos: un número explícito de días, o una palabra que implica
# "reciente" sin número — en ese caso se usa una ventana por defecto.
# No intenta preservar materia/autor del pedido original: se prioriza no
# inventar por sobre la precisión del filtro (SPLEY igual no filtra bien por
# materia — ver comentario en _fetch_spley_por_materia en scraper.py).
DEFAULT_DIAS_PROYECTOS = 15
DIAS_PROYECTOS_RE = re.compile(r"(\d+)\s*d[ií]as", re.IGNORECASE)
RECIENTE_RE = re.compile(r"\b(novedades|reciente|recientes|últim[oa]s?)\b", re.IGNORECASE)


# ── Atajo conversacional: saltear el router ────────────────────────────────
#
# prompts/router.md dice "usa SIEMPRE responder_directo" para saludos, cortesía
# y preguntas sobre el propio asistente. Medido contra Gemini: preguntarle eso
# al router cuesta un viaje de red completo que termina, sin excepción, en
# responder_directo — el 70% de la latencia de un "hola" se iba ahí. Cuando la
# respuesta del router es previsible con certeza, se la saltea.
#
# El criterio es deliberadamente estrecho: la regex ancla el mensaje ENTERO
# (^...$), así que "gracias, ahora dame los proyectos de ley" no matchea y sigue
# yendo al router. Ante cualquier duda se paga el router — equivocarse para el
# otro lado significa que el modelo grande responde sin datos, y este código ya
# tiene historial de inventar tablas de proyectos falsos cuando le falta la
# herramienta (ver _detecta_proyectos_por_dias).
#
# Los afirmativos sueltos ("sí", "dale", "ok") quedan AFUERA a propósito: si el
# turno anterior fue "¿querés que busque los proyectos?", ese "sí" sí necesita
# herramienta y el router es quien tiene el contexto para verlo.
CONVERSACIONAL_RE = re.compile(
    r"^[\s¡¿]*(?:"
    r"hola|holis?|buenas|buen[oa]s?\s+(?:d[ií]as?|tardes|noches)|"
    r"qu[eé]\s+tal|c[oó]mo\s+(?:est[aá]s|va|andas|and[aá]s)|"
    r"gracias|muchas\s+gracias|mil\s+gracias|te\s+agradezco|"
    r"chau|adi[oó]s|hasta\s+luego|nos\s+vemos|"
    r"ja(?:ja)+|je(?:je)+|"
    r"qui[eé]n\s+(?:sos|eres)|c[oó]mo\s+te\s+llam[aá]s|"
    r"qu[eé]\s+(?:pod[eé]s|puedes|sab[eé]s|sabes)\s+hacer|"
    r"para\s+qu[eé]\s+(?:serv[ií]s|sirves)|"
    r"en\s+qu[eé]\s+(?:me\s+)?(?:pod[eé]s|puedes)\s+ayudar"
    r")[\s!¡?¿.,…\-]*$",
    re.IGNORECASE,
)

# Red de seguridad: si aparece cualquier término del dominio, va al router
# aunque la frase haya matcheado arriba.
DOMINIO_RE = re.compile(
    r"\b(proyecto|ley|leyes|comisi[oó]n|comisiones|congreso|senado|diputad|"
    r"sesi[oó]n|sesiones|pleno|dictamen|dictamenes|dict[aá]menes|citaci[oó]n|"
    r"moci[oó]n|votaci[oó]n|expediente|congresista|ministr|interpelaci[oó]n|"
    r"decreto|norma|agenda|pdf|documento)",
    re.IGNORECASE,
)


def _es_conversacional(texto: str) -> bool:
    """True solo si el mensaje entero es saludo, cortesía o meta-pregunta."""
    t = (texto or "").strip()
    if not t or len(t) > 80:
        return False
    if DOMINIO_RE.search(t):
        return False
    return bool(CONVERSACIONAL_RE.match(t))


def _detecta_proyectos_por_dias(texto: str) -> int | None:
    t = texto.lower()
    if "proyecto" not in t or "ley" not in t:
        return None
    m = DIAS_PROYECTOS_RE.search(t)
    if m:
        return int(m.group(1))
    if RECIENTE_RE.search(t):
        return DEFAULT_DIAS_PROYECTOS
    return None


# ── Corrección de nombres de congresistas en la respuesta final ────────────
#
# scraper.py normaliza los nombres de firmantes/autores a "Nombre Apellido"
# (ver _nombre_a_formato_natural y _normalizar_lista_autores) porque SPLEY
# los devuelve mezclados: unas veces "Apellido, Nombre", otras ya en orden
# natural, según qué endpoint respondió. Verificado en vivo (04/08/2026,
# probado con 3 variantes de instrucción distintas, incluyendo un ejemplo
# negativo explícito) que el modelo principal reescribe el nombre normalizado
# de vuelta a "Apellido, Nombre" en su respuesta pese a que el dato ya le
# llega limpio — es un estilo tan dominante en los documentos del Congreso
# que ninguna instrucción de prompt lo revirtió. Se corrige acá con un swap
# determinístico: solo se tocan los nombres que efectivamente trajeron las
# herramientas en este turno, nunca un patrón genérico de "Apellido, Nombre"
# que podría coincidir con texto no relacionado a personas.
_CAMPOS_NOMBRE = {"autor", "autor_principal", "coautores", "autores"}


def _extraer_nombres_normalizados(tool_msgs: list[dict]) -> set[str]:
    nombres: set[str] = set()

    def _recolectar(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in _CAMPOS_NOMBRE and isinstance(v, str) and v:
                    for parte in v.split(";"):
                        parte = parte.strip()
                        if parte and "," not in parte and " " in parte:
                            nombres.add(parte)
                else:
                    _recolectar(v)
        elif isinstance(obj, list):
            for item in obj:
                _recolectar(item)

    for msg in tool_msgs:
        contenido = msg.get("content", "")
        idx = contenido.find("{")
        if idx == -1:
            continue
        try:
            _recolectar(json.loads(contenido[idx:]))
        except (json.JSONDecodeError, ValueError):
            continue
    return nombres


def _fix_nombres_reformateados(texto: str, nombres_normalizados: set[str]) -> str:
    for nombre in nombres_normalizados:
        partes = nombre.split()
        if len(partes) < 2:
            continue
        for corte in range(1, len(partes)):
            variante = f"{' '.join(partes[corte:])}, {' '.join(partes[:corte])}"
            if variante in texto:
                texto = texto.replace(variante, nombre)
    return texto


class ChatOrchestrator:
    """Ejecuta una vuelta completa de conversación sobre el historial dado."""

    def __init__(self, messages: list, client=None):
        self.messages = messages or []
        self.client = client or llm.get_client()
        ahora = datetime.now()
        self.hoy = ahora.strftime("%d/%m/%Y")
        # Ventana del resumen semanal: los 7 días que terminan hoy.
        self.desde = (ahora - timedelta(days=RESUMEN_DIAS)).strftime("%d/%m/%Y")
        self.system_base = system_con_fecha(self.hoy)

        # Estado que rellena _analyze()
        self.last_msg = ""
        self.is_resumen = False
        self.forzar_dias_proyectos: int | None = None
        self.sector = None
        self.analizar_documento = False
        self.has_sesion = False
        self.has_expediente_en_contexto = False
        self.doc_en_contexto = False
        self.es_conversacional = False
        self.conversation: list = []

        # Estado que rellenan las fases 2 y 3
        self.tool_msgs: list = []
        self.tools_usados: list = []
        self.solo_responder_directo = False

    # ── Análisis del pedido ──────────────────────────────────────────────────

    def _analyze(self):
        """Clasifica el pedido para decidir el camino y armar el historial."""
        msgs = self.messages
        self.last_msg = msgs[-1].get("content", "") if msgs else ""
        low_last = self.last_msg.lower()

        # Resumen semanal: el frontend lo marca con un centinela.
        self.is_resumen = self.last_msg.strip().startswith("__RESUMEN_SEMANAL__")
        if self.is_resumen and ":" in self.last_msg:
            self.sector = self.last_msg.strip().split(":", 1)[1].strip()

        # "proyectos de ley de los últimos N días" — no is_resumen (que ya
        # tiene su propio set fijo de herramientas).
        if not self.is_resumen:
            self.forzar_dias_proyectos = _detecta_proyectos_por_dias(self.last_msg)

        # ¿Hay un PDF cargado? El frontend lo inyecta como mensaje de usuario.
        recientes = msgs[-6:]
        self.doc_en_contexto = any(
            "He cargado el documento" in (m.get("content", "") or "")
            for m in recientes if m.get("role") == "user"
        )
        pide_analisis = any(w in low_last for w in PALABRAS_ANALISIS)
        # Analizar un doc ya cargado se resuelve con el texto en contexto:
        # no hace falta ninguna herramienta de scraping.
        self.analizar_documento = self.doc_en_contexto and (
            pide_analisis or len(self.last_msg) < 60
        )

        self.has_sesion = (
            "youtube.com" in low_last or "youtu.be" in low_last
            or "transcript" in low_last or "[sesión" in low_last
        )

        # Saludo/cortesía puro: se saltea el router (ver CONVERSACIONAL_RE).
        # No aplica al resumen ni al override de proyectos, que ya tienen sus
        # propias herramientas fijas y ni pasan por Fase 1.
        self.es_conversacional = (
            not self.is_resumen
            and self.forzar_dias_proyectos is None
            and _es_conversacional(self.last_msg)
        )

        recent_assistant = " ".join(
            m.get("content", "") for m in msgs[-6:] if m.get("role") == "assistant"
        )
        self.has_expediente_en_contexto = any(
            marker in recent_assistant for marker in EXPEDIENTE_MARKERS
        )

        if self.is_resumen:
            base = (
                "Genera el resumen ejecutivo semanal completo del Congreso del Perú "
                f"cubriendo únicamente del {self.desde} al {self.hoy} "
                f"(últimos {RESUMEN_DIAS} días)."
            )
            if self.sector and self.sector != "general":
                base += (
                    f" Enfoca el análisis especialmente en el sector {self.sector} y los "
                    "proyectos de ley, noticias y agenda que impacten a ese sector."
                )
            self.conversation = [{"role": "user", "content": base}]
        else:
            self.conversation = msgs[-20:]

    @property
    def _short_circuit(self) -> bool:
        """Analizar doc o sesión no requiere Fase 1 ni 2."""
        return self.analizar_documento or (self.has_sesion and not self.is_resumen)

    # ── Atajo: documento o sesión en contexto ────────────────────────────────

    async def _run_short_circuit(self):
        system_p3 = self.system_base
        if self.analizar_documento:
            system_p3 += "\n" + WORKFLOW_PDF_FORMULA
        if self.has_sesion:
            system_p3 += "\n" + WORKFLOW_SESION
            ok = True
            async for ev in self._inject_transcript():
                if ev is None:          # centinela: sin transcript, ya se avisó
                    ok = False
                    break
                yield ev
            if not ok:
                return

        msgs = [{"role": "system", "content": system_p3}] + self.conversation
        async for ev in self._stream_final(msgs, max_tokens=2048):
            yield ev

    async def _inject_transcript(self):
        """
        Busca el transcript del video de YouTube del último mensaje y lo mete
        en la conversación. Emite None si no hay transcript disponible.
        """
        m = YT_ID_RE.search(self.last_msg)
        if not m:
            return

        yield sse.status("Obteniendo transcript de la sesión...")
        try:
            data = await fetch_transcript_youtube(m.group(1))
        except Exception as e:
            logger.warning("fetch_transcript_youtube falló: %s", e)
            data = None

        if data and data.get("ok") and data.get("text"):
            transcript_msg = {
                "role": "user",
                "content": (
                    f"[TRANSCRIPT DE LA SESIÓN — fuente: {data.get('source', 'youtube')}]\n"
                    f"{data['text']}\n"
                    "[FIN DEL TRANSCRIPT]"
                ),
            }
            self.conversation = (
                self.conversation[:-1] + [transcript_msg, self.conversation[-1]]
            )
            return

        # Sin subtítulos: responder sin gastar una llamada al modelo.
        yield sse.text(
            "No pude obtener el transcript de ese video (sin subtítulos disponibles "
            "o video privado). Para analizar la sesión podés: (1) cargar el PDF del "
            "acta con el botón de adjunto, o (2) pegar el texto del transcript "
            "directamente en el chat."
        )
        yield sse.DONE
        yield None

    # ── Fase 1: elección de herramienta ──────────────────────────────────────

    def _router_messages(self) -> list:
        """
        Historial que se manda al router. Se recorta agresivamente: un PDF o un
        expediente en contexto son miles de tokens que no aportan nada a la
        elección de herramienta y revientan el TPM ya en la Fase 1.
        """
        # is_resumen ni pasa por acá — run() lo desvía a _phase2_resumen()
        # directo, sin Fase 1 (ver RESUMEN_TOOLS).
        if self.doc_en_contexto:
            return [{"role": "system", "content": ROUTER_PROMPT},
                    {"role": "user", "content": self.last_msg}]

        if self.has_expediente_en_contexto:
            ctx = (
                "[CONTEXTO: El asistente ya mostró el expediente completo de un proyecto "
                "en esta conversación (FICHA DEL PROYECTO, SEGUIMIENTO, COMISIONES, etc.). "
                "Si el usuario pregunta sobre ese expediente — comisiones, actos, predictamen, "
                "adjuntos, estado, autores — usa responder_directo. "
                "Solo llama fetch_expediente si pide OTRO proyecto diferente.]\n\n"
                f"Pregunta: {self.last_msg}"
            )
            return [{"role": "system", "content": ROUTER_PROMPT},
                    {"role": "user", "content": ctx}]

        return [{"role": "system", "content": ROUTER_PROMPT}] + self.messages[-4:]

    async def _phase1(self):
        """Devuelve el choice del router, o None si ya se emitió una respuesta."""
        try:
            resp = self.client.chat.completions.create(
                model=ROUTER_MODEL,
                messages=self._router_messages(),
                tools=TOOLS,
                tool_choice="required",
                max_tokens=512,
                temperature=0.2,
                stream=False,
            )
            return resp.choices[0]
        except Exception as e:
            logger.warning("Fase 1 (router) falló: %s", e)
            raise

    async def _phase1_fallback(self, exc):
        """
        El router falló. Si fue por un tool_call malformado, respondemos igual
        con el modelo grande y sin herramientas; si no, mostramos el error.
        """
        if not llm.is_tool_format_error(exc):
            yield sse.error(llm.friendly_error(exc))
            return

        msgs = [{"role": "system", "content": self.system_base}] + self.conversation
        try:
            async for delta in llm.stream_deltas(
                self.client, msgs, model=MAIN_MODEL, max_tokens=2048
            ):
                yield sse.text(delta)
            yield sse.DONE
        except Exception as e2:
            logger.error("Fallback de Fase 1 falló: %s", e2)
            yield sse.error(llm.friendly_error(e2))

    # ── Fase 2: ejecución de herramientas ────────────────────────────────────

    @staticmethod
    def _clean_args(raw: str) -> dict:
        try:
            args = json.loads(raw or "{}")
        except (ValueError, TypeError):
            return {}
        if not isinstance(args, dict):
            return {}
        if "limit" in args:
            try:
                args["limit"] = int(args["limit"])
            except (ValueError, TypeError):
                args["limit"] = 20
        return {k: v for k, v in args.items() if v != ""}

    # Herramientas donde repetir la llamada en el mismo turno nunca agrega
    # cobertura real — con el parámetro por defecto (sin camara) ya traen
    # todo. El router a veces llama esto una vez por cada cámara que el
    # usuario nombra (ej. "Senado y Diputados"), pensando que hace falta una
    # llamada por cámara — confirmado en vivo con fetch_comisiones, dos veces
    # seguidas en pruebas distintas, incluso después de aclarar la descripción
    # de la herramienta (el fix de prompt solo no alcanzó).
    TOOLS_UNA_SOLA_VEZ = {"fetch_comisiones"}

    @classmethod
    def _deduplicar_tool_calls(cls, real_calls: list) -> list:
        """
        Si una herramienta de TOOLS_UNA_SOLA_VEZ aparece más de una vez, se
        queda solo con la primera — las demás no aportan nada. La llamada que
        sobrevive se fuerza a args vacíos: si el router la mandó con
        camara='senado', quedarse con esos args tal cual perdería los enlaces
        de Diputados (fetch_comisiones con una cámara puntual NO incluye la
        otra — solo camara=None trae ambas). Sin argumentos garantiza la
        cobertura completa sin importar qué haya mandado el router.
        """
        vistas = set()
        resultado = []
        for tc in real_calls:
            name = tc.function.name
            if name in cls.TOOLS_UNA_SOLA_VEZ:
                if name in vistas:
                    continue
                vistas.add(name)
                tc = SimpleNamespace(id=tc.id, function=SimpleNamespace(name=name, arguments="{}"))
            resultado.append(tc)
        return resultado

    async def _phase2(self, choice):
        """Ejecuta las tool calls del router, emitiendo el estado de cada una."""
        if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
            return

        # responder_directo no es una herramienta real: es la señal de
        # "contestá con tu conocimiento, sin datos externos".
        real_calls = [tc for tc in choice.message.tool_calls
                      if tc.function.name != "responder_directo"]
        real_calls = self._deduplicar_tool_calls(real_calls)
        if not real_calls:
            self.solo_responder_directo = True
            return

        # OJO: acá antes se armaba el intercambio nativo de function-calling de
        # OpenAI (mensaje "assistant" con tool_calls + mensaje "tool" por cada
        # resultado). Eso rompe con Gemini: exige un campo interno
        # ("thought_signature") atado a la respuesta original del modelo que
        # nosotros no tenemos cómo reconstruir a mano. Fase 3 nunca manda
        # `tools=` en su propio pedido (no es una llamada nativa real, ver
        # _stream_final) así que no hace falta ese formato — un mensaje de
        # texto plano con el resultado le sirve igual al modelo y funciona
        # con cualquier proveedor.
        for tc in real_calls:
            name = tc.function.name
            self.tools_usados.append(name)
            args = self._clean_args(tc.function.arguments)

            yield sse.status(STATUS_LABELS.get(name, "Consultando el Congreso..."))

            result = await self._run_tool(name, args)

            result_str = _recortar_resultado(name, json.dumps(result, ensure_ascii=False))

            self.tool_msgs.append({
                "role": "user",
                "content": f"[Resultado de la herramienta {name}]\n{result_str}",
            })

    async def _phase2_tools_fijas(self, tools: tuple):
        """
        Corre una lista fija de herramientas directamente, sin pasar por el
        router — mismo formato de tool_calls que arma _phase2, pero con ids
        sintéticos en vez de los que devolvería el modelo. Usado cuando ya
        sabemos con certeza qué herramienta hace falta y no vale la pena
        (o es riesgoso) dejárselo a la elección del router de 8B — ver
        RESUMEN_TOOLS y _detecta_proyectos_por_dias.
        """
        # Mismo motivo que en _phase2: texto plano en vez del formato nativo
        # de tool_calls, para no depender de metadata específica de un
        # proveedor (ver comentario ahí).
        for name, args in tools:
            self.tools_usados.append(name)
            yield sse.status(STATUS_LABELS.get(name, "Consultando el Congreso..."))

            result = await self._run_tool(name, args)

            result_str = _recortar_resultado(name, json.dumps(result, ensure_ascii=False))

            self.tool_msgs.append({
                "role": "user",
                "content": f"[Resultado de la herramienta {name}]\n{result_str}",
            })

    @staticmethod
    async def _run_tool(name: str, args: dict) -> dict:
        """
        Ejecuta una herramienta y normaliza cualquier fallo a 'sin_datos'.

        Un reintento corto ante excepción: los servidores del Congreso tienen
        hipos transitorios (timeout puntual, conexión caída) que no vuelven a
        pasar un segundo después — confirmado en vivo con buscar_proyectos,
        que falló una vez y funcionó perfecto al tiro siguiente contra la
        misma consulta. Sin esto, un hipo de 1 request tumbaba toda la
        respuesta con un "no puedo buscar ahora mismo".
        """
        if name not in TOOL_MAP:
            logger.warning("El modelo pidió una herramienta inexistente: %s", name)
            return {"sin_datos": True, "mensaje": f"Herramienta '{name}' no disponible."}

        last_exc = None
        for intento in range(2):
            try:
                result = await TOOL_MAP[name](args)
                break
            except Exception as e:
                last_exc = e
                if intento == 0:
                    logger.warning("Herramienta %s falló (intento 1/2), reintentando: %s", name, e)
                    await asyncio.sleep(1.5)
        else:
            logger.error("Herramienta %s falló con args=%s tras reintentar: %s", name, args, last_exc)
            return {"sin_datos": True,
                    "mensaje": f"Error al consultar {name}: {str(last_exc)[:100]}"}

        if isinstance(result, dict) and "error" in result:
            return {"sin_datos": True,
                    "mensaje": result.get("error", "No hay información disponible.")}
        return result

    # ── Fase 3: respuesta final ──────────────────────────────────────────────

    def _phase3_system(self) -> str:
        """Arma el system prompt: base compacta + solo los flujos que aplican."""
        if self.is_resumen:
            return resumen_con_fechas(self.hoy, self.desde)
        if self.solo_responder_directo or not self.tool_msgs:
            return self.system_base

        # Con tool results conviene SYSTEM_MINI (~800 tokens menos), salvo que
        # la herramienta tenga un workflow de formato propio que sí hace falta.
        needs_workflow = any(t in WORKFLOWS for t in self.tools_usados)
        if not needs_workflow:
            return SYSTEM_MINI.format(hoy=self.hoy)

        system_p3 = self.system_base
        for t in self.tools_usados:
            if t in WORKFLOWS:
                system_p3 += "\n" + WORKFLOWS[t]
        return system_p3

    def _phase3_max_tokens(self) -> int:
        if self.is_resumen:
            # Reporte multi-tema con links — necesita más espacio que una
            # respuesta de una sola herramienta. 5 tool results (RESUMEN_TOOLS),
            # no 7-8: un poco más de margen que antes por la sección de
            # interpelaciones que se sumó.
            return 3500
        if "fetch_expediente" in self.tools_usados:
            return 4000
        if self.tools_usados:
            # Proyectos/agenda: respuesta más corta, menos presión sobre el TPM.
            #
            # Salvo cuando la herramienta devolvió un listado grande. Una fila
            # de la tabla de proposiciones ronda los 70 tokens, así que un
            # "últimos 15 días" (89 proyectos) necesita más de 6000 y con 1800
            # la tabla se cortaba a la cuarta fila, en mitad de un título.
            # El presupuesto sigue el tamaño de lo que vino en vez de ser fijo:
            # así el caso grande entra entero sin inflar cada respuesta corta,
            # que son la mayoría.
            chars = sum(len(m.get("content") or "") for m in self.tool_msgs)
            return 14000 if chars > 12000 else 1800
        return 2500

    async def _stream_final(self, msgs, max_tokens):
        """
        Streaming de Fase 3 con reintento sobre rate limit.

        Si este turno trajo nombres de congresistas de una herramienta, se
        buferea el texto para poder corregir el reformateo del modelo antes
        de mandarlo (ver _fix_nombres_reformateados) — el resto de los turnos
        sigue transmitiendo en vivo, delta por delta, sin buffer.
        """
        nombres = _extraer_nombres_normalizados(self.tool_msgs) if self.tool_msgs else set()
        buffer = ""
        async for kind, payload in llm.stream_with_retry(
            self.client, msgs, model=MAIN_MODEL, max_tokens=max_tokens
        ):
            if kind == "text":
                if nombres:
                    buffer += payload
                else:
                    yield sse.text(payload)
            elif kind == "status":
                yield sse.status(payload)
            elif kind == "error":
                yield sse.error(payload)
                return
        if buffer:
            buffer = _fix_nombres_reformateados(buffer, nombres)
            CHUNK = 60
            for i in range(0, len(buffer), CHUNK):
                yield sse.text(buffer[i:i + CHUNK])
        yield sse.DONE

    # ── Punto de entrada ─────────────────────────────────────────────────────

    async def run(self):
        """Generador de eventos SSE para una vuelta completa de conversación."""
        self._analyze()

        if self._short_circuit:
            async for ev in self._run_short_circuit():
                yield ev
            return

        if self.is_resumen:
            # Herramientas fijas — nada de router acá (ver RESUMEN_TOOLS).
            async for ev in self._phase2_tools_fijas(RESUMEN_TOOLS):
                yield ev
        elif self.forzar_dias_proyectos is not None:
            # "proyectos de ley de los últimos N días" — el router fallaba en
            # elegir buscar_proyectos acá con demasiada frecuencia (ver
            # _detecta_proyectos_por_dias). Forzarlo evita que el modelo
            # grande termine inventando una tabla de proyectos falsos.
            tools = (("buscar_proyectos", {"dias": self.forzar_dias_proyectos}),)
            async for ev in self._phase2_tools_fijas(tools):
                yield ev
        elif self.es_conversacional:
            # Saludo o cortesía: el router siempre contesta responder_directo
            # acá, así que se ahorra ese viaje de red entero (ver
            # CONVERSACIONAL_RE). Fase 3 queda igual que si hubiera pasado por
            # el router: mismo system prompt, mismo historial, sin tool_msgs.
            logger.info("Atajo conversacional: se saltea el router")
            self.solo_responder_directo = True
        else:
            try:
                choice = await self._phase1()
            except Exception as e:
                async for ev in self._phase1_fallback(e):
                    yield ev
                return

            async for ev in self._phase2(choice):
                yield ev

        # Con tool results el contexto ya viene del resultado; mandar 20 mensajes
        # extra dispara el TPM. Con herramientas: solo los últimos 4.
        conv_p3 = self.messages[-4:] if self.tool_msgs else self.conversation
        msgs = (
            [{"role": "system", "content": self._phase3_system()}]
            + conv_p3
            + self.tool_msgs
        )

        async for ev in self._stream_final(msgs, self._phase3_max_tokens()):
            yield ev
