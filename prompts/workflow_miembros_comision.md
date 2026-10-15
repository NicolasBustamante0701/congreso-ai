## FORMATO — INTEGRANTES DE UNA COMISIÓN

El resultado trae `comisiones`: una entrada por cada comisión encontrada, con
su `camara`, `comision`, `enlace` (página oficial) y `miembros`. Si el usuario
no nombró una cámara y vienen dos (la de Constitución existe en el Senado y en
Diputados), muestra las dos, cada una en su sección: no las mezcles.

Por cada comisión:

### [comision] — [camara]

**Mesa directiva:** Presidente/a, Vicepresidente/a y Secretario/a (campo
`cargo`), con su grupo parlamentario.

| Apellidos y nombres | Grupo parlamentario | Cargo |
|---|---|---|
| [nombre] | [grupo_parlamentario] | [cargo] |

Todos los `miembros`, en el orden en que vienen (mesa, titulares, suplentes).
Debajo, una línea con el conteo: "N integrantes: X titulares y Y suplentes",
contados de `cargo`, y el link: [Página oficial de la comisión](enlace).

Si viene `comision_permanente` (se pidió la Comisión Permanente), usa el
formato de la nómina: una tabla | Apellidos y nombres | Cámara | Grupo
parlamentario |, con el conteo "N integrantes: X senadores y Y diputados" y
el link a la fuente.

Si viene `sin_datos` con `comisiones_disponibles`, dilo en una línea y lista
las comisiones disponibles de cada cámara para que el usuario elija.

Copia los nombres tal cual vienen ("Apellidos, Nombres"). No inventes cargos,
bancadas ni integrantes que no estén en los datos.
