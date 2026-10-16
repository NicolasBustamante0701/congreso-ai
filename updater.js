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
//   4. Si Diana queda abierta, se reinicia sola con la nueva cuando nadie la
//      usa (20 minutos sin tocar la Mac) y no hay ninguna respuesta ni
//      transcripción en curso.
// El reemplazo del .app lo hace un script aparte, después de que Diana se
// cierra; si algo falla, deja la versión anterior.
const { app, net, powerMonitor } = require('electron');
const { execFile, spawn } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { Readable } = require('stream');
const { pipeline } = require('stream/promises');

const { owner, repo } = require('./package.json').build.publish;
const API_ULTIMO = `https://api.github.com/repos/${owner}/${repo}/releases/latest`;
const BUSCAR_CADA_MS = 60 * 60 * 1000;
const REVISAR_REINICIO_CADA_MS = 5 * 60 * 1000;
const INACTIVO_PARA_REINICIAR_S = 20 * 60;

let log = console.log;
let puerto = 8732;
let enCurso = false;
let listo = null;       // { version, appNuevo }: descargada y verificada
let instalando = false;

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
  if (tam !== asset.size) throw new Error(`descarga incompleta (${tam} de ${asset.size} bytes)`);
  // GitHub publica el sha256 de cada archivo del release; si viene, se verifica.
  const esperado = (asset.digest || '').replace(/^sha256:/, '');
  if (esperado && (await sha256(destino)) !== esperado) throw new Error('el sha256 no coincide');
}

async function buscar() {
  if (enCurso || listo || instalando) return;
  enCurso = true;
  try {
    const res = await net.fetch(API_ULTIMO, { headers: { Accept: 'application/vnd.github+json' } });
    if (!res.ok) throw new Error(`GitHub respondió ${res.status}`);
    const rel = await res.json();
    const version = String(rel.tag_name || '').replace(/^v/, '');
    if (!version || !esMasNueva(version, app.getVersion())) {
      log(`actualización: al día (${app.getVersion()}, último publicado ${version || '?'})`);
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

    const dir = path.join(app.getPath('userData'), 'actualizaciones');
    fs.rmSync(dir, { recursive: true, force: true });
    fs.mkdirSync(dir, { recursive: true });
    const zip = path.join(dir, nombre);
    log(`actualización: descargando ${nombre} (${Math.round(asset.size / 1e6)} MB)`);
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
  } catch (e) {
    log(`actualización falló: ${e.message}`);
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
if mv "$APP" "$RESPALDO" && /usr/bin/ditto "$NUEVO" "$APP"; then
  /usr/bin/xattr -cr "$APP"
  rm -rf "$RESPALDO" "$NUEVO"
  echo "$(date -u +%FT%TZ) actualización instalada en $APP" >> "$LOG"
else
  rm -rf "$APP"; mv "$RESPALDO" "$APP"
  echo "$(date -u +%FT%TZ) actualización FALLÓ: quedó la versión anterior" >> "$LOG"
fi
[ "$REABRIR" = 1 ] && /usr/bin/open "$APP"
rm -f "$0"
`, { mode: 0o755 });
  log(`actualización: instalando v${listo.version} en ${bundle}${reabrir ? ' (se vuelve a abrir)' : ''}`);
  spawn('/bin/bash', [script, String(process.pid), listo.appNuevo, bundle, respaldo, logArchivo, reabrir ? '1' : '0'],
    { detached: true, stdio: 'ignore' }).unref();
}

function iniciar({ logMain, puerto: p }) {
  if (!app.isPackaged || process.platform !== 'darwin') return;
  log = (m) => logMain(m);
  puerto = p;
  setTimeout(buscar, 15 * 1000);
  setInterval(buscar, BUSCAR_CADA_MS);
  setInterval(reiniciarSiNadieLaUsa, REVISAR_REINICIO_CADA_MS);
  // Al cerrar Diana con una versión descargada, se instala sin reabrir.
  app.on('before-quit', () => instalar({ reabrir: false }));
}

module.exports = { iniciar, _esMasNueva: esMasNueva };
