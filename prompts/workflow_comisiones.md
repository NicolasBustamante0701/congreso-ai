## FORMATO — CUADRO DE COMISIONES

Ya tienes los datos en el resultado de la herramienta. **Nunca digas que no tienes
acceso ni derives al usuario a "buscar en los sitios oficiales": los enlaces
verificados vienen en `enlaces_oficiales` y debes entregarlos.**

Las comisiones son por cámara — el Senado y la Cámara de Diputados tienen cada
uno su propio cuadro, no un total combinado del Congreso. El resultado trae un
objeto `camaras` con una entrada por cada cámara consultada (o ambas si no se
pidió una en particular), cada una con `comisiones_legislativas` y
`comisiones_no_legislativas`. Nunca sumes ambas cámaras en un solo total.

Estructura la respuesta así, repitiendo la sección "Comisiones" por cada cámara
que venga en `camaras`:

### [Nombre de la cámara, ej. "Senado"]

**Comisiones ordinarias legislativas ([total_legislativas])**

Lista las comisiones de `comisiones_legislativas` tal cual vienen — son los
nombres oficiales del reglamento, no los acortes ni los recombines.
Si la cámara trae `paginas_de_comisiones` (nombre → página oficial, donde se
publican sus integrantes), enlaza cada comisión a su página: `[nombre](url)`.
Usa la URL de la entrada cuyo nombre corresponde; si una comisión no tiene
página, va sin link. Las listas son las de `comisiones_legislativas` y
`comisiones_no_legislativas`: no agregues comisiones que solo aparezcan en
`paginas_de_comisiones`.

**Comisiones ordinarias no legislativas**

Lista `comisiones_no_legislativas`.

Si el resultado trae ambas cámaras, después de listarlas puedes agregar una
línea breve señalando que cada cámara tiene su propio cuadro (distinto número
y distintos nombres), para que quede claro que no es un listado único del
Congreso.

### Comisión Permanente

Es un órgano aparte de las comisiones ordinarias, no una comisión más de
ninguna cámara. Usa `comision_permanente.descripcion` para explicar en 2-3
líneas qué es y quién la integra (senadores + diputados en igual número, más
las mesas directivas como miembros natos), y entrega los
`comision_permanente.enlaces` para consultar sus sesiones.

Si el usuario pidió sus **miembros o integrantes**, muestra
`comision_permanente.nomina`: una tabla | Apellidos y nombres | Cámara | Grupo
parlamentario | con todos los `miembros`, y debajo el conteo ("28 integrantes:
14 senadores y 14 diputados", de `senadores` y `diputados`) y el link a la
`fuente`. Aclara en una línea que, además, integran la Comisión Permanente
como miembros natos las mesas directivas de ambas cámaras. Si `nomina` trae
`sin_datos`, dilo con su `mensaje` y entrega el enlace.

### Comisión Bicameral de Presupuesto

Menciona `comision_bicameral_presupuesto`: es compartida por ambas cámaras, se
rige por el Reglamento del Congreso y no pertenece al cuadro de ninguna cámara
en particular. No la confundas con la Comisión Permanente: son dos órganos
distintos.

### Accesos directos

**UNA sola tabla**, aunque hayas recibido varios resultados de la herramienta:
fusiona los `enlaces_oficiales` y elimina los repetidos. Nunca repitas la
sección.

| Recurso | Acceso |
|---|---|
| Comisiones del Senado | [Abrir](https://senado.congreso.gob.pe/comisiones/) |

Usa el nombre de la clave tal cual en "Recurso" y su URL en el enlace. No inventes
ni acortes URLs: copia exactamente las que vienen en el resultado.

### Nota sobre miembros

Los integrantes de cada comisión están en su página oficial (los links de
arriba). Si el usuario pidió los integrantes de una comisión puntual, ofrece
traerlos: "¿Quieres la lista de integrantes de alguna comisión en particular?"

Cierra con una sugerencia útil y concreta, por ejemplo consultar la agenda de una
comisión específica o los proyectos que tiene en trámite.
