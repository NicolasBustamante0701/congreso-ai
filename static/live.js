/* ── Tema (sincronizado en vivo con el chat principal) ──── */
window.addEventListener("message", (e) => {
  if (e.origin !== location.origin) return;   // solo mensajes de la propia app
  if (e.data && e.data.type === "theme") {
    document.documentElement.dataset.theme = e.data.dark ? "dark" : "light";
  }
});

/* ── State ───────────────────────────────────────────── */
let es           = null;
let transcript   = [];
let autoScroll   = true;
let analyzeTimer = null;
let countdown    = 60;
let currentVideoId = "";
let currentIsLive = false;
let lastAnalysisText = "";

// Reconexión: cuántas líneas ya se analizaron (para mandar solo el tramo
// nuevo a /live/analyze) y en qué segundo del video quedó la última línea
// recibida (para retomar ahí si la conexión se corta, en vez de arrancar
// de nuevo desde 0 — ver openTranscriptionStream/onerror).
let lastAnalyzedIndex   = 0;
let lastElapsedSeconds  = 0;
let reconnectAttempts   = 0;
let userStopped         = false;
const MAX_RECONNECT_ATTEMPTS = 4;
const RECONNECT_DELAY_MS     = 2500;

const $ = id => document.getElementById(id);

/* ── DOM refs ────────────────────────────────────────── */
const videoList       = $("lv-video-list");
const listLoading     = $("lv-list-loading");
const refreshBtn      = $("lv-refresh-btn");
const lv_empty        = $("lv-empty");
const lv_columns      = $("lv-columns");
const videoTitleLabel = $("lv-video-title-label");
const indicatorDot    = $("lv-indicator-dot");
const indicatorLabel  = $("lv-indicator-label");
const startBtn        = $("lv-start-btn");
const stopBtn         = $("lv-stop-btn");
const iaSpinner       = $("lv-ia-spinner");
const iaEmpty         = $("lv-ia-empty");
const iaContent       = $("lv-ia-content");
const iaBody          = $("lv-ia-body");
const iaFooter        = $("lv-ia-footer");
const iaCountdown     = $("lv-ia-countdown");
const iaNowBtn        = $("lv-ia-now-btn");
const transcriptBody  = $("lv-transcript-body");
const transcriptEmpty = $("lv-transcript-empty");
const linesEl         = $("lv-lines");
const scrollBtn       = $("lv-scroll-btn");
const copyBtn         = $("lv-copy-btn");
const transcriptActions = $("lv-transcript-actions");
const pdfBtnT          = $("lv-pdf-btn-t");
const wordBtnT         = $("lv-word-btn-t");
const iaActions        = $("lv-ia-actions");
const iaCopyBtn        = $("lv-ia-copy-btn");
const iaPdfBtn         = $("lv-ia-pdf-btn");
const iaWordBtn        = $("lv-ia-word-btn");
const transcriptMenuBtn = $("lv-transcript-menu-btn");
const transcriptMenu    = $("lv-transcript-menu");
const iaMenuBtn         = $("lv-ia-menu-btn");
const iaMenu            = $("lv-ia-menu");

/* ── Reproductor de YouTube (API real, no <iframe src> a pelo) ────────
   Necesitamos leer en qué segundo va el video para poder arrancar la
   transcripción justo ahí si el usuario adelantó/atrasó — con un <iframe>
   simple no hay forma de preguntarle eso. */
let ytPlayer   = null;
let ytApiReady = null; // Promise

function loadYouTubeApi() {
  if (ytApiReady) return ytApiReady;
  ytApiReady = new Promise((resolve) => {
    if (window.YT && window.YT.Player) { resolve(); return; }
    const prevCb = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => { if (prevCb) prevCb(); resolve(); };
    const tag = document.createElement("script");
    tag.src = "https://www.youtube.com/iframe_api";
    document.head.appendChild(tag);
  });
  return ytApiReady;
}

async function embedVideo(id) {
  await loadYouTubeApi();
  if (ytPlayer && typeof ytPlayer.loadVideoById === "function") {
    ytPlayer.loadVideoById(id);
    return;
  }
  ytPlayer = new YT.Player("lv-iframe", {
    videoId: id,
    playerVars: { autoplay: 1 },
  });
}

