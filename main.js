const { app, BrowserWindow, shell, Menu, globalShortcut, ipcMain, dialog, nativeImage } = require('electron');
const { spawn }  = require('child_process');
const http       = require('http');
const path       = require('path');
const fs         = require('fs');
const os         = require('os');

// ── Nombre de la app ────────────────────────────────────────
// La app se llamaba "Congreso IA" y pasó a llamarse "Diana". userData cuelga
// del nombre, así que renombrar a secas dejaría el historial de chats (vive
// en el localStorage del renderer, bajo userData) en la carpeta vieja y quien
// ya usaba la app abriría Diana sin ninguna de sus conversaciones. Se mueve
// la carpeta una única vez, ANTES de que Electron resuelva la ruta.
const APP_NAME    = 'Diana';
const APP_NAME_ANTERIOR = 'Congreso IA';
try {
  const base   = app.getPath('appData');           // no depende del nombre
  const nueva  = path.join(base, APP_NAME);
  const previa = path.join(base, APP_NAME_ANTERIOR);
  if (!fs.existsSync(nueva) && fs.existsSync(previa)) {
    fs.renameSync(previa, nueva);
    console.log('[diana] userData migrado desde "%s"', APP_NAME_ANTERIOR);
  }
} catch (e) {
  // Si falla, la app arranca igual: solo se pierde el historial anterior.
  console.error('[diana] no se pudo migrar userData:', e.message);
}
app.setName(APP_NAME);
if (process.platform === 'linux') app.setDesktopName('congreso-ai.desktop');
const { autoUpdater } = require('electron-updater');

const PORT = 8732;   // puerto único para no chocar con adam
let   win  = null;
let   py   = null;

// Últimas líneas de stderr del server. Si Python no levanta, la ventana de
// error mostraba solo "Error iniciando el servidor" y el motivo real quedaba
// en una consola que quien abre el .app nunca ve.
const pyErr = [];

// El `python3` del PATH es el del sistema y no tiene las dependencias
// instaladas (en macOS además no se le pueden instalar sin ensuciar el Python
// global). start.sh crea .venv/ en el repo: si está, ese es el intérprete
// correcto; si no, se cae al del PATH como antes.
function devPython() {
  const venv = path.join(__dirname, '.venv', 'bin', process.platform === 'win32' ? 'python.exe' : 'python3');
  return fs.existsSync(venv) ? venv : 'python3';
}

// ── Arranca el servidor FastAPI ─────────────────────────────
function startServer() {
  const isDev = !app.isPackaged;
  const exe     = isDev ? devPython()                                       : path.join(process.resourcesPath, 'server', 'server');
  const args    = isDev ? [path.join(__dirname, 'server.py')]               : [];
  const cwd     = isDev ? __dirname                                          : path.join(process.resourcesPath, 'server');

  py = spawn(exe, args, {
    env: { ...process.env, PORT: String(PORT) },
    cwd,
  });
  py.stdout.on('data', d => process.stdout.write('[py] ' + d));
  py.stderr.on('data', d => {
    process.stderr.write('[py] ' + d);
    pyErr.push(String(d));
    if (pyErr.length > 40) pyErr.shift();
  });
  py.on('exit', code => {
    if (code && code !== 0) console.error('[py] salió con código', code);
  });
}

// ── Espera a que el servidor esté listo ─────────────────────
// El presupuesto era de 40 reintentos = 16s, calibrado contra `python
// server.py` en dev, que levanta en ~2s. El server empaquetado con PyInstaller
// tarda mucho más la primera vez: macOS verifica uno por uno los cientos de
// binarios del bundle antes de dejarlo correr, y encima el grafo de imports es
// enorme (yt_dlp, websockets, urllib3, Crypto). Medido en una Mac Intel: ~38s
// en el primer arranque, bastante menos después. O sea que el .app se rendía
// y mostraba "no se pudo iniciar"
// mientras el server terminaba de arrancar perfectamente 20s después.
//
// 300 reintentos = 120s. Es holgado a propósito: el costo de esperar de más es
// que la pantalla de carga se ve un rato en una máquina lenta; el de esperar
// de menos es que la app parece rota cuando no lo está.
const READY_RETRIES = 300;

// A partir de acá el arranque ya no es "instantáneo" y conviene avisar, para
// que la ventana no parezca colgada.
const SLOW_AFTER_MS = 12000;

