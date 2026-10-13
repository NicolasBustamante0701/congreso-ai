# Diana (Congreso IA)

Asistente de investigación parlamentaria del Perú. App de escritorio
(Electron) con un servidor local (FastAPI, Python).

- **Diana**: chat que responde **solo con datos de las fuentes oficiales**
  del Congreso, el Senado y la Cámara de Diputados (`congreso.gob.pe` y sus
  subdominios: SPLEY, portales de las cámaras, agenda, mociones). No usa
  buscadores ni medios. Distingue "no hay datos" de "la fuente no respondió",
  verifica que los números de proyecto y los links citados estén en lo
  consultado y mantiene las tablas en pantalla, en Word y en PDF.
- **Inicio**: próxima actividad, proyectos ingresados, sesiones transcritas
  y consultas recientes, con la hora de consulta y aviso si una fuente falló.
- **Agenda**: los próximos 14 días por cámara.
- **Sesiones**: transcripción de videos del canal del Congreso (Groq
  Whisper), guardada al vuelo; preguntas con marcas de tiempo `[HH:MM:SS]`
  que llevan al segundo del video, e informe final.
- **Documentos**: PDFs oficiales; Diana los lee citando la página.
- **Seguimiento**: proyectos seguidos por número, con alertas cuando cambia
  el estado, aparece un paso nuevo o un documento nuevo.
- **Búsqueda** (⌘K): SPLEY, lo dicho en sesiones transcritas y la agenda, en
  paralelo.

## Arrancar en macOS

```bash
./start.sh
```

Eso crea `.venv/`, instala las dependencias de Python y de Node si faltan, y
levanta la app. La primera vez tarda unos minutos (descarga Electron, ~90 MB).

Para trabajar solo contra el servidor, sin la ventana de Electron:

```bash
.venv/bin/python server.py     # http://127.0.0.1:8732
```

### Requisitos

- **Python 3.11 o superior.** Probado en 3.14.
- **Node 18+.** Probado en 18.20.8.
- **ffmpeg** — solo para la transcripción de sesiones. No hace falta Homebrew:
  viene en el wheel de `imageio-ffmpeg`, que ya está en `requirements.txt`. Si
  hay un `ffmpeg` en el `PATH`, ese tiene prioridad
  (ver `live_transcriber.ffmpeg_exe`).

### Configuración

En desarrollo, las credenciales van en un `.env` en la raíz (no se commitea; la app empaquetada usa Ajustes):

```
GROQ_API_KEY=...        # transcripción Whisper — siempre Groq, ver config.py
LLM_PROVIDER=gemini     # groq | gemini | cerebras | openai
```

Cada proveedor lee su propia key (`GEMINI_API_KEY`, `CEREBRAS_API_KEY`,
`OPENAI_API_KEY`). Todos hablan por el SDK de `openai`, así que cambiar de
proveedor es cambiar una línea del `.env`.

## Tests y lint

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check .
```

Los tests no salen a la red: VCR reproduce el HTTP real del Congreso desde
`tests/cassettes/`, y el LLM, SPLEY y Whisper se simulan donde hace falta
(cada test lo dice). Para regrabar una cassette o un snapshot, borrá el
archivo y volvé a correr los tests.

Línea base de tiempos del chat (usa las keys del `.env` y la red real):

```bash
.venv/bin/python scripts/medir_chat.py --etiqueta antes --repeticiones 2
```

## Estructura

```
server.py              punto de entrada; vigila al proceso de Electron
config.py              rutas (datos, logs), credenciales, proveedor, hora de Lima
routers/               endpoints HTTP por área
  chat, export, pdfs, expediente, sesiones, live, transcripts,
  investigacion (búsqueda, seguimiento, alertas), pages (inicio, agenda),
  settings (ajustes de IA, diagnóstico)
services/
  orchestrator.py      chat en 3 fases: router → herramientas (en paralelo,
                       con caché y recorte por filas) → respuesta en streaming
  llm.py               proveedores (SDK de openai) con respaldo en cadena
  tools.py, tool_cache.py   herramientas del modelo y su caché por tipo
  transcripts.py       sesiones transcritas: ventanas, cobertura, FTS5
  seguimiento.py       proyectos seguidos y alertas
  busqueda.py          búsqueda unificada
  db.py                SQLite local con esquema versionado
  pdf.py, docx.py, sse.py, observability.py, prompt_registry.py
prompts/               prompts en Markdown
scraper.py             fuentes oficiales; candado de dominio en el cliente HTTP
live_transcriber.py    ffmpeg + Groq Whisper por ventanas de 10 s
static/                frontend vanilla JS: tokens.css (sistema de diseño),
                       app.js/app.css (shell), md.js, sse.js, live/pdfs/expediente
main.js, preload.js    Electron
scripts/medir_chat.py  línea base de tiempos del chat
```

### Datos locales

- `~/Library/Application Support/Diana/diana.db` — transcripciones,
  informes, proyectos seguidos y alertas (en la app: `userData` de Electron).
- `userData/chat-history.json` — historial de consultas.
- `~/Library/Logs/Diana/` — `server.log` (rotativo) y `main.log`.

Todo se borra desde Ajustes → Datos guardados o desde Sesiones. Al actualizar
la app, las migraciones de la base solo agregan tablas: no se pierde nada.

### Qué sale de la computadora

Las consultas a los portales del Congreso; la pregunta y los datos
consultados al proveedor de IA elegido; el audio de las sesiones (tramos de
10 s) a Groq. Diana no es "100 % local".

### Limitaciones conocidas

- Transcripción automática: no es un acta oficial; nombres y cifras pueden
  salir mal reconocidos. No hay diarización (no se identifica quién habla).
- En un stream **en vivo** los tiempos son de captura, no del video.
- El seguimiento y la revisión automática (cada 3 h) solo corren con la app
  abierta.
- La búsqueda es por coincidencia de palabras (título en SPLEY, BM25 en
  transcripciones), no semántica.
- Los portales del Congreso a veces tardan o no responden (medido: la
  Relatoría llegó a no contestar en 60 s); Diana lo informa como error.

## Instalar la app (usuarios)

El DMG sale de la pestaña [Releases](https://github.com/nicobus0701-dot/congreso-ai/releases).

La app **no está firmada** con una cuenta de Apple Developer (son $99/año), así
que macOS le pone el atributo de cuarentena al bajarla y la primera apertura
tiene un paso extra. Después de eso abre con doble clic como cualquier app:

1. Arrastrá **Diana** a Aplicaciones.
2. **Clic derecho sobre la app → Abrir** (el doble clic no alcanza la primera vez).
3. En el aviso, tocá **Abrir**.

En macOS 15 (Sequoia) o superior el clic derecho ya no sirve: abrila una vez,
y después andá a **Ajustes del Sistema → Privacidad y Seguridad**, donde
aparece un botón **Abrir igualmente**.

O directo, sin diálogos:

```bash
xattr -dr com.apple.quarantine "/Applications/Diana.app"
```

No hace falta tener Python, Node ni ffmpeg: el servidor va compilado adentro
del bundle y ffmpeg viaja en el wheel de `imageio-ffmpeg` (ver `server.spec`).

Estas instrucciones también van en las notas de cada release
(`build.releaseInfo.releaseNotes` en `package.json`), así que quien baje el DMG
las ve sin entrar acá.

## Empaquetar el DMG

Lo hace el workflow `build-mac.yml` al pushear un tag `v*`. Corre en un runner
**Intel** a propósito: `macos-latest` es Apple Silicon y produce un DMG arm64
que no arranca en una Mac Intel (Rosetta 2 traduce x86→arm, no al revés). El
DMG x64 corre nativo en Intel y bajo Rosetta 2 en Apple Silicon.

### Credenciales: el DMG sale sin keys

Cada usuario pega su propia API key en **Ajustes de IA**, que se abre solo la
primera vez. La key se guarda cifrada con el llavero de macOS (`safeStorage`,
en `userData/ai-settings.json`), y Electron se la pasa al servidor por
variable de entorno (`main.js`, `aiEnv`). Si el llavero no está disponible, la
key queda solo en memoria hasta cerrar la app: nunca se escribe en texto
plano.

Hasta la versión anterior, el workflow escribía las keys del repo en
`dist/server/.env` y viajaban en texto plano dentro del `.app`. **Las keys que
estuvieron en esos secrets hay que rotarlas**: cualquiera con un DMG viejo
puede extraerlas. Ahora el paso *Verificar que no viajen credenciales* hace
fallar el build si aparece un `.env` en `dist/server`.

Secrets que siguen haciendo falta en Actions: ninguno de proveedores de IA.
`GITHUB_TOKEN` alcanza para publicar el release.

### A mano

```bash
.venv/bin/pip install pyinstaller
.venv/bin/pyinstaller server.spec --noconfirm
npm run build
```
