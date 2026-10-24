const { app, BrowserWindow, shell, Menu, globalShortcut, ipcMain, dialog, nativeImage, safeStorage } = require('electron');
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
// Para correr una segunda instancia aislada (pruebas, desarrollo) sin tocar
// los datos ni el puerto de la app que ya está abierta.
if (process.env.DIANA_USER_DATA) app.setPath('userData', process.env.DIANA_USER_DATA);
if (process.platform === 'linux') app.setDesktopName('congreso-ai.desktop');
const updater = require('./updater');

const PORT = Number(process.env.DIANA_PORT) || 8732;   // puerto único para no chocar con adam
// Identifica al server de este arranque (ver waitReady).
const INSTANCIA = require('crypto').randomUUID();
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

// ── Log del proceso principal ───────────────────────────────
// Mismo directorio que el server.log de Python (~/Library/Logs/Diana en
// macOS): ahí quedan las caídas del server que Python no llega a registrar
// (segfault, SIGKILL del sistema por memoria) y los reinicios del watchdog.
let mainLog = null;
function logMain(...parts) {
  const line = `${new Date().toISOString()} ${parts.join(' ')}`;
  console.log('[diana]', line);
  try {
    if (!mainLog) {
      const dir = app.getPath('logs');
      fs.mkdirSync(dir, { recursive: true });
      mainLog = fs.createWriteStream(path.join(dir, 'main.log'), { flags: 'a' });
    }
    mainLog.write(line + '\n');
  } catch { /* sin log a archivo, la app sigue */ }
}

// ── API key propia del usuario ──────────────────────────────
// La app se distribuye sin keys: cada usuario pega la suya en Ajustes de IA.
// Se guarda en userData, cifrada con el llavero del sistema (safeStorage), y
// se le pasa al server como variable de entorno. En desarrollo gana sobre el
// .env porque load_dotenv no pisa variables ya definidas.
const AI_SETTINGS_FILE = path.join(app.getPath('userData'), 'ai-settings.json');
const PROVIDER_ENV = {
  gemini: 'GEMINI_API_KEY', groq: 'GROQ_API_KEY',
  openai: 'OPENAI_API_KEY',
  mistral: 'MISTRAL_API_KEY',
};

// Sin llavero disponible la key NO se escribe a disco en texto plano: queda
// solo en memoria hasta que se cierre la app, y Ajustes la vuelve a pedir.
const memKeys = {};

function encryptKey(value, field) {
  if (safeStorage.isEncryptionAvailable()) {
    return { enc: safeStorage.encryptString(value).toString('base64') };
  }
  logMain(`safeStorage no disponible: la key (${field}) queda solo en memoria`);
  memKeys[field] = value;
  return { mem: true };
}

function decryptKey(stored, field) {
  if (!stored) return '';
  if (stored.mem) return memKeys[field] || '';
  // `plain` es de versiones anteriores que sí la guardaban sin cifrar: se
  // sigue leyendo para no dejar al usuario sin key tras actualizar.
  try {
    return stored.enc ? safeStorage.decryptString(Buffer.from(stored.enc, 'base64')) : (stored.plain || '');
  } catch (e) {
    logMain(`no se pudo descifrar una API key guardada: ${e.message}`);
    return '';
  }
}