function waitReady(retries, cb, startedAt = Date.now()) {
  if (Date.now() - startedAt > SLOW_AFTER_MS) notifySlowStart();

  http.get(`http://localhost:${PORT}/`, () => cb(true))
    .on('error', () => {
      if (retries > 0) setTimeout(() => waitReady(retries - 1, cb, startedAt), 400);
      else             cb(false);
    });
}

// Cambia el subtítulo de la pantalla de carga una sola vez.
let avisoLento = false;
function notifySlowStart() {
  if (avisoLento || !win || win.isDestroyed()) return;
  avisoLento = true;
  win.webContents.executeJavaScript(`
    (() => {
      const m = document.querySelector('.msg');
      if (m) m.textContent = 'La primera vez tarda un poco más...';
    })()
  `).catch(() => {});
}

// ── Pantalla de error ────────────────────────────────────────
// El texto de dev (crear el venv, correr ./start.sh) no le sirve de nada a
// quien abrió el .app desde Aplicaciones: no tiene repo ni terminal a mano.
function errorPage() {
  const detalle = pyErr.join('').trim().slice(-1500);
  const bloque = detalle
    ? `<p class="lbl">Detalle técnico</p><pre>${detalle.replace(/[<&]/g, c => c === '<' ? '&lt;' : '&amp;')}</pre>`
    : '';

  const cuerpo = app.isPackaged
    ? `<p>La app no pudo arrancar su servidor interno. Probá cerrarla y abrirla
         de nuevo; si sigue igual, puede que otro programa esté ocupando el
         puerto ${PORT}.</p>
       <p class="lbl">Si el problema persiste</p>
       <p>Reportalo en
         <a href="https://github.com/nicobus0701-dot/congreso-ai/issues">GitHub</a>
         pegando el detalle de abajo.</p>`
    : `<p>Falta el entorno de Python o alguna dependencia:</p>
       <pre>python3 -m venv .venv &amp;&amp; .venv/bin/pip install -r requirements.txt</pre>
       <p>O corré <code>./start.sh</code>, que lo hace solo.</p>`;

  return `<html><head><meta charset="utf-8"><style>
      body{margin:0;padding:44px;font-family:system-ui;color:#111;background:#fff;
           line-height:1.55;-webkit-user-select:text}
      h2{margin:0 0 14px;font-size:20px}
      p{margin:0 0 12px;max-width:62ch}
      .lbl{font-weight:700;margin-top:22px}
      pre{background:#f4f4f6;border:1px solid #e2e2e8;border-radius:7px;padding:12px;
          font-size:12px;white-space:pre-wrap;word-break:break-word;max-height:240px;
          overflow:auto}
      a{color:#1a5fd0}
    </style></head><body>
      <h2>No se pudo iniciar Diana</h2>
      ${cuerpo}
      ${bloque}
    </body></html>`;
}

// ── Ventana principal ────────────────────────────────────────
function createWindow() {
  win = new BrowserWindow({
    width:  1300,
    height: 840,
    minWidth:  900,
    minHeight: 600,
    title: 'Asistente Congreso Perú',
    icon: path.join(__dirname, 'static', 'app-icon.png'),
    backgroundColor: '#ffffff',
    frame: false,
    webPreferences: {
      nodeIntegration:  false,
      contextIsolation: true,
      partition: 'persist:congreso',
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  // Pantalla de carga mientras Python arranca
  win.loadURL(`data:text/html,
    <html><head><style>
      body{margin:0;height:100vh;display:flex;flex-direction:column;
           align-items:center;justify-content:center;
           font-family:system-ui;background:#fff;color:#111;font-weight:700}
      .icon{font-size:52px;margin-bottom:16px}
      .msg{font-size:16px;color:#666}
      .dot{display:inline-block;animation:b 1.2s ease-in-out infinite}
      .dot:nth-child(2){animation-delay:.2s}
      .dot:nth-child(3){animation-delay:.4s}
      @keyframes b{0%,100%{opacity:.2}50%{opacity:1}}
    </style></head><body>
      <div class="icon">🏛</div>
      <div>Iniciando asistente</div>
      <div class="msg">
        <span class="dot">.</span><span class="dot">.</span><span class="dot">.</span>
      </div>
    </body></html>`);

  waitReady(READY_RETRIES, ok => {
    if (ok) {
      // Limpiar caché HTTP para que siempre cargue los estáticos frescos
      win.webContents.session.clearCache().then(() => {
        win.loadURL(`http://localhost:${PORT}?_=${Date.now()}`);
      });
    } else {
      win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(errorPage()));
    }
  });

  // Links externos se abren en el navegador del sistema
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith('http://localhost')) shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (e, url) => {
    if (!url.startsWith('http://localhost')) {
      e.preventDefault();
      shell.openExternal(url);
    }
  });
}

