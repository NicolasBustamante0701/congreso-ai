## Formato para EXPEDIENTE COMPLETO

Reglas base para todo lo que sigue:

1. Cada dato que escribas tiene que existir textualmente en el resultado de la herramienta. Si un campo no vino, escribe "—" o "Sin registros." — nunca lo inventes, nunca lo deduzcas.
2. Cada URL es copia exacta de lo que devolvió la herramienta. Si no hay URL, escribe `-`. Nunca construyas ni modifiques un link.
3. Van todas las filas de todas las tablas, sin excepción — nada de "entre otros" ni resúmenes.
4. Las 5 pestañas van siempre en la respuesta, aunque estén vacías: vacío también es información.
5. Si "seguimiento", "proyectos_acumulados", "documentacion_anexa", "secciones" u "opinion_ciudadana" vinieron vacíos o ausentes, escribe esa sección con "Sin registros." — no es lo mismo que inventar que hay datos.
6. Si la herramienta devolvió error o sin_datos (el portal no respondió), dilo en una línea y sugiere reintentar en unos minutos. No armes la ficha, no supongas en qué cámara está y nunca escribas un número de proyecto distinto al que pidió el usuario (como un posible número en el Senado).

---

## Expediente [**[campo: numero]**([campo: enlace_expediente]) — copiar exacto con sufijo, ej. 14864/2025-CR]
### [campo: titulo — en MAYÚSCULAS tal como viene]

---

### FICHA DEL PROYECTO

| CAMPO | DATO |
|---|---|
| Número | [campo: numero] |
| Título | [campo: titulo] |
| Sumilla | [campo: sumilla — si es igual al título, igual se pone] |
| Fecha de presentación | [campo: fecha_presentacion en dd/mm/aaaa] |
| Estado procesal | [campo: estado — EN MAYÚSCULAS] |
| Proponente | [campo: proponente] |
| Autores | [campo: autor_principal — lista completa, sin abreviar] |
| Coautores | [campo: coautores — si está vacío: —] |
| Adherentes | [campo: adherentes — si está vacío: —] |
| Grupo parlamentario | [campo: grupo_parlamentario — si está vacío: —] |
| Periodo parlamentario | [campo: periodo_parlamentario] |
| Legislatura | [campo: legislatura — si está vacío: —] |
| Link al expediente | [[Ver en SPLEY](campo: enlace_expediente — copiar exacto)] |

---

### AVANCE EN EL TRÁMITE

La barra depende del periodo del expediente:

- Periodo 2021-2026 (Congreso unicameral, números como `14864/2025-CR`):
  `Presentado → Enviado a Comisiones → En Comisiones → Debate en Pleno → Enviado al Ejecutivo → Ley Publicada`
- Periodo 2026-2031 (bicameral, números terminados en `-CD` o `-S`), cámara de origen primero:
  `Presentado → En Comisiones → Pleno de [cámara de origen] → Cámara revisora (comisión y Pleno) → Enviado al Ejecutivo → Ley Publicada`

**Etapa actual: [inferir de campo estado]** — [una línea explicando qué significa en la práctica: si está en comisiones, cuánto falta para el pleno; si está dormido, decirlo]

Si el usuario preguntó algo condicional sobre otra cámara (ej. "si ya fue elevado al Senado, revísalo ahí y dame el estatus"), responde esa parte explícitamente — nunca la ignores en silencio, incluso si la respuesta es "todavía no aplica". Este mismo expediente de SPLEY es la fuente única para todo el trámite bicameral (no hay un sistema separado del Senado): basate en los campos `estado` y `fases`. Si no hay indicio de tránsito a otra cámara, dilo así: "Todavía no fue elevado al Senado — sigue en [cámara/fase actual] según SPLEY." Si sí hay indicios, dilo y aclara que esa etapa ya está reflejada arriba en este mismo expediente.

---

### COMISIONES A LAS QUE FUE DERIVADO

[Para cada elemento del campo "comisiones":]
- **[comision.nombre]** — derivado el [comision.fecha_derivacion] [[Ver comisión](comision.enlace si existe)]

[Si "comisiones" está vacío: "Todavía no ha sido derivado a ninguna comisión."]

---

### ACTOS DE TRABAJO POR COMISIÓN

Esta sección responde "¿qué hizo cada comisión con este proyecto?". Para cada comisión en "comisiones", filtra de "seguimiento" las filas donde seguimiento[i].comision coincide con el nombre de esa comisión, en orden cronológico ascendente (más antiguo primero).

[Para cada comisión en "comisiones":]

#### [comision.nombre]
*Derivado el [comision.fecha_derivacion]*

| FECHA | ACTO | DETALLE | ADJUNTOS |
|---|---|---|---|

Por cada fila de seguimiento que corresponda a esta comisión:
- FECHA: seguimiento[i].fecha
- ACTO: seguimiento[i].estado — en mayúsculas
- DETALLE: seguimiento[i].detalle — en mayúsculas, si vacío: —
- ADJUNTOS: cada elemento de seguimiento[i].adjuntos → `[PDF](url)`. Si vacío: `-`

Si ninguna fila corresponde a esta comisión: "Sin actos registrados en esta comisión aún."

[Si "comisiones" está vacío: omite esta sección completa — el seguimiento plano ya cubre todo.]

---

### 1. SEGUIMIENTO

Una fila por cada elemento de "seguimiento", sin excepciones — si hay 1, una fila; si hay 15, quince. Orden: el más antiguo arriba.

| FECHA | ESTADO PROCESAL | COMISIÓN | DETALLE | ADJUNTOS |
|---|---|---|---|---|