/* Segundo exacto donde va el video ahora mismo (0 si no se puede leer aún). */
function getPlayerSeconds() {
  try {
    const t = ytPlayer && typeof ytPlayer.getCurrentTime === "function"
      ? ytPlayer.getCurrentTime() : 0;
    return Number.isFinite(t) && t > 0 ? Math.floor(t) : 0;
  } catch (_) {
    return 0;
  }
}

/* ── Menú compacto de Copiar/PDF/Word (header de cada columna) ── */
function setupMenu(btn, menu) {
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = menu.style.display !== "none";
    document.querySelectorAll(".lv-menu-dropdown").forEach(m => m.style.display = "none");
    menu.style.display = open ? "none" : "flex";
  });
  menu.addEventListener("click", (e) => {
    if (e.target.closest("button")) menu.style.display = "none";
  });
}
setupMenu(transcriptMenuBtn, transcriptMenu);
setupMenu(iaMenuBtn, iaMenu);
document.addEventListener("click", () => {
  document.querySelectorAll(".lv-menu-dropdown").forEach(m => m.style.display = "none");
});

/* ── Load video list ─────────────────────────────────── */
async function loadVideos() {
  listLoading.style.display = "flex";
  // Clear existing cards
  Array.from(videoList.querySelectorAll(".lv-video-card")).forEach(c => c.remove());

  try {
    const res  = await fetch("/sesiones/videos");
    const data = await res.json();
    listLoading.style.display = "none";

    if (!data.ok || !data.videos?.length) {
      videoList.innerHTML += `<p style="padding:16px 14px;font-size:12px;color:#aaa">No se encontraron videos.</p>`;
      return;
    }

    for (const v of data.videos) {
      const card = document.createElement("div");
      card.className = "lv-video-card";
      card.dataset.id    = v.id;
      card.dataset.title = v.titulo;
      card.dataset.live  = v.en_vivo ? "1" : "0";

      const liveBadge = v.en_vivo
        ? `<span class="lv-badge-live">🔴 EN VIVO</span>`
        : v.programado ? `<span class="lv-badge-past">Programado</span>`
        : v.fue_live ? `<span class="lv-badge-past">Finalizado</span>` : "";

      const duration = v.duracion
        ? `<span class="lv-card-duration">${v.duracion}</span>` : "";

      card.innerHTML = `
        <img class="lv-card-thumb" src="${v.thumb}" alt="" loading="lazy">
        <div class="lv-card-meta">${liveBadge}${duration}</div>
        <div class="lv-card-title">${escHtml(v.titulo)}</div>`;

      card.addEventListener("click", () => selectVideo(v.id, v.titulo, v.en_vivo));
      videoList.appendChild(card);
    }
  } catch (err) {
    listLoading.style.display = "none";
    videoList.innerHTML += `<p style="padding:16px 14px;font-size:12px;color:#c53030">Error al cargar: ${escHtml(String(err))}</p>`;
  }
}

/* ── Select a video ──────────────────────────────────── */
function selectVideo(id, title, isLive) {
  if (!id) return;
  // Stop any running transcription
  stopTranscription(false);

  currentVideoId = id;
  currentIsLive  = !!isLive;

  // Highlight selected card
  document.querySelectorAll(".lv-video-card").forEach(c => {
    c.classList.toggle("active", c.dataset.id === id);
  });

  // Show 3-column layout
  lv_empty.style.display   = "none";
  lv_columns.style.display = "";

  // Embed video (API real de YouTube — necesaria para saber en qué
  // segundo va cuando el usuario adelanta/atrasa, ver getPlayerSeconds()).
  embedVideo(id);
  videoTitleLabel.textContent = title;

  // Reset transcript panel
  transcript = [];
  linesEl.innerHTML = "";
  currentLineEl = null;
  transcriptEmpty.style.display = "";
  transcriptActions.style.display = "none";
  autoScroll = true;
  scrollBtn.style.display = "none";

  // Reset IA panel
  iaContent.style.display = "none";
  iaEmpty.style.display   = "";
  iaContent.innerHTML     = "";
  iaFooter.style.display  = "none";
  iaActions.style.display = "none";
  lastAnalysisText = "";
  lastAnalyzedIndex = 0;
  reconnectAttempts = 0;
  userStopped = false;

  // Update indicator
  setStatus(isLive ? "🔴 En vivo" : "Sesión grabada", isLive ? "live" : "idle");

  // Show start button
  startBtn.style.display = "";
  stopBtn.style.display  = "none";
  qaForm.style.display = "none";

  abrirGuardada(id);
}

