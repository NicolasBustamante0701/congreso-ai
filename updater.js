// ── Actualización automática ────────────────────────────────
//
// electron-updater en macOS necesita una app firmada con certificado de
// Apple Developer (Squirrel.Mac rechaza la actualización si la firma no
// coincide) y un .zip en el release. Diana no tiene ninguna de las dos, así
// que nunca se actualizó sola: hasta la v1.0.13 había que instalar cada DMG a
// mano (visto el 16/10/2026).
//
// Este actualizador no depende de la firma y no le pregunta nada al usuario:
//   1. Consulta el último release de GitHub al abrir y cada hora.
//   2. Si hay una versión nueva, baja el .zip de la arquitectura de esta Mac
//      (arm64 en Apple Silicon, x64 en Intel) y lo verifica.
//   3. La instala al cerrar Diana: la próxima vez que se abre, ya es la nueva.
//      Si se cerró antes de instalarla, lo descargado queda guardado y se
//      instala al abrir (antes se borraba y había que esperar otra descarga
//      entera con la app abierta: visto el 24/10/2026, una Mac que se abría y
//      cerraba rápido nunca se actualizaba).
//   Mientras tanto, la ventana muestra "Actualizar ahora".
//   4. Si Diana queda abierta, se reinicia sola con la nueva cuando nadie la
//      usa (20 minutos sin tocar la Mac) y no hay ninguna respuesta ni
//      transcripción en curso.
// El reemplazo del .app lo hace un script aparte, después de que Diana se
// cierra; si algo falla, deja la versión anterior.
const { app, net, powerMonitor } = require('electron');
const { execFile, execFileSync, spawn } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { Readable } = require('stream');
const { pipeline } = require('stream/promises');

// Fijo acá y no leído de package.json: electron-builder le quita la sección
// "build" al package.json que empaqueta, y leerla tiraba la app al arrancar.
const REPO = 'NicolasBustamante0701/congreso-ai';
const API_ULTIMO = `https://api.github.com/repos/${REPO}/releases/latest`;
const BUSCAR_CADA_MS = 60 * 60 * 1000;
// Tras un error (sin red, GitHub con 403/429), se reintenta antes de la hora.
const REINTENTAR_MS = 10 * 60 * 1000;
const WEB_ULTIMO = `https://github.com/${REPO}/releases/latest`;
const REVISAR_REINICIO_CADA_MS = 5 * 60 * 1000;
const INACTIVO_PARA_REINICIAR_S = 20 * 60;

let log = console.log;
let puerto = 8732;
let enCurso = false;
let listo = null;       // { version, appNuevo }: descargada y verificada
let instalando = false;
let ventana = () => null;

const dirActualizaciones = () => path.join(app.getPath('userData'), 'actualizaciones');

function versionDe(appPath) {
  try {
    return execFileSync('/usr/libexec/PlistBuddy', ['-c', 'Print :CFBundleShortVersionString',
      path.join(appPath, 'Contents', 'Info.plist')]).toString().trim();
  } catch { return null; }
}

// Versión ya descargada y verificada en una sesión anterior.
function pendienteGuardada() {
  const appNuevo = path.join(dirActualizaciones(), 'nuevo', 'Diana.app');
  const version = fs.existsSync(appNuevo) ? versionDe(appNuevo) : null;
  return version && esMasNueva(version, app.getVersion()) ? { version, appNuevo } : null;
}

function avisarVentana() {
  try { ventana()?.webContents.send('actualizacion-lista', listo?.version); } catch { /* ventana cerrada */ }
}

// Una Mac con Apple Silicon que corre la versión Intel bajo Rosetta pasa a
// la nativa en la próxima actualización.
function arquitectura() {
  return process.arch === 'arm64' || app.runningUnderARM64Translation ? 'arm64' : 'x64';
}

function esMasNueva(a, b) {
  const pa = a.split('.').map(Number), pb = b.split('.').map(Number);
  for (let i = 0; i < 3; i++) {
    if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) > (pb[i] || 0);
  }
  return false;
}

// /Applications/Diana.app (o donde esté instalada).
function bundleActual() {
  return path.resolve(app.getPath('exe'), '..', '..', '..');
}

function motivoParaNoReemplazar(bundle) {
  if (!bundle.endsWith('.app')) return 'la app no está en un .app';
  if (bundle.startsWith('/Volumes/')) return 'se está ejecutando desde el DMG';
  if (bundle.includes('/AppTranslocation/')) return 'macOS la abrió aislada: hay que moverla a Aplicaciones';
  try { fs.accessSync(path.dirname(bundle), fs.constants.W_OK); } catch { return `sin permiso de escritura en ${path.dirname(bundle)}`; }
  return null;
}