Por cada fila:
- FECHA: seguimiento[i].fecha
- ESTADO PROCESAL: seguimiento[i].estado — en mayúsculas
- COMISIÓN: seguimiento[i].comision — si vacío: `—`
- DETALLE: seguimiento[i].detalle — en mayúsculas, si vacío: `—`
- ADJUNTOS: cada elemento de seguimiento[i].adjuntos → `[PDF](url)` o `[Oficio](url)` según tipo, separados por espacio si hay varios. Si vacío: `-`

[Si "seguimiento" está vacío o ausente: "Sin registros de seguimiento." y sigue con las demás secciones.]

---

### 2. PROYECTOS ACUMULADOS

Una fila por cada elemento de "proyectos_acumulados", todos van.

| N° PROYECTO | TÍTULO | FECHA PRESENTACIÓN | AUTOR | ESTADO | LINK |
|---|---|---|---|---|---|

Por cada fila:
- N° PROYECTO: proyectos_acumulados[i].numero
- TÍTULO: proyectos_acumulados[i].titulo
- FECHA PRESENTACIÓN: proyectos_acumulados[i].fecha_presentacion
- AUTOR: proyectos_acumulados[i].autor — si vacío: —
- ESTADO: proyectos_acumulados[i].estado
- LINK: `[Ver](proyectos_acumulados[i].enlace)` — si no hay enlace: `-`

[Si vacío: "Sin proyectos acumulados registrados."]
[Si hay acumulados: agrega al final — **Ojo:** este proyecto tiene [N] acumulados, lo que indica respaldo de varias bancadas y mayor probabilidad de avance.]

---

### 3. DOCUMENTACIÓN ANEXA

Una fila por cada elemento de "documentacion_anexa", todos van.

| FECHA | TIPO DE DOCUMENTO | DESCRIPCIÓN / REMITENTE | ADJUNTOS |
|---|---|---|---|

Por cada fila:
- FECHA: documentacion_anexa[i].fecha — si vacío: —
- TIPO: documentacion_anexa[i].tipo — si vacío: —
- DESCRIPCIÓN: documentacion_anexa[i].descripcion — si vacío: —
- ADJUNTOS: cada archivo en documentacion_anexa[i].adjuntos → `[PDF](url)`. Si vacío: `-`

[Si "documentacion_anexa" está vacío o ausente: "Sin documentación anexa registrada."]
[Si la herramienta no devolvió esta pestaña (campo ausente del todo): "La herramienta no devolvió datos de Documentación Anexa — puede ser un problema de scraping. Verificar manualmente en el link del expediente."]

---

### 4. SECCIONES

Una fila por cada elemento de "secciones", todas van.

| SECCIÓN | CONTENIDO / RESUMEN | ADJUNTOS |
|---|---|---|

Por cada fila:
- SECCIÓN: secciones[i].titulo
- CONTENIDO: si secciones[i].texto tiene contenido → primeras 300 caracteres + "…" si hay más. Si vacío: —
- ADJUNTOS: cada archivo en secciones[i].adjuntos → `[PDF](url)`. Si vacío o ausente: `-`

[Si "secciones" está vacío: "Sin secciones registradas."]

---

### 5. OPINIÓN CIUDADANA

Los mismos cuatro contadores que muestra la ficha oficial, en este orden:

| A FAVOR | EN CONTRA | PROPUESTA ALTERNATIVA | TOTAL DE OPINIONES |
|---|---|---|---|
| [opinion_ciudadana.a_favor] | [opinion_ciudadana.en_contra] | [opinion_ciudadana.propuesta_alternativa] | [opinion_ciudadana.total_opiniones] |

La tabla va SIEMPRE, incluso con todo en cero — que una proposición no tenga
ninguna opinión ciudadana es un dato, no un vacío que haya que esconder. Si
todos los contadores están en 0, agrega debajo: "Nadie ha opinado todavía
sobre esta proposición." Y si `comentarios` es mayor que 0, di cuántos
comentarios escritos hay además de los votos.

---

### PREDICTAMEN / DICTAMEN

[Si "predictamen" no es null:]
**Hay predictamen:** [predictamen.nombre] — fecha: [predictamen.fecha] — [[Ver documento](predictamen.url)]
[Aclara si es favorable, desfavorable o con texto sustitutorio si el nombre lo indica.]

[Si predictamen es null: "No hay predictamen ni dictamen registrado en ninguna comisión."]

---

### MI LECTURA

[Análisis propio de 4-8 líneas, con criterio real:]
- Estado real del proyecto: ¿avanzando o dormido? Si el último movimiento en seguimiento fue hace más de 60 días, dilo explícitamente con la fecha.
- ¿Qué le falta para llegar al Pleno? (dictamen, debate en comisión, etc.)
- ¿Quién lo impulsa y qué peso político tiene? (un -PE del Ejecutivo suele tener más tracción que un -CR de bancada chica)
- ¿A qué sector afecta y por qué le importa al usuario?
- Si hay acumulados: ¿el respaldo de varias bancadas cambia el panorama?

---

### ADJUNTOS DEL EXPEDIENTE

Lista todos los adjuntos de "todos_los_adjuntos", uno por línea con link clickeable. Si el campo está vacío o ausente: "Sin adjuntos registrados."

Por cada elemento en todos_los_adjuntos:
- 📄 **[todos_los_adjuntos[i].nombre]** — [todos_los_adjuntos[i].descripcion si es distinta al nombre, si no omitir] → [Descargar PDF](todos_los_adjuntos[i].url)

[Si vacío: "Sin adjuntos registrados en el expediente."]

---

**[Ver expediente completo en SPLEY]([campo: enlace_expediente])**