/* ── Transcription controls ──────────────────────────── */
function startTranscription() {
  if (!currentVideoId) return;

  transcript = [];
  linesEl.innerHTML = "";
  currentLineEl = null;
  transcriptEmpty.style.display = "";
  transcriptActions.style.display = "none";
  iaContent.innerHTML = "";
  iaContent.style.display = "none";
  iaEmpty.style.display = "";
  iaActions.style.display = "none";
  lastAnalysisText = "";
  lastAnalyzedIndex = 0;
  autoScroll = true;
  scrollBtn.style.display = "none";

  userStopped       = false;
  reconnectAttempts = 0;

  startBtn.style.display = "none";
  stopBtn.style.display  = "";
  setStatus("Conectando...", "live");

  // Si el video es grabado y el usuario adelantó/atrasó el reproductor,
  // arrancamos la transcripción justo en ese segundo en vez de siempre
  // desde 0:00. En un stream en vivo no aplica (no hay "adelantar").
  const startSec = currentIsLive ? 0 : getPlayerSeconds();
  lastElapsedSeconds = startSec;

  openTranscriptionStream(startSec);
}

/*
 * Abre el EventSource. Se usa tanto para el arranque manual como para
 * reconectar tras un corte (ver onerror más abajo) — por eso el segundo
 * en el que arranca es un parámetro, no siempre 0/getPlayerSeconds().
 */
function openTranscriptionStream(startSec) {
  if (es) es.close();
  // El título va para que la sesión quede guardada con nombre (ver
  // routers/live.py, que persiste cada ventana apenas se transcribe).
  es = new EventSource(
    `/live/transcribe?video_id=${encodeURIComponent(currentVideoId)}&start=${startSec}` +
    `&titulo=${encodeURIComponent(videoTitleLabel.textContent || "")}`
  );

  es.onmessage = (e) => {
    if (e.data === "[DONE]") {
      if (es) { es.close(); es = null; }
      stopBtn.style.display  = "none";
      startBtn.style.display = "";
      stopCountdown();
      cargarGuardadas();
      if (!userStopped && transcript.length) {
        // Terminó el video: el informe final sale solo, con lo transcrito.
        setStatus("Transcripción terminada. Armando el informe final…", "transcribing");
        generarInforme();
      } else {
        setStatus("Transcripción terminada.", "idle");
      }
      return;
    }
    try {
      const d = JSON.parse(e.data);
      if (d.info) {
        currentIsLive = !!d.info.en_vivo;
        qaForm.style.display = "";
      }
      if (d.status) setStatus(d.status, d.status.toLowerCase().includes("transcrib") ? "transcribing" : "live");
      if (d.error) {
        setStatus("Error: " + d.error, "idle");
        if (d.fatal) {
          // Key inválida: reconectar repetiría el mismo error.
          stopTranscription(false);
          stopBtn.style.display  = "none";
          startBtn.style.display = "";
        }
      }
      if (d.text) {
        // Conexión sana: cualquier línea real que llegue resetea el
        // contador de reintentos, para no gastar el cupo de reconexión
        // por hipos aislados y aguantar toda la sesión si hace falta.
        reconnectAttempts = 0;
        if (typeof d.elapsed === "number") lastElapsedSeconds = d.elapsed;
        addLine(d.timestamp || "??:??", d.text, d.elapsed);
        if (transcript.length === 1) startCountdown();
      }
    } catch (_) {}
  };

  es.onerror = () => {
    if (es) { es.close(); es = null; }
    stopCountdown();
    if (userStopped) return; // el usuario ya apretó Detener, no reconectar

    if (reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
      reconnectAttempts++;
      setStatus(
        `Conexión interrumpida. Reconectando… (intento ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})`,
        "live"
      );
      // Retoma desde el último segundo transcrito, no desde el inicio —
      // así no se repite/reordena la transcripción ya recibida (en vivo
      // el segundo se ignora del lado del servidor, no afecta).
      setTimeout(() => {
        if (!userStopped) openTranscriptionStream(lastElapsedSeconds);
      }, RECONNECT_DELAY_MS);
    } else {
      setStatus("Conexión interrumpida — no se pudo reconectar. Vuelve a apretar Transcribir.", "idle");
      stopBtn.style.display  = "none";
      startBtn.style.display = "";
    }
  };
}