function exec(cmd, args) {
  return new Promise((resolve, reject) =>
    execFile(cmd, args, (err, stdout, stderr) => (err ? reject(new Error(stderr || err.message)) : resolve(stdout.trim()))));
}

async function sha256(archivo) {
  const h = crypto.createHash('sha256');
  await pipeline(fs.createReadStream(archivo), h);
  return h.digest('hex');
}

async function descargar(asset, destino) {
  const res = await net.fetch(asset.browser_download_url);
  if (!res.ok) throw new Error(`descarga HTTP ${res.status}`);
  await pipeline(Readable.fromWeb(res.body), fs.createWriteStream(destino));
  const tam = fs.statSync(destino).size;
  const esperado = asset.size ?? Number(res.headers.get('content-length'));
  if (esperado && tam !== esperado) throw new Error(`descarga incompleta (${tam} de ${esperado} bytes)`);
  // GitHub publica el sha256 de cada archivo del release; si viene, se verifica.
  const sha = (asset.digest || '').replace(/^sha256:/, '');
  if (sha && (await sha256(destino)) !== sha) throw new Error('el sha256 no coincide');
}

// Último release. La API de GitHub limita a 60 consultas por hora por IP sin
// cuenta (en una red compartida responde 403); si falla, el link público
// /releases/latest redirige a /releases/tag/vX.Y.Z y los archivos tienen
// dirección fija. Sin la API no hay sha256: valen el tamaño, que el zip se
// descomprima y que traiga la versión anunciada.
async function ultimoRelease() {
  const res = await net.fetch(API_ULTIMO, { headers: { Accept: 'application/vnd.github+json' } });
  if (res.ok) return res.json();
  log(`actualización: la API de GitHub respondió ${res.status}, pruebo por el link público`);
  const web = await net.fetch(WEB_ULTIMO, { method: 'HEAD' });
  const tag = (web.url.match(/\/releases\/tag\/(v[\d.]+)$/) || [])[1];
  if (!tag) throw new Error(`GitHub respondió ${res.status} y el link público no dio la versión (${web.status})`);
  const v = tag.slice(1);
  const asset = (arch) => ({
    name: `Diana-${v}-${arch}.zip`,
    browser_download_url: `https://github.com/${REPO}/releases/download/${tag}/Diana-${v}-${arch}.zip`,
  });
  return { tag_name: tag, assets: [asset('arm64'), asset('x64')] };
}

async function buscar() {
  if (enCurso || listo || instalando) return;
  enCurso = true;
  try {
    const rel = await ultimoRelease();
    const version = String(rel.tag_name || '').replace(/^v/, '');
    if (!version || !esMasNueva(version, app.getVersion())) {
      log(`actualización: al día (${app.getVersion()}, último publicado ${version || '?'})`);
      return;
    }
    const guardada = pendienteGuardada();
    if (guardada && guardada.version === version) {
      listo = guardada;
      avisarVentana();
      return;
    }
    const motivo = motivoParaNoReemplazar(bundleActual());
    if (motivo) {
      log(`actualización v${version} disponible, pero no se puede instalar sola: ${motivo}`);
      return;
    }
    const nombre = `Diana-${version}-${arquitectura()}.zip`;
    const asset = (rel.assets || []).find((a) => a.name === nombre);
    if (!asset) throw new Error(`el release v${version} no trae ${nombre}`);

    const dir = dirActualizaciones();
    fs.rmSync(dir, { recursive: true, force: true });
    fs.mkdirSync(dir, { recursive: true });
    const zip = path.join(dir, nombre);
    log(`actualización: descargando ${nombre}${asset.size ? ` (${Math.round(asset.size / 1e6)} MB)` : ''}`);
    await descargar(asset, zip);
    const extraido = path.join(dir, 'nuevo');
    await exec('/usr/bin/ditto', ['-x', '-k', zip, extraido]);
    fs.rmSync(zip, { force: true });
    const appNuevo = path.join(extraido, 'Diana.app');
    const vNueva = await exec('/usr/libexec/PlistBuddy', ['-c', 'Print :CFBundleShortVersionString',
      path.join(appNuevo, 'Contents', 'Info.plist')]);
    if (vNueva !== version) throw new Error(`el zip trae la versión ${vNueva}, no ${version}`);
    listo = { version, appNuevo };
    log(`actualización v${version} lista: se instala al cerrar Diana o cuando nadie la esté usando`);
    avisarVentana();
  } catch (e) {
    log(`actualización falló: ${e.message} (reintento en ${REINTENTAR_MS / 60000} min)`);
    setTimeout(buscar, REINTENTAR_MS);
  } finally {
    enCurso = false;
  }
}

