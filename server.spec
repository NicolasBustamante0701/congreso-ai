# -*- mode: python ; coding: utf-8 -*-
import os

# ffmpeg para la transcripción. En el .app empaquetado no hay PATH del usuario
# ni Homebrew, así que si no viaja adentro la sesión en vivo no funciona en
# ninguna Mac que no tenga ffmpeg instalado a mano. imageio-ffmpeg trae el
# binario dentro del wheel; lo copiamos al bundle y live_transcriber.ffmpeg_exe
# lo encuentra por la ruta del paquete.
try:
    import imageio_ffmpeg

    _ff_dir = os.path.join(os.path.dirname(imageio_ffmpeg.__file__), "binaries")
    _ff_datas = [(_ff_dir, os.path.join("imageio_ffmpeg", "binaries"))]
except Exception:
    _ff_datas = []

a = Analysis(
    ['server.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('static', 'static'),
        # Los prompts se leen en runtime desde PROMPTS_DIR — sin esto el
        # ejecutable empaquetado falla al importar services.prompt_registry.
        ('prompts', 'prompts'),
        ('scraper.py', '.'),
        ('live_transcriber.py', '.'),
    ] + _ff_datas,
    hiddenimports=[
        'config',
        'routers',
        'routers.chat',
        'routers.expediente',
        'routers.export',
        'routers.live',
        'routers.pages',
        'routers.pdfs',
        'routers.sesiones',
        'routers.settings',
        'services',
        'services.docx',
        'services.llm',
        'services.observability',
        'services.orchestrator',
        'services.pdf',
        'services.prompt_registry',
        'services.sse',
        'services.tools',
        'openai',
        # ffmpeg_exe() lo importa dentro de la función, así que PyInstaller no
        # lo ve por análisis estático.
        'imageio_ffmpeg',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.main',
        'anyio',
        'anyio._backends._asyncio',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='server',
    debug=False,
    strip=False,
    upx=True,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name='server',
)