function stopTranscription(resetUI = true) {
  const estabaCorriendo = !!es;
  userStopped = true;
  if (es) { es.close(); es = null; }
  stopCountdown();
  if (estabaCorriendo) setTimeout(cargarGuardadas, 600);   // el server cierra la sesión al desconectarse
  if (resetUI) {
    stopBtn.style.display  = "none";
    startBtn.style.display = "";
    setStatus("Detenido", "idle");
  }
}

/* ── Helpers ─────────────────────────────────────────── */
function setStatus(msg, dotClass = "idle") {
  indicatorLabel.textContent = msg;
  indicatorDot.className = "lv-indicator-dot " + dotClass;
}

function addLine(ts, text, elapsed) {
  if (!text.trim()) return;
  transcript.push({ ts, text, elapsed: typeof elapsed === "number" ? elapsed : null });
  if (transcript.length === 1) {
    transcriptEmpty.style.display = "none";
    transcriptActions.style.display = "";
  }
  const line = document.createElement("div");
  line.className = "lv-line";
  if (typeof elapsed === "number") line.dataset.elapsed = String(elapsed);
  const seekable = typeof elapsed === "number" && !currentIsLive;
  line.innerHTML = (seekable
    ? `<span class="lv-line-ts lv-ts-link" role="button" tabindex="0" data-s="${elapsed}" title="Ir a este momento del video">[${ts}]</span>`
    : `<span class="lv-line-ts">[${ts}]</span>`) +
    `<span class="lv-line-text">${escHtml(text)}</span>`;
  linesEl.appendChild(line);
  if (autoScroll) transcriptBody.scrollTop = transcriptBody.scrollHeight;
}

/*
 * La transcripción llega adelantada al video (Whisper procesa más rápido de
 * lo que dura reproducirlo) — a propósito, no es un bug. Esto solo marca en
 * negrita qué línea corresponde a dónde va el reproductor AHORA, para que
 * el usuario pueda ubicarse sin tener que calcular el desfase a ojo. Es
 * puramente visual: no toca transcript[], ni el resumen, ni lastAnalyzedIndex.
 */
let currentLineEl = null;
function syncCurrentLine() {
  if (!linesEl.children.length) return;
  const nowSec = getPlayerSeconds();
  if (!nowSec) return;

  let match = null;
  for (const child of linesEl.children) {
    const el = Number(child.dataset.elapsed);
    if (Number.isNaN(el)) continue;
    if (el <= nowSec) match = child;
    else break; // las líneas están en orden, no hace falta seguir mirando
  }
  if (match === currentLineEl) return;
  if (currentLineEl) currentLineEl.classList.remove("lv-line-current");
  if (match) match.classList.add("lv-line-current");
  currentLineEl = match;
}
setInterval(syncCurrentLine, 1000);