// Respuestas o transcripciones en curso en el servidor (services/sse.py).
async function servidorOcupado() {
  try {
    const r = await net.fetch(`http://127.0.0.1:${puerto}/status`);
    return ((await r.json()).ocupado || 0) > 0;
  } catch {
    return false;
  }
}

async function reiniciarSiNadieLaUsa() {
  if (!listo || instalando) return;
  if (powerMonitor.getSystemIdleTime() < INACTIVO_PARA_REINICIAR_S) return;
  if (await servidorOcupado()) return;
  log('actualización: Diana sin uso, se reinicia con la versión nueva');
  instalar({ reabrir: true });
  app.quit();
}

// El .app no se puede pisar mientras corre: el script espera a que Diana se
// cierre, cambia el bundle (si algo falla vuelve a dejar el anterior) y, si
// corresponde, la abre de nuevo.
function instalar({ reabrir }) {
  if (!listo || instalando) return;
  instalando = true;
  const bundle = bundleActual();
  const respaldo = path.dirname(listo.appNuevo);
  const script = path.join(os.tmpdir(), `diana-actualizar-${Date.now()}.sh`);
  const logArchivo = path.join(app.getPath('logs'), 'main.log');
  fs.writeFileSync(script, `#!/bin/bash
PID="$1"; NUEVO="$2"; APP="$3"; RESPALDO="$4/Diana-anterior.app"; LOG="$5"; REABRIR="$6"
for i in $(seq 1 240); do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
rm -rf "$RESPALDO"
# Si el primer mv falla, la app sigue en su lugar y no se toca: antes caía en
# la rama de error, que borraba "$APP" sin respaldo y Diana desaparecía.
if ! mv "$APP" "$RESPALDO"; then
  echo "$(date -u +%FT%TZ) actualización FALLÓ: no se pudo mover $APP; quedó la versión anterior" >> "$LOG"
  rm -rf "$NUEVO"
elif /usr/bin/ditto "$NUEVO" "$APP"; then
  /usr/bin/xattr -cr "$APP"
  rm -rf "$RESPALDO" "$NUEVO"
  echo "$(date -u +%FT%TZ) actualización instalada en $APP" >> "$LOG"
else
  rm -rf "$APP"; mv "$RESPALDO" "$APP"
  rm -rf "$NUEVO"
  echo "$(date -u +%FT%TZ) actualización FALLÓ: quedó la versión anterior" >> "$LOG"
fi
[ "$REABRIR" = 1 ] && /usr/bin/open "$APP"
rm -f "$0"
`, { mode: 0o755 });
  log(`actualización: instalando v${listo.version} en ${bundle}${reabrir ? ' (se vuelve a abrir)' : ''}`);
  spawn('/bin/bash', [script, String(process.pid), listo.appNuevo, bundle, respaldo, logArchivo, reabrir ? '1' : '0'],
    { detached: true, stdio: 'ignore' }).unref();
}

// Se llama al arrancar, antes de abrir la ventana. Si hay una versión ya
// descargada, la instala y devuelve true: main.js cierra y el script la reabre.
function instalarPendienteAlArrancar({ logMain }) {
  if (!app.isPackaged || process.platform !== 'darwin') return false;
  log = (m) => logMain(m);
  const p = pendienteGuardada();
  if (!p) return false;
  const motivo = motivoParaNoReemplazar(bundleActual());
  if (motivo) { log(`actualización v${p.version} guardada, pero no se puede instalar: ${motivo}`); return false; }
  listo = p;
  log(`actualización: v${p.version} ya estaba descargada, se instala al abrir`);
  instalar({ reabrir: true });
  return true;
}

// Botón "Actualizar ahora" de la ventana.
function actualizarAhora() {
  if (!listo || instalando) return false;
  log('actualización: el usuario pidió actualizar ahora');
  instalar({ reabrir: true });
  app.quit();
  return true;
}

function iniciar({ logMain, puerto: p, ventana: v }) {
  if (!app.isPackaged || process.platform !== 'darwin') return;
  log = (m) => logMain(m);
  puerto = p;
  if (v) ventana = v;
  setTimeout(buscar, 15 * 1000);
  setInterval(buscar, BUSCAR_CADA_MS);
  setInterval(reiniciarSiNadieLaUsa, REVISAR_REINICIO_CADA_MS);
  // Al cerrar Diana con una versión descargada, se instala sin reabrir.
  app.on('before-quit', () => instalar({ reabrir: false }));
}

module.exports = {
  iniciar, instalarPendienteAlArrancar, actualizarAhora,
  versionLista: () => listo?.version || null,
  _esMasNueva: esMasNueva,
};