// Escritura atómica: se escribe a un temporal y se renombra. Con
// writeFileSync directo, un cierre forzado o un corte a mitad de camino
// dejaba el JSON truncado; load-history lo leía como [] y el siguiente
// guardado pisaba todo el historial.
function escribirAtomico(archivo, datos, opciones) {
  const tmp = `${archivo}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, datos, opciones);
  fs.renameSync(tmp, archivo);
}

function readAiSettings() {
  try { return JSON.parse(fs.readFileSync(AI_SETTINGS_FILE, 'utf8')) || {}; }
  catch { return {}; }
}

function aiEnv() {
  const s = readAiSettings();
  const env = {};
  const key = decryptKey(s.apiKey, 'apiKey');
  if (PROVIDER_ENV[s.provider] && key) {
    env.LLM_PROVIDER = s.provider;
    env[PROVIDER_ENV[s.provider]] = key;
  }
  // Con Groq como proveedor de chat, esa misma key sirve para transcribir.
  const groq = decryptKey(s.groqKey, 'groqKey');
  if (groq && !env.GROQ_API_KEY) env.GROQ_API_KEY = groq;
  // Key de respaldo (Mistral gratis): entra cuando el proveedor del chat
  // falla o está saturado — ver LLM_FALLBACKS en config.py.
  const respaldo = decryptKey(s.mistralKey, 'mistralKey');
  if (respaldo && !env.MISTRAL_API_KEY) env.MISTRAL_API_KEY = respaldo;
  return env;
}

// ── Arranca el servidor FastAPI ─────────────────────────────
let quitting      = false;
let serverReady   = false;   // el server respondió al menos una vez desde el último arranque
let restartTimer  = null;
let crashTimes    = [];      // timestamps de caídas recientes, para el backoff

const BACKOFF_MS        = [1000, 2000, 4000, 8000, 16000, 30000];
const CRASH_WINDOW_MS   = 5 * 60 * 1000;
const MAX_CRASHES       = 6;   // en CRASH_WINDOW_MS: más que esto, se rinde y muestra el error

function startServer() {
  const isDev = !app.isPackaged;
  const exe     = isDev ? devPython()                                       : path.join(process.resourcesPath, 'server', 'server');
  const args    = isDev ? [path.join(__dirname, 'server.py')]               : [];
  const cwd     = isDev ? __dirname                                          : path.join(process.resourcesPath, 'server');

  serverReady = false;
  // detached: el server es líder de su propio grupo de procesos, así al
  // cerrar se le manda la señal al GRUPO entero y los ffmpeg que lanzó para
  // transcribir no quedan huérfanos consumiendo red y CPU.
  const proc = spawn(exe, args, {
    env: { ...process.env, ...aiEnv(), PORT: String(PORT), DIANA_LOG_DIR: app.getPath('logs'),
           DIANA_DATA_DIR: app.getPath('userData'), DIANA_PARENT_PID: String(process.pid),
           DIANA_INSTANCIA: INSTANCIA },
    cwd,
    detached: process.platform !== 'win32',
  });
  py = proc;
  logMain(`server iniciado pid=${proc.pid}`);
  proc.stdout.on('data', d => process.stdout.write('[py] ' + d));
  proc.stderr.on('data', d => {
    process.stderr.write('[py] ' + d);
    pyErr.push(String(d));
    if (pyErr.length > 40) pyErr.shift();
  });
  proc.on('error', err => logMain(`no se pudo lanzar el server: ${err.message}`));
  proc.on('exit', (code, signal) => {
    if (py === proc) py = null;
    if (quitting) return;
    if (restartRequested) {
      restartRequested = false;
      return;   // reinicio pedido (ajustes de IA): lo arranca restartServer
    }
    logMain(`server terminó pid=${proc.pid} código=${code} señal=${signal}`);
    logMain('últimas líneas de stderr:\n' + pyErr.slice(-10).join('').trim());
    scheduleRestart();
  });
}

// Señal al grupo del server (server + ffmpeg/yt-dlp hijos). Si el grupo ya
// no existe, cae a la señal directa al proceso.
function killServer(proc, signal = 'SIGTERM') {
  if (!proc) return;
  try {
    if (process.platform !== 'win32') process.kill(-proc.pid, signal);
    else proc.kill(signal);
  } catch {
    try { proc.kill(signal); } catch { /* ya terminó */ }
  }
}

// Reinicio a pedido, para que el server tome una API key nueva. No cuenta
// como caída: no pasa por el backoff de scheduleRestart.
let restartRequested = false;
function restartServer() {
  return new Promise(resolve => {
    const arrancar = () => {
      startServer();
      waitReady(READY_RETRIES, ok => {
        if (ok) serverReady = true;
        logMain(`server reiniciado por ajustes de IA: ${ok ? 'listo' : 'no levantó'}`);
        resolve(ok);
      });
    };
    if (!py) return arrancar();
    restartRequested = true;
    serverReady = false;
    py.once('exit', arrancar);
    killServer(py);
  });
}

function scheduleRestart() {
  if (quitting || restartTimer) return;
  const now = Date.now();
  crashTimes = crashTimes.filter(t => now - t < CRASH_WINDOW_MS);
  crashTimes.push(now);
  if (crashTimes.length > MAX_CRASHES) {
    logMain(`${crashTimes.length} caídas en ${CRASH_WINDOW_MS / 60000} min: no se reinicia más`);
    if (win && !win.isDestroyed()) {
      win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(errorPage()));
    }
    return;
  }
  const delay = BACKOFF_MS[Math.min(crashTimes.length - 1, BACKOFF_MS.length - 1)];
  logMain(`reiniciando el server en ${delay} ms (caída ${crashTimes.length}/${MAX_CRASHES})`);
  restartTimer = setTimeout(() => {
    restartTimer = null;
    startServer();
    waitReady(READY_RETRIES, ok => {
      if (!ok) return;   // si no levanta, su 'exit' vuelve a programar el reinicio
      serverReady = true;
      logMain('server de vuelta en línea');
      // La página ya cargada sigue sirviendo (mismo origen); solo hace falta
      // recargar si la ventana quedó en la pantalla de error.
      if (win && !win.isDestroyed() && !win.webContents.getURL().startsWith('http://localhost')) {
        win.loadURL(`http://localhost:${PORT}?_=${Date.now()}`);
      }
    });
  }, delay);
}