function escHtml(s) {
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

/* ── IA analysis ─────────────────────────────────────── */
const RANGE_ICON = '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="7" cy="7" r="5.5"/><path d="M7 4v3l2 1.5"/></svg>';

async function runAnalysis() {
  // Solo se manda (y se cobra tokens de) el tramo NUEVO desde el último
  // análisis — antes se mandaba todo lo acumulado y cada resultado
  // reemplazaba al anterior; ahora cada tramo queda como su propio bloque
  // fechado, y nunca se vuelve a analizar lo que ya se analizó.
  const newLines = transcript.slice(lastAnalyzedIndex);
  if (!newLines.length) return;

  const rangeStart = newLines[0].ts;
  const rangeEnd   = newLines[newLines.length - 1].ts;
  const capturedIndex = transcript.length; // por si llegan líneas nuevas mientras esto corre

  iaEmpty.style.display   = "none";
  iaContent.style.display = "";
  iaSpinner.style.display = "";

  const segment = document.createElement("div");
  segment.className = "lv-ia-segment";
  segment.innerHTML = `
    <div class="lv-ia-segment-range">${RANGE_ICON}<span>${escHtml(rangeStart)} – ${escHtml(rangeEnd)}</span></div>
    <div class="lv-ia-segment-body"></div>`;
  iaContent.appendChild(segment);
  const segmentBody = segment.querySelector(".lv-ia-segment-body");
  iaBody.scrollTop = iaBody.scrollHeight;

  const body = {
    transcript: newLines.map(l => `[${l.ts}] ${l.text}`).join("\n"),
    titulo: videoTitleLabel.textContent,
  };

  try {
    const res = await fetch("/live/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const reader = res.body.getReader();
    const dec    = new TextDecoder();
    let buf = "", segmentText = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n"); buf = parts.pop();
      for (const part of parts) {
        if (!part.startsWith("data: ")) continue;
        const raw = part.slice(6).trim();
        if (raw === "[DONE]") break;
        try {
          const d = JSON.parse(raw);
          if (d.text) {
            segmentText += d.text;
            segmentBody.innerHTML = renderMd(segmentText);
            iaActions.style.display = "";
            iaBody.scrollTop = iaBody.scrollHeight;
          }
        } catch (_) {}
      }
    }
    lastAnalysisText += `${lastAnalysisText ? "\n\n" : ""}[${rangeStart} – ${rangeEnd}]\n${segmentText}`;
    lastAnalyzedIndex = capturedIndex;
  } catch (err) {
    segmentBody.innerHTML = `<p style="color:#c53030">Error: ${escHtml(String(err))}</p>`;
  } finally {
    iaSpinner.style.display = "none";
  }
}

// static/md.js, con `tramos` para que "[00:10 – 01:10]" salga como
// encabezado (también en el PDF/Word exportado). Antes era un parser mínimo
// que no dibujaba tablas.
function renderMd(text) {
  return DianaMD.render(text, { tramos: true });
}

/* ── Countdown ───────────────────────────────────────── */
function startCountdown() {
  countdown = 60; iaFooter.style.display = "";
  clearInterval(analyzeTimer);
  analyzeTimer = setInterval(() => {
    iaCountdown.textContent = --countdown;
    if (countdown <= 0) { runAnalysis(); countdown = 60; iaCountdown.textContent = countdown; }
  }, 1000);
}
function stopCountdown() {
  clearInterval(analyzeTimer); analyzeTimer = null;
  iaFooter.style.display = "none";
}

/* ── Auto-scroll ─────────────────────────────────────── */
transcriptBody.addEventListener("scroll", () => {
  const atBottom = transcriptBody.scrollHeight - transcriptBody.scrollTop <= transcriptBody.clientHeight + 40;
  autoScroll = atBottom;
  scrollBtn.style.display = (!atBottom && transcript.length) ? "" : "none";
});
scrollBtn.addEventListener("click", () => {
  autoScroll = true; scrollBtn.style.display = "none";
  transcriptBody.scrollTop = transcriptBody.scrollHeight;
});

/* ── Copiar / descargar (transcripción y resumen) ────── */
function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.innerHTML;
    btn.textContent = "¡Copiado!";
    setTimeout(() => { btn.innerHTML = orig; }, 2000);
  });
}