// ── IPC: abrir enlace externo ────────────────────────────────
ipcMain.handle('open-external', (e, url) => {
  if (typeof url === 'string' && (url.startsWith('https://') || url.startsWith('http://'))) {
    shell.openExternal(url);
  }
});

// ── IPC: ventana de Sesiones ─────────────────────────────────
let sessionsWin = null;
ipcMain.handle('open-sessions', () => {
  if (sessionsWin && !sessionsWin.isDestroyed()) {
    sessionsWin.focus();
    return;
  }
  sessionsWin = new BrowserWindow({
    width: 1100, height: 750,
    minWidth: 800, minHeight: 550,
    title: 'Sesiones del Congreso',
    icon: path.join(__dirname, 'static', 'app-icon.png'),
    backgroundColor: '#ffffff',
    webPreferences: {
      nodeIntegration:  false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });
  sessionsWin.loadURL(`http://localhost:${PORT}/sessions`);
  sessionsWin.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith('http://localhost')) shell.openExternal(url);
    return { action: 'deny' };
  });
  sessionsWin.on('closed', () => { sessionsWin = null; });
});

// ── IPC: exportar PDF ────────────────────────────────────────
ipcMain.handle('export-pdf', async (event, html) => {
  const date = new Date().toISOString().slice(0, 10);
  const { filePath } = await dialog.showSaveDialog(win, {
    title: 'Guardar PDF',
    defaultPath: path.join(os.homedir(), `Resumen-Congreso-${date}.pdf`),
    filters: [{ name: 'PDF', extensions: ['pdf'] }],
  });
  if (!filePath) return { ok: false };

  const tmp = path.join(os.tmpdir(), `congreso-export-${Date.now()}.html`);
  fs.writeFileSync(tmp, html, 'utf8');

  const pdfWin = new BrowserWindow({ show: false });
  await pdfWin.loadFile(tmp);
  const data = await pdfWin.webContents.printToPDF({ printBackground: true, pageSize: 'A4' });
  pdfWin.destroy();
  try { fs.unlinkSync(tmp); } catch {}
  fs.writeFileSync(filePath, data);
  return { ok: true };
});

// ── IPC: exportar Word ───────────────────────────────────────
ipcMain.handle('export-word', async (event, content) => {
  const date = new Date().toISOString().slice(0, 10);
  const { filePath } = await dialog.showSaveDialog(win, {
    title: 'Guardar Word',
    defaultPath: path.join(os.homedir(), `Resumen-Congreso-${date}.docx`),
    filters: [{ name: 'Word Document', extensions: ['docx'] }],
  });
  if (!filePath) return { ok: false };

  const res = await fetch(`http://localhost:${PORT}/export/docx`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
  const buf = Buffer.from(await res.arrayBuffer());
  fs.writeFileSync(filePath, buf);
  return { ok: true };
});

// ── IPC: historial de chats (archivo JSON en disco) ─────────
const HISTORY_FILE = path.join(app.getPath('userData'), 'chat-history.json');

ipcMain.handle('save-history', (event, data) => {
  try {
    fs.writeFileSync(HISTORY_FILE, JSON.stringify(data), 'utf8');
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle('load-history', () => {
  try {
    if (!fs.existsSync(HISTORY_FILE)) return [];
    return JSON.parse(fs.readFileSync(HISTORY_FILE, 'utf8')) || [];
  } catch {
    return [];
  }
});

// ── Auto-update ──────────────────────────────────────────────
function setupAutoUpdater() {
  if (!app.isPackaged) return;

  autoUpdater.checkForUpdates();

  autoUpdater.on('update-downloaded', () => {
    dialog.showMessageBox(win, {
      type: 'info',
      title: 'Actualización lista',
      message: 'Hay una nueva versión de Diana. ¿Instalar ahora?',
      buttons: ['Instalar y reiniciar', 'Después'],
    }).then(({ response }) => {
      if (response === 0) autoUpdater.quitAndInstall();
    });
  });
}

// ── Ciclo de vida ────────────────────────────────────────────
app.whenReady().then(() => {
  Menu.setApplicationMenu(null);
  startServer();
  createWindow();
  setupAutoUpdater();
  globalShortcut.register('CmdOrCtrl+Shift+R', () => {
    win?.webContents.reloadIgnoringCache();
  });
});

app.on('window-all-closed', () => {
  if (py) py.kill();
  app.quit();
});

app.on('before-quit', () => {
  if (py) py.kill();
});