// ── Watchdog de "vivo pero colgado" ─────────────────────────
// Un proceso que no murió pero dejó de responder (event loop trabado) no
// dispara 'exit'. Si /status no contesta HEALTH_FAILS veces seguidas, se lo
// mata y el handler de 'exit' de arriba lo reinicia.
const HEALTH_INTERVAL_MS = 10000;
const HEALTH_TIMEOUT_MS  = 5000;
const HEALTH_FAILS       = 3;
let healthFails = 0;

function healthCheck() {
  if (!py || !serverReady || quitting) return;
  const req = http.get({ host: '127.0.0.1', port: PORT, path: '/status', timeout: HEALTH_TIMEOUT_MS }, res => {
    res.resume();
    healthFails = 0;
  });
  req.on('timeout', () => req.destroy(new Error('timeout')));
  req.on('error', err => {
    healthFails += 1;
    logMain(`health check falló (${healthFails}/${HEALTH_FAILS}): ${err.message}`);
    if (healthFails >= HEALTH_FAILS && py) {
      healthFails = 0;
      logMain(`server colgado, matando pid=${py.pid}`);
      killServer(py, 'SIGKILL');
    }
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

// Solo cuenta como listo el server que lanzó ESTA app: antes cualquier cosa
// que respondiera en el puerto servía, y con un server viejo huérfano ahí
// (visto el 19/10/2026: un build del 13/10 seguía escuchando) la ventana
// cargaba ese código viejo mientras el nuevo moría por puerto ocupado.
let avisoPuertoAjeno = false;
function waitReady(retries, cb, startedAt = Date.now()) {
  if (Date.now() - startedAt > SLOW_AFTER_MS) notifySlowStart();

  const reintentar = () => {
    if (retries > 0) setTimeout(() => waitReady(retries - 1, cb, startedAt), 400);
    else             cb(false);
  };
  http.get(`http://localhost:${PORT}/status`, res => {
    let cuerpo = '';
    res.setEncoding('utf8');
    res.on('data', d => { cuerpo += d; });
    res.on('end', () => {
      let instancia = null;
      try { instancia = JSON.parse(cuerpo).instancia; } catch { /* no es Diana */ }
      if (instancia === INSTANCIA) return cb(true);
      if (!avisoPuertoAjeno) {
        avisoPuertoAjeno = true;
        logMain(`el puerto ${PORT} lo ocupa otro proceso (instancia=${instancia ?? 'desconocida'})`);
      }
      reintentar();
    });
  }).on('error', reintentar);
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
    ? `<p>La app no pudo arrancar su servidor interno. Prueba cerrarla y abrirla
         de nuevo; si sigue igual, puede que otro programa esté ocupando el
         puerto ${PORT}.</p>
       <p class="lbl">Si el problema persiste</p>
       <p>Reportalo en
         <a href="https://github.com/nicobus0701-dot/congreso-ai/issues">GitHub</a>
         pegando el detalle de abajo.</p>`
    : `<p>Falta el entorno de Python o alguna dependencia:</p>
       <pre>python3 -m venv .venv &amp;&amp; .venv/bin/pip install -r requirements.txt</pre>
       <p>O corre <code>./start.sh</code>, que lo hace solo.</p>`;

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
    title: 'Diana',
    icon: path.join(__dirname, 'static', 'app-icon.png'),
    backgroundColor: '#ffffff',
    frame: false,
    webPreferences: {
      nodeIntegration:  false,
      contextIsolation: true,
      sandbox:          true,   // el preload solo usa contextBridge/ipcRenderer
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
      serverReady = true;
      logMain('server listo');
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
    abrirAfuera(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (e, url) => {
    if (!esUrlPropia(url)) {
      e.preventDefault();
      abrirAfuera(url);
    }
  });
}

// ── Navegación: qué es "la app" y qué se abre afuera ─────────
// Antes era url.startsWith('http://localhost'), que también dejaba pasar
// http://localhost.evil.com o http://localhost:9999 (otro servicio local).
function esUrlPropia(raw) {
  try {
    const u = new URL(raw);
    return u.protocol === 'http:' &&
           (u.hostname === 'localhost' || u.hostname === '127.0.0.1') &&
           Number(u.port) === PORT;
  } catch { return false; }
}

// Solo http/https van al navegador del sistema: file:, javascript:, smb:…
// que pudieran venir en un enlace raspado no se abren.
function abrirAfuera(raw) {
  try {
    const u = new URL(raw);
    if ((u.protocol === 'https:' || u.protocol === 'http:') && !esUrlPropia(raw)) shell.openExternal(u.href);
  } catch { /* URL inválida: se ignora */ }
}

// ── IPC: abrir enlace externo ────────────────────────────────
ipcMain.handle('open-external', (e, url) => {
  if (typeof url === 'string') abrirAfuera(url);
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
  // Sin esto, un error del server se guardaba como .docx y se informaba "ok".
  if (!res.ok) return { ok: false, error: `El servidor respondió ${res.status}` };
  const buf = Buffer.from(await res.arrayBuffer());
  fs.writeFileSync(filePath, buf);
  return { ok: true };
});

// ── IPC: ajustes de IA ───────────────────────────────────────
ipcMain.handle('ai-settings-get', () => {
  const s = readAiSettings();
  const key = decryptKey(s.apiKey, 'apiKey');
  return {
    provider:   PROVIDER_ENV[s.provider] ? s.provider : null,
    hasKey:     Boolean(key),
    keyHint:    key ? key.slice(-4) : '',
    hasGroqKey: Boolean(decryptKey(s.groqKey, 'groqKey')),
    hasMistralKey: Boolean(decryptKey(s.mistralKey, 'mistralKey')),
  };
});

ipcMain.handle('ai-settings-save', async (event, { provider, apiKey, groqKey, mistralKey } = {}) => {
  if (!PROVIDER_ENV[provider]) return { ok: false, error: 'Proveedor desconocido.' };
  const s = readAiSettings();
  s.provider = provider;
  if (typeof apiKey === 'string' && apiKey.trim()) s.apiKey = encryptKey(apiKey.trim(), 'apiKey');
  if (typeof groqKey === 'string' && groqKey.trim()) s.groqKey = encryptKey(groqKey.trim(), 'groqKey');
  if (typeof mistralKey === 'string' && mistralKey.trim()) s.mistralKey = encryptKey(mistralKey.trim(), 'mistralKey');
  try {
    escribirAtomico(AI_SETTINGS_FILE, JSON.stringify(s), { encoding: 'utf8', mode: 0o600 });
  } catch (e) {
    return { ok: false, error: `No se pudo guardar: ${e.message}` };
  }
  logMain(`ajustes de IA guardados: proveedor=${provider}`);
  const ok = await restartServer();
  return ok ? { ok: true } : { ok: false, error: 'El servidor no volvió a arrancar. Cierra y abre la app.' };
});

ipcMain.handle('ai-settings-clear', async () => {
  try { fs.unlinkSync(AI_SETTINGS_FILE); } catch { /* ya no estaba */ }
  for (const k of Object.keys(memKeys)) delete memKeys[k];
  logMain('ajustes de IA borrados');
  const ok = await restartServer();
  return { ok };
});

// ── IPC: historial de chats (archivo JSON en disco) ─────────
const HISTORY_FILE = path.join(app.getPath('userData'), 'chat-history.json');

ipcMain.handle('save-history', (event, data) => {
  try {
    escribirAtomico(HISTORY_FILE, JSON.stringify(data), 'utf8');
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

// ── Ciclo de vida ────────────────────────────────────────────
app.whenReady().then(() => {
  Menu.setApplicationMenu(null);
  startServer();
  createWindow();
  updater.iniciar({ logMain, puerto: PORT });
  setInterval(healthCheck, HEALTH_INTERVAL_MS);
  globalShortcut.register('CmdOrCtrl+Shift+R', () => {
    win?.webContents.reloadIgnoringCache();
  });
});

app.on('window-all-closed', () => {
  quitting = true;
  killServer(py);
  app.quit();
});

app.on('before-quit', () => {
  quitting = true;
  clearTimeout(restartTimer);
  killServer(py);
});