function slug(s) {
  return (s || "sesion").normalize("NFD").replace(/[̀-ͯ]/g, "")
    .replace(/[^a-zA-Z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "sesion";
}

function buildPrintHtml(title, bodyHtml) {
  const date = new Date().toLocaleDateString("es-PE", { day: "numeric", month: "long", year: "numeric" });
  return `<!DOCTYPE html><html lang="es"><head>
<meta charset="UTF-8"><title>${escHtml(title)} — Congreso del Perú</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Georgia',serif;font-size:12pt;color:#111;padding:40px 60px;line-height:1.7;max-width:900px;margin:0 auto}
  h1{font-size:17pt;font-weight:bold;margin-bottom:16px}
  h3{font-size:12pt;font-weight:bold;margin:16px 0 6px}
  p{margin-bottom:9px} ul,ol{padding-left:20px;margin-bottom:9px} li{margin-bottom:4px}
  .ts{font-weight:bold;color:#555;margin-right:8px;font-variant-numeric:tabular-nums}
  strong{font-weight:bold}
  table{border-collapse:collapse;width:100%;margin:12px 0;font-size:10pt}
  th{background:#111;color:#fff;padding:6px 10px;text-align:left;font-weight:bold}
  td{border:1px solid #ccc;padding:6px 10px;vertical-align:top}
  tr:nth-child(even) td{background:#f7f7f7}
  .ftr{margin-top:40px;border-top:1px solid #ccc;padding-top:12px;font-size:9pt;color:#666}
  @media print{body{padding:20px 30px}}
</style></head><body>
<h1>${escHtml(title)}</h1>
${bodyHtml}
<div class="ftr">Generado por Diana — Sistema de Monitoreo Parlamentario · ${date}</div>
</body></html>`;
}

/* Este iframe no tiene acceso a window.electronAPI (Electron no inyecta el
   preload en subframes) y window.open() aquí es denegado por el
   setWindowOpenHandler del proceso principal — así que la exportación real
   se delega al frame padre (index.html) vía postMessage. */
let _exportReqId = 0;
const _exportCallbacks = {};

window.addEventListener("message", (e) => {
  if (e.origin !== location.origin) return;   // solo mensajes de la propia app
  if (!e.data || !e.data.reqId || !_exportCallbacks[e.data.reqId]) return;
  const cb = _exportCallbacks[e.data.reqId];
  delete _exportCallbacks[e.data.reqId];
  if (e.data.type === "export-error") alert("Error al exportar: " + e.data.message);
  cb();
});

function requestExport(type, payload, btn) {
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = "Generando…";
  const reqId = ++_exportReqId;
  _exportCallbacks[reqId] = () => { btn.disabled = false; btn.innerHTML = orig; };
  window.parent.postMessage({ type, ...payload, reqId }, location.origin);
}

function exportPdf(html, btn) {
  requestExport("export-pdf", { html }, btn);
}

function exportWord(content, filenameBase, btn) {
  requestExport("export-word", { content, filename: filenameBase }, btn);
}

copyBtn.addEventListener("click", () => {
  copyText(transcript.map(l => `[${l.ts}] ${l.text}`).join("\n"), copyBtn);
});
pdfBtnT.addEventListener("click", () => {
  const bodyHtml = transcript.map(l => `<p><span class="ts">[${escHtml(l.ts)}]</span>${escHtml(l.text)}</p>`).join("");
  const html = buildPrintHtml(`Transcripción — ${videoTitleLabel.textContent}`, bodyHtml);
  exportPdf(html, pdfBtnT);
});
wordBtnT.addEventListener("click", () => {
  const content = transcript.map(l => `[${l.ts}] ${l.text}`).join("\n");
  exportWord(content, `Transcripcion-${slug(videoTitleLabel.textContent)}`, wordBtnT);
});

iaCopyBtn.addEventListener("click", () => copyText(lastAnalysisText, iaCopyBtn));
iaPdfBtn.addEventListener("click", () => {
  const html = buildPrintHtml(`Resumen — ${videoTitleLabel.textContent}`, renderMd(lastAnalysisText));
  exportPdf(html, iaPdfBtn);
});
iaWordBtn.addEventListener("click", () => {
  exportWord(lastAnalysisText, `Resumen-Live-${slug(videoTitleLabel.textContent)}`, iaWordBtn);
});

/* ── Events ──────────────────────────────────────────── */
startBtn.addEventListener("click", startTranscription);
stopBtn.addEventListener("click",  () => stopTranscription(true));
iaNowBtn.addEventListener("click", () => { runAnalysis(); countdown = 60; iaCountdown.textContent = 60; });
refreshBtn.addEventListener("click", loadVideos);

/* ── Transcripciones guardadas, preguntas e informe ─────
   Todo trabaja sobre lo ya transcrito (backend /transcripts): abrir una
   sesión guardada o preguntarle algo no vuelve a procesar el video. */
const savedBox   = $("lv-saved");
const savedList  = $("lv-saved-list");
const qaForm     = $("lv-qa");
const qaInput    = $("lv-qa-input");
const qaSend     = $("lv-qa-send");
const informeBtn = $("lv-informe-btn");
let qaHistorial  = [];
let qaOcupado    = false;

async function cargarGuardadas() {
  try {
    const r = await fetch("/transcripts");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const { sesiones } = await r.json();
    savedList.innerHTML = "";
    savedBox.style.display = sesiones.length ? "" : "none";
    for (const ses of sesiones) {
      const card = document.createElement("div");
      card.className = "lv-video-card lv-saved-card";
      card.dataset.id = ses.video_id;
      card.innerHTML = `
        <div class="lv-card-title">${escHtml(ses.titulo || ses.video_id)}</div>
        <div class="lv-saved-meta">
          <span>${ses.en_vivo ? "en vivo · " : ""}hasta ${escHtml(ses.hasta)}${ses.tiene_informe ? " · informe" : ""}</span>
          <button class="lv-saved-del" title="Borrar esta transcripción" aria-label="Borrar transcripción">×</button>
        </div>`;
      card.addEventListener("click", (e) => {
        if (e.target.closest(".lv-saved-del")) return;
        selectVideo(ses.video_id, ses.titulo || ses.video_id, ses.en_vivo);
      });
      card.querySelector(".lv-saved-del").addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm(`¿Borrar la transcripción guardada de "${ses.titulo || ses.video_id}"? No se puede deshacer.`)) return;
        await fetch(`/transcripts/${encodeURIComponent(ses.video_id)}`, { method: "DELETE" });
        if (ses.video_id === currentVideoId) qaForm.style.display = "none";
        cargarGuardadas();
      });
      savedList.appendChild(card);
    }
  } catch (err) {
    console.warn("no se pudieron cargar las transcripciones guardadas:", err);
  }
}

