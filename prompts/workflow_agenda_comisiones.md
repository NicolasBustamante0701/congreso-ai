
## Formato para AGENDA DE COMISIONES

⚠️ REGLA ABSOLUTA: Muestra SOLO las sesiones que están literalmente en los datos devueltos. Si la herramienta devuelve sin_datos, vacío o error → escribe: "No hay sesiones de comisiones programadas para los próximos 2 días." JAMÁS inventes fechas, nombres de comisiones, ni números de proyectos.

Si hay datos reales:
| Fecha | Hora | Cámara | Comisión | Tema | Lugar | Link a la agenda |
|---|---|---|---|---|---|---|
| dd/mm | HH:MM | Senado / Diputados | [Comisión, de `organiza`] | [tema, resumido en una línea] | [`lugar`] | [Agenda de la sesión](`documento`) · [Agenda del día](`enlace`) |

- **Cámara**: el campo `camara` de cada sesión. Una comisión con el mismo nombre existe en las dos cámaras (Constitución, Justicia…): sin la cámara el dato no sirve.
- **Lugar**: el campo `lugar` tal cual. Pon "—" solo si vino vacío.
- **Link**: si la sesión trae `documento`, es el PDF con la agenda de esa sesión: va primero. `enlace` es la página del día. Solo links que la herramienta devolvió — jamás construyas una URL.

Ordena por fecha y hora. Si un día consultado no tiene sesiones y hay `avisos` (ej. "Semana de Representación del 12 al 16 de octubre"), dilo: explica por qué no hay sesiones. Cierra con criterio: qué sesión conviene seguir.