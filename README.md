# Congreso IA

Asistente de IA sobre el Congreso del Perú: chat con herramientas que raspan
SPLEY y los portales de Congreso/Senado/Diputados, resumen semanal, lectura de
PDFs, y transcripción en vivo de sesiones de YouTube.

App de escritorio Electron + servidor FastAPI en Python.

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

Las credenciales van en un `.env` en la raíz (no se commitea):

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

Los tests del scraper no salen a la red: VCR reproduce el HTTP real del
Congreso desde `tests/cassettes/`. Para regrabar una cassette o un snapshot,
borrá el archivo y volvé a correr los tests.

## Estructura

```
server.py       punto de entrada de la API
config.py       rutas, credenciales, selección de proveedor LLM
routers/        endpoints HTTP, uno por área funcional
services/       orquestación del chat (3 fases), LLM, PDFs, Word, SSE
prompts/        prompts del sistema en Markdown
scraper.py      todo el scraping del Congreso (SPLEY, agendas, YouTube)
live_transcriber.py   transcripción en vivo: ffmpeg + Groq Whisper
static/         frontend (vanilla JS, sin framework)
main.js         proceso principal de Electron
```

## Instalar la app (usuarios)

El DMG sale de la pestaña [Releases](https://github.com/nicobus0701-dot/congreso-ai/releases).

La app **no está firmada** con una cuenta de Apple Developer (son $99/año), así
que macOS le pone el atributo de cuarentena al bajarla y la primera apertura
tiene un paso extra. Después de eso abre con doble clic como cualquier app:

1. Arrastrá **Congreso IA** a Aplicaciones.
2. **Clic derecho sobre la app → Abrir** (el doble clic no alcanza la primera vez).
3. En el aviso, tocá **Abrir**.

En macOS 15 (Sequoia) o superior el clic derecho ya no sirve: abrila una vez,
y después andá a **Ajustes del Sistema → Privacidad y Seguridad**, donde
aparece un botón **Abrir igualmente**.

O directo, sin diálogos:

```bash
xattr -dr com.apple.quarantine "/Applications/Congreso IA.app"
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

### Las credenciales van dentro del bundle

`config.py` lee las keys de un `.env`, y `.env` está en `.gitignore` — así que
el DMG salía sin credenciales: la app abría bien pero `LLM_API_KEY` quedaba en
`""` y el chat cortaba con *"Falta la API key para el proveedor activo"*.

El paso **Inyectar credenciales en el bundle** del workflow escribe ese `.env`
desde los secrets del repo, en `dist/server/`, que `extraResources` copia a
`Resources/server/` — exactamente donde `config.ENV_FILE` lo busca cuando corre
congelado. El workflow aborta si falta la key del proveedor activo.

Secrets a configurar en **Settings → Secrets and variables → Actions**:

| Nombre | Tipo | Para qué |
|---|---|---|
| `GEMINI_API_KEY` / `GROQ_API_KEY` / `CEREBRAS_API_KEY` / `OPENAI_API_KEY` | secret | la del proveedor activo es obligatoria |
| `GROQ_API_KEY` | secret | además, siempre, para la transcripción |
| `LLM_PROVIDER` | variable | opcional, default `gemini` |

> Las keys viajan en texto plano dentro del `.app`: cualquiera con el DMG puede
> sacarlas y gastar la cuota. Sirve para repartir entre gente de confianza, no
> para publicar. La alternativa es una pantalla de ajustes donde cada usuario
> pegue la suya.

### A mano

```bash
.venv/bin/pip install pyinstaller
.venv/bin/pyinstaller server.spec --noconfirm
cp .env dist/server/.env      # sin esto el .app abre sin credenciales
npm run build
```