/* Pinta en la columna de transcripción lo que ya estaba guardado. */
async function abrirGuardada(id) {
  qaHistorial = [];
  let datos;
  try {
    const r = await fetch(`/transcripts/${encodeURIComponent(id)}`);
    if (r.status === 404) return;            // nunca se transcribió: nada que mostrar
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    datos = await r.json();
  } catch (err) {
    console.warn("no se pudo abrir la transcripción guardada:", err);
    return;
  }
  if (id !== currentVideoId || es) return;   // el usuario ya cambió de video o arrancó otra captura
  currentIsLive = !!datos.sesion.en_vivo;
  for (const seg of datos.segmentos) addLine(seg.ts, seg.texto, seg.start_s);
  lastElapsedSeconds = datos.segmentos.length ? datos.segmentos[datos.segmentos.length - 1].end_s : 0;
  const c = datos.cobertura;
  setStatus(`Guardada: ${c.desde} – ${c.hasta}` + (c.huecos.length ? ` · ${c.huecos.length} tramo(s) sin procesar` : ""), "idle");
  qaForm.style.display = datos.segmentos.length ? "" : "none";
  if (datos.sesion.informe) {
    mostrarBloque("Informe final", datos.sesion.informe, `generado ${datos.sesion.informe_generado}`);
  }
}

/* Bloque en la columna de Diana (pregunta o informe), con tablas y
   marcas de tiempo clicables. */
function mostrarBloque(titulo, texto, nota) {
  iaEmpty.style.display   = "none";
  iaContent.style.display = "";
  iaActions.style.display = "";
  const bloque = document.createElement("div");
  bloque.className = "lv-ia-segment";
  bloque.innerHTML = `
    <div class="lv-ia-segment-range"><span>${escHtml(titulo)}</span>${nota ? `<span class="lv-ia-nota">${escHtml(nota)}</span>` : ""}</div>
    <div class="lv-ia-segment-body"></div>`;
  iaContent.appendChild(bloque);
  const body = bloque.querySelector(".lv-ia-segment-body");
  const pintar = (md) => { body.innerHTML = renderMd(md); enlazarTiempos(body); };
  if (texto) pintar(texto);
  iaBody.scrollTop = iaBody.scrollHeight;
  return { body, pintar };
}

/* "[00:24:10]" → botón que lleva el reproductor a ese segundo. El HTML ya
   pasó por DOMPurify: el patrón solo matchea dígitos, así que el reemplazo
   no puede inyectar nada. En vivo no hay segundo de video al cual ir. */
function enlazarTiempos(el) {
  if (currentIsLive) return;
  el.innerHTML = el.innerHTML.replace(/\[(\d{2}):(\d{2}):(\d{2})\]/g, (m, h, mi, se) => {
    const seg = (+h) * 3600 + (+mi) * 60 + (+se);
    return `<span class="lv-ts-link" role="button" tabindex="0" data-s="${seg}" title="Ir a este momento del video">${m}</span>`;
  });
}

function irAlSegundo(seg) {
  if (!Number.isFinite(seg)) return;
  try {
    if (ytPlayer && typeof ytPlayer.seekTo === "function") ytPlayer.seekTo(seg, true);
  } catch (_) { /* reproductor no listo */ }
}

document.addEventListener("click", (e) => {
  const t = e.target.closest(".lv-ts-link");
  if (t) irAlSegundo(Number(t.dataset.s));
});
document.addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.classList?.contains("lv-ts-link")) {
    e.preventDefault();
    irAlSegundo(Number(e.target.dataset.s));
  }
});

async function consultarStream(url, body, titulo) {
  const { pintar } = mostrarBloque(titulo, "", "");
  pintar("_Revisando la transcripción…_");
  let texto = "";
  iaSpinner.style.display = "";
  try {
    await DianaSSE.post(url, body, {
      onEvent(obj) {
        if (obj.error)  { pintar(`${texto}\n\n**Error:** ${obj.error}`); return false; }
        if (obj.status && !texto) pintar(`_${obj.status}_`);
        if (obj.text)   { texto += obj.text; pintar(texto); }
      },
    });
  } catch (err) {
    pintar(`${texto}\n\n**Error de conexión:** ${err.message}`);
  } finally {
    iaSpinner.style.display = "none";
  }
  if (texto) lastAnalysisText += `${lastAnalysisText ? "\n\n" : ""}## ${titulo}\n${texto}`;
  return texto;
}

qaForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const pregunta = qaInput.value.trim();
  if (!pregunta || qaOcupado || !currentVideoId) return;
  qaOcupado = true; qaSend.disabled = true; qaInput.value = "";
  const respuesta = await consultarStream(
    `/transcripts/${encodeURIComponent(currentVideoId)}/preguntar`,
    { pregunta, historial: qaHistorial }, `Pregunta: ${pregunta}`);
  if (respuesta) {
    qaHistorial.push({ role: "user", content: pregunta }, { role: "assistant", content: respuesta });
    qaHistorial = qaHistorial.slice(-6);
  }
  qaOcupado = false; qaSend.disabled = false; qaInput.focus();
});

async function generarInforme() {
  if (qaOcupado || !currentVideoId) return;
  qaOcupado = true; informeBtn.disabled = true;
  await consultarStream(`/transcripts/${encodeURIComponent(currentVideoId)}/informe`, {}, "Informe final");
  qaOcupado = false; informeBtn.disabled = false;
  if (!es) setStatus("Informe listo.", "idle");
  cargarGuardadas();
}
informeBtn.addEventListener("click", generarInforme);

/* Desde la búsqueda de la app: abrir una sesión guardada en un segundo.
   La transcripción y el reproductor cargan asíncronos, así que se reintenta
   unos segundos hasta encontrar la línea y poder posicionar el video. */
window.addEventListener("message", (e) => {
  if (e.origin !== location.origin || e.data?.type !== "abrir-sesion") return;
  const { video_id, titulo, en_vivo, start_s } = e.data;
  if (currentVideoId !== video_id) selectVideo(video_id, titulo || video_id, en_vivo);
  let intentos = 0;
  const buscarLinea = () => {
    const linea = linesEl.querySelector(`.lv-line[data-elapsed="${start_s}"]`);
    if (linea) {
      autoScroll = false;
      linea.scrollIntoView({ block: "center" });
      linea.classList.add("lv-line-buscada");
      setTimeout(() => linea.classList.remove("lv-line-buscada"), 2600);
    }
    const listo = ytPlayer && typeof ytPlayer.seekTo === "function" && typeof ytPlayer.getPlayerState === "function";
    if (listo && !currentIsLive) irAlSegundo(start_s);
    if ((!linea || !listo) && ++intentos < 25) setTimeout(buscarLinea, 400);
  };
  buscarLinea();
});

/* ── Init ────────────────────────────────────────────── */
loadVideos();
cargarGuardadas();
