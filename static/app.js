/**
 * Diana — shell de la app.
 *
 * Secciones: tema · navegación entre vistas · estado del asistente ·
 * consultas (historial + chat) · exportación · PDFs · Inicio · Agenda ·
 * Ajustes · puente con las vistas embebidas (Sesiones, Documentos,
 * Expediente) · búsqueda y atajos.
 *
 * Todo lo que se muestra sale del backend (fuentes oficiales del Congreso o
 * datos guardados por el usuario). Si una fuente falla se dice; no se
 * completa con números de relleno.
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => DianaMD.esc(s);
  const md = (s) => DianaMD.render(s);
  const api = window.electronAPI || null;

  // La versión anterior registraba un service worker que cacheaba "/" y
  // app.js; ya no hace falta (el backend es local) y podía servir la
  // interfaz vieja. Se da de baja si quedó registrado.
  navigator.serviceWorker?.getRegistrations?.()
    .then((rs) => rs.forEach((r) => r.unregister())).catch(() => {});

  async function getJSON(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }

  // ════════════════════════════════════════════════════════════════════════
  // Tema: "sistema" | "claro" | "oscuro"
  // ════════════════════════════════════════════════════════════════════════
  const TEMA_KEY = 'diana_tema';
  const html = document.documentElement;
  const mqOscuro = window.matchMedia('(prefers-color-scheme: dark)');

  function temaGuardado() {
    try { return localStorage.getItem(TEMA_KEY) || 'sistema'; } catch { return 'sistema'; }
  }
  function esOscuro() {
    const t = temaGuardado();
    return t === 'oscuro' || (t === 'sistema' && mqOscuro.matches);
  }
  function aplicarTema(tema) {
    try { localStorage.setItem(TEMA_KEY, tema); } catch { /* sin storage */ }
    if (tema === 'claro') html.dataset.theme = 'light';
    else if (tema === 'oscuro') html.dataset.theme = 'dark';
    else delete html.dataset.theme;
    const dark = esOscuro();
    // Las vistas embebidas leen esta clave al cargar y escuchan el mensaje.
    try { localStorage.setItem('congreso_dark', dark ? '1' : '0'); } catch { /* sin storage */ }
    for (const id of ['live-iframe', 'pdfs-iframe', 'expediente-iframe']) {
      $(id)?.contentWindow?.postMessage({ type: 'theme', dark }, location.origin);
    }
    document.querySelectorAll('#tema-seg .seg-btn').forEach((b) => {
      const on = b.dataset.tema === tema;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-checked', String(on));
    });
  }
  aplicarTema(temaGuardado());
  mqOscuro.addEventListener('change', () => { if (temaGuardado() === 'sistema') aplicarTema('sistema'); });
  $('theme-toggle').addEventListener('click', () => aplicarTema(esOscuro() ? 'claro' : 'oscuro'));
  document.querySelectorAll('#tema-seg .seg-btn').forEach((b) =>
    b.addEventListener('click', () => aplicarTema(b.dataset.tema)));

  // ════════════════════════════════════════════════════════════════════════
  // Navegación entre vistas
  // ════════════════════════════════════════════════════════════════════════
  const VISTAS = ['inicio', 'diana', 'agenda', 'sesiones', 'documentos', 'expediente', 'ajustes'];
  const FRAMES = { sesiones: ['live-iframe', '/live'], documentos: ['pdfs-iframe', '/pdfs'] };
  const alEntrar = {};       // vista → función que la refresca al mostrarse
  let vistaActual = null;

  // Los iframes arrancan en about:blank: con src="" el navegador cargaba la
  // app entera dentro de cada uno (tres copias de este script corriendo).
  const necesitaCarga = (f) => !f.src || f.src === 'about:blank' || f.src === location.href;

  function irA(vista, { sinFoco = false } = {}) {
    if (!VISTAS.includes(vista)) vista = 'inicio';
    for (const v of VISTAS) {
      const el = $(`view-${v}`);
      if (!el) continue;
      const mostrar = v === vista;
      if (mostrar && el.hidden) {
        el.hidden = false;
        el.classList.remove('is-entering'); void el.offsetWidth; el.classList.add('is-entering');
      } else if (!mostrar) {
        el.hidden = true;
      }
    }
    document.querySelectorAll('.rail-item').forEach((b) => {
      const on = b.dataset.view === vista;
      b.classList.toggle('is-on', on);
      if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
    });
    $('topbar-title').textContent = $(`view-${vista}`)?.dataset.title || 'Diana';
    if (FRAMES[vista]) {
      const [id, src] = FRAMES[vista];
      if (necesitaCarga($(id))) $(id).src = src;
    }
    vistaActual = vista;
    try { localStorage.setItem('diana_vista', vista === 'expediente' ? 'diana' : vista); } catch { /* sin storage */ }
    alEntrar[vista]?.();
    if (vista === 'diana' && !sinFoco) setTimeout(() => $('msg-input').focus(), 50);
  }

  document.querySelectorAll('.rail-item').forEach((b) => b.addEventListener('click', () => irA(b.dataset.view)));
  document.addEventListener('click', (e) => {
    const g = e.target.closest('[data-goto]');
    if (g) { e.preventDefault(); irA(g.dataset.goto); }
  });

  const shell = $('shell');
  try { if (localStorage.getItem('diana_rail_mini') === '1') shell.classList.add('rail-mini'); } catch { /* */ }
  $('rail-collapse').addEventListener('click', () => {
    shell.classList.toggle('rail-mini');
    try { localStorage.setItem('diana_rail_mini', shell.classList.contains('rail-mini') ? '1' : '0'); } catch { /* */ }
  });

  // ════════════════════════════════════════════════════════════════════════
  // Estado del asistente (pie de la navegación)
  // ════════════════════════════════════════════════════════════════════════
  const NOMBRE_PROV = { gemini: 'Gemini', groq: 'Groq', openai: 'OpenAI', cerebras: 'Cerebras' };
  let estadoIA = null;

  async function refrescarEstado() {
    const el = $('rail-status');
    const txt = el.querySelector('.rail-status-text');
    try {
      estadoIA = await getJSON('/settings/ai');
      el.className = 'rail-status ' + (estadoIA.ready ? 'ok' : 'bad');
      txt.textContent = estadoIA.ready
        ? `Conectado · ${NOMBRE_PROV[estadoIA.provider] || estadoIA.provider}`
        : 'Falta la API key';
      el.title = estadoIA.ready
        ? `Chat con ${NOMBRE_PROV[estadoIA.provider] || estadoIA.provider}. Transcripción ${estadoIA.transcripcion ? 'activa' : 'desactivada (falta key de Groq)'}.`
        : 'Sin API key: Diana no puede responder. Configurala en Ajustes.';
    } catch {
      el.className = 'rail-status bad';
      txt.textContent = 'Servidor sin respuesta';
    }
    return estadoIA;
  }

  // ════════════════════════════════════════════════════════════════════════
  // Consultas: historial (archivo vía IPC, localStorage de respaldo)
  // ════════════════════════════════════════════════════════════════════════
  const STORE = 'congreso_convs';
  let convs = [];
  let activeId = null;
  let streaming = false;
  let abortCtl = null;

  function loadConvsLocal() {
    try { convs = JSON.parse(localStorage.getItem(STORE)) || []; } catch { convs = []; }
  }
  function saveConvs() {
    // localStorage tiene ~5 MB: unas pocas respuestas largas lo llenan. Si
    // revienta, se recortan las más viejas en vez de congelar el chat. La
    // copia completa va al archivo en disco (main.js save-history).
    try {
      localStorage.setItem(STORE, JSON.stringify(convs));
    } catch {
      try { localStorage.setItem(STORE, JSON.stringify(convs.slice(0, 10))); } catch { localStorage.removeItem(STORE); }
    }
    api?.saveHistory?.(convs).catch(() => {});
  }
  const getActive = () => convs.find((c) => c.id === activeId) || null;

  function newChat() {
    const id = Date.now().toString();
    convs.unshift({ id, title: 'Nueva consulta', messages: [], ts: Date.now() });
    activeId = id;
    saveConvs();
    renderConvs();
    renderMessages();
  }
  function switchConv(id) {
    if (id === activeId || streaming) return;
    activeId = id;
    renderConvs();
    renderMessages();
  }
  function deleteConv(id) {
    const c = convs.find((x) => x.id === id);
    if (!c || !confirm(`¿Borrar la consulta "${c.title}"?`)) return;
    convs = convs.filter((x) => x.id !== id);
    if (activeId === id) activeId = null;
    saveConvs();
    renderConvs();
    renderMessages();
  }
  function renameConv(id, tituloEl) {
    const conv = convs.find((c) => c.id === id);
    if (!conv) return;
    const input = document.createElement('input');
    input.className = 'conv-rename';
    input.value = conv.title;
    tituloEl.replaceWith(input);
    input.focus(); input.select();
    const commit = () => { const v = input.value.trim(); if (v) conv.title = v; saveConvs(); renderConvs(); };
    input.addEventListener('blur', commit);
    input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); input.blur(); }
      if (ev.key === 'Escape') { input.value = conv.title; input.blur(); }
    });
    input.addEventListener('click', (ev) => ev.stopPropagation());
  }

  function cuando(ts) {
    const DIA = 86400000;
    const edad = Date.now() - ts;
    if (edad < DIA && new Date(ts).getDate() === new Date().getDate())
      return new Date(ts).toLocaleTimeString('es-PE', { hour: '2-digit', minute: '2-digit' });
    if (edad < 2 * DIA) return 'Ayer';
    if (edad < 7 * DIA) return `Hace ${Math.floor(edad / DIA)} d`;
    return new Date(ts).toLocaleDateString('es-PE', { day: '2-digit', month: 'short' });
  }
  function preview(conv) {
    const first = conv.messages.find((m) => m.role === 'user');
    if (!first) return '';
    return first.content.replace(/^__RESUMEN_SEMANAL__:?/, 'Resumen semanal ').replace(/\s+/g, ' ').trim().slice(0, 70);
  }

  function renderConvs() {
    const list = $('chat-list');
    const filtro = $('convs-filtro').value.trim().toLowerCase();
    const visibles = convs.filter((c) => c.messages.length || c.id === activeId)
      .filter((c) => !filtro || c.title.toLowerCase().includes(filtro) || preview(c).toLowerCase().includes(filtro));
    list.innerHTML = '';
    if (!visibles.length) {
      list.innerHTML = `<div class="convs-vacio">${filtro ? 'Nada coincide con el filtro.' : 'Todavía no hay consultas.'}</div>`;
      return;
    }
    for (const c of visibles) {
      const el = document.createElement('div');
      el.className = 'conv' + (c.id === activeId ? ' is-on' : '');
      el.tabIndex = 0;
      el.setAttribute('role', 'button');
      el.innerHTML = `
        <div class="conv-fila"><span class="conv-titulo" title="Doble clic para renombrar">${esc(c.title)}</span>
          <span class="conv-cuando">${cuando(c.ts)}</span></div>
        <div class="conv-prev">${esc(preview(c))}</div>
        <button class="conv-del" title="Borrar consulta" aria-label="Borrar consulta">
          <svg viewBox="0 0 14 14" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 3l8 8M11 3l-8 8"/></svg>
        </button>`;
      el.addEventListener('click', (e) => { if (!e.target.closest('.conv-del')) switchConv(c.id); });
      el.addEventListener('keydown', (e) => { if (e.key === 'Enter') switchConv(c.id); });
      el.querySelector('.conv-del').addEventListener('click', (e) => { e.stopPropagation(); deleteConv(c.id); });
      el.querySelector('.conv-titulo').addEventListener('dblclick', (e) => { e.stopPropagation(); renameConv(c.id, e.target); });
      list.appendChild(el);
    }
  }
  $('convs-filtro').addEventListener('input', renderConvs);
  $('new-chat-btn').addEventListener('click', () => { if (!streaming) { newChat(); $('msg-input').focus(); } });

  // ════════════════════════════════════════════════════════════════════════
  // Chat
  // ════════════════════════════════════════════════════════════════════════
  const chatArea = $('chat-area');
  const msgInput = $('msg-input');
  const sendBtn = $('send-btn');

  // Consultas frecuentes: los mismos comandos verificados de la versión
  // anterior, agrupados por tarea. Se usan en Inicio y en Diana vacía.
  const FRECUENTES = [
    { grupo: 'Agenda', t: 'Agenda de comisiones', d: 'Próximos 2 días, Senado y Diputados',
      cmd: 'Dame la agenda de sesiones de comisiones de los próximos 2 días en SENADO y DIPUTADOS, en un cuadro con fecha, hora, lugar y link' },
    { grupo: 'Agenda', t: 'Agenda del Pleno', d: 'Dictámenes, mociones e insistencias',
      cmd: 'Dame un resumen de la estructura de la Agenda del Pleno (cuántos dictámenes, cuántas denuncias constitucionales, mociones e insistencias hay agendados)' },
    { grupo: 'Legislación', t: 'Proyectos de los últimos 15 días', d: 'Cuadro por temas',
      cmd: 'Revisa los proyectos de ley de los últimos 15 días calendario, y entrégame un cuadro resumen dividido por temas' },
    { grupo: 'Legislación', t: 'Destacados y citaciones', d: 'Congreso, Senado y Diputados',
      cmd: 'Dame el resumen y el acceso a descarga de documentos en DESTACADO y CITACIONES de DIPUTADOS, SENADO y CONGRESO' },
    { grupo: 'Comisiones', t: 'Comisiones y Comisión Permanente', d: 'Cuadros y miembros',
      cmd: 'Dame los accesos al Cuadro de Comisiones y miembros de la Comisión Permanente del SENADO y DIPUTADOS' },
    { grupo: 'Comisiones', t: 'Interpelaciones en trámite', d: 'Mociones formales registradas',
      cmd: 'Dame las mociones de interpelación a ministros registradas y su estado' },
  ];

  const SVG_USER = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="10" cy="7" r="3.2"/><path d="M3.8 16.5c0-3.2 2.8-5.3 6.2-5.3s6.2 2.1 6.2 5.3"/></svg>';
  const SVG_BOT = '<svg viewBox="0 0 32 32" fill="none"><path d="M3 12h26M16 4L3 12h26L16 4z" stroke="currentColor" stroke-width="2.4" stroke-linejoin="round"/><path d="M7 14v10M13 14v10M19 14v10M25 14v10M4 27h24" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/></svg>';

  function renderVacio() {
    const grupos = [...new Set(FRECUENTES.map((f) => f.grupo))];
    chatArea.innerHTML = `
      <div class="chat-vacio">
        <h2>¿Qué querés investigar?</h2>
        <p>Diana consulta en el momento las fuentes oficiales del Congreso, el Senado y la Cámara de Diputados, y te dice de dónde sale cada dato. También podés adjuntar un PDF o abrir un expediente.</p>
        ${grupos.map((g) => `
          <div class="grupo"><div class="grupo-t">${esc(g)}</div>
            <div class="sugerencias">${FRECUENTES.map((f, i) => f.grupo !== g ? '' : `
              <button class="sugerencia" data-frecuente="${i}"><strong>${esc(f.t)}</strong><span>${esc(f.d)}</span></button>`).join('')}
            </div></div>`).join('')}
      </div>`;
  }

  function renderMessages() {
    const conv = getActive();
    if (!conv || !conv.messages.length) { renderVacio(); return; }
    chatArea.innerHTML = '';
    for (const m of conv.messages) {
      if (m.role === 'user') appendUser(textoVisibleUsuario(m.content));
      else if (m.role === 'assistant') appendBotFinal(m.content);
    }
    scrollBottom();
  }

  // Los turnos de "contexto" (PDF cargado, resumen) guardan texto interno que
  // no tiene sentido mostrar tal cual.
  function textoVisibleUsuario(content) {
    if (content.startsWith('__RESUMEN_SEMANAL__')) {
      const sector = content.split(':')[1];
      return `Resumen semanal${sector && sector !== 'general' ? ' — ' + sector : ''}`;
    }
    const pdf = content.match(/^He cargado el documento "([^"]+)" \((\d+) páginas\)/);
    if (pdf) return `📄 ${pdf[1]} (${pdf[2]} págs.)`;
    return content;
  }

  function appendUser(text) {
    const d = document.createElement('div');
    d.className = 'msg user';
    d.innerHTML = `<div class="msg-av">${SVG_USER}</div><div class="msg-body"><div class="msg-quien">Vos</div><div class="msg-texto">${esc(text)}</div></div>`;
    chatArea.appendChild(d);
    scrollBottom();
  }
  function appendBotPendiente() {
    const d = document.createElement('div');
    d.className = 'msg bot';
    d.innerHTML = `<div class="msg-av">${SVG_BOT}</div><div class="msg-body"><div class="msg-quien">Diana</div>
      <div class="md msg-md"><span class="msg-estado"><span class="d-spinner"></span>Pensando…</span></div></div>`;
    chatArea.appendChild(d);
    scrollBottom();
    return d;
  }
  function appendBotFinal(texto) {
    const d = appendBotPendiente();
    pintar(d, texto);
    terminarMensaje(d, texto);
    return d;
  }
  function pintar(msgEl, texto) { msgEl.querySelector('.msg-md').innerHTML = md(texto); }
  function pintarEstado(msgEl, estado) {
    msgEl.querySelector('.msg-md').innerHTML = `<span class="msg-estado"><span class="d-spinner"></span>${esc(estado)}</span>`;
  }
  const scrollBottom = () => { chatArea.scrollTop = chatArea.scrollHeight; };

  function setStreaming(on) {
    streaming = on;
    if (on) {
      sendBtn.disabled = false;
      sendBtn.classList.add('is-stop');
      sendBtn.title = 'Detener la respuesta';
      sendBtn.innerHTML = '<svg viewBox="0 0 20 20" fill="currentColor"><rect x="5.5" y="5.5" width="9" height="9" rx="1.6"/></svg>';
    } else {
      abortCtl = null;
      sendBtn.classList.remove('is-stop');
      sendBtn.title = 'Enviar';
      sendBtn.innerHTML = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M10 16V4M4.8 9.2L10 4l5.2 5.2"/></svg>';
      sendBtn.disabled = !msgInput.value.trim();
    }
    document.querySelectorAll('[data-frecuente], .chip').forEach((b) => { b.disabled = on; });
  }

  async function send(textOverride, sector) {
    const text = (textOverride ?? msgInput.value).trim();
    if (!text || streaming) return;
    if (vistaActual !== 'diana') irA('diana', { sinFoco: true });
    const esResumen = text === '__RESUMEN_SEMANAL__';
    if (!getActive()) newChat();
    const conv = getActive();
    if (!conv.messages.length) {
      conv.title = esResumen
        ? 'Resumen semanal' + (sector && sector !== 'general' ? ' — ' + sector : '')
        : text.slice(0, 48) + (text.length > 48 ? '…' : '');
    }
    if (!conv.messages.length) chatArea.innerHTML = '';
    const contenido = esResumen ? `__RESUMEN_SEMANAL__${sector ? ':' + sector : ''}` : text;
    conv.messages.push({ role: 'user', content: contenido });
    conv.ts = Date.now();
    saveConvs();
    renderConvs();
    appendUser(textoVisibleUsuario(contenido));
    msgInput.value = '';
    autoAltura(msgInput);

    abortCtl = new AbortController();
    setStreaming(true);
    const msgEl = appendBotPendiente();
    let full = '';
    try {
      await DianaSSE.post('/chat', { messages: conv.messages }, {
        signal: abortCtl.signal,
        onEvent(obj) {
          if (obj.error) { full += `${full ? '\n\n' : ''}**No se pudo completar:** ${obj.error}`; pintar(msgEl, full); return false; }
          if (obj.status && !full) pintarEstado(msgEl, obj.status);
          if (obj.text) { full += obj.text; pintar(msgEl, full); scrollBottom(); }
        },
      });
      if (!full) { full = '_Sin respuesta del asistente. Probá de nuevo._'; pintar(msgEl, full); }
    } catch (err) {
      full = err.name === 'AbortError'
        ? (full ? full + '\n\n_(respuesta detenida)_' : '_Respuesta detenida._')
        : `${full}${full ? '\n\n' : ''}**Error de conexión con el servidor local:** ${err.message}`;
      pintar(msgEl, full);
    } finally {
      // Siempre en el finally: si `streaming` queda en true, el chat deja de
      // aceptar mensajes sin ningún error visible.
      try {
        conv.messages.push({ role: 'assistant', content: full });
        saveConvs();
        terminarMensaje(msgEl, full);
      } catch (e) { console.error('No se pudo guardar la respuesta:', e); }
      setStreaming(false);
      scrollBottom();
      if (vistaActual === 'diana') msgInput.focus();
    }
  }

  sendBtn.addEventListener('click', () => (streaming ? abortCtl?.abort() : send()));
  function autoAltura(t) { t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 180) + 'px'; }
  msgInput.addEventListener('input', () => { autoAltura(msgInput); if (!streaming) sendBtn.disabled = !msgInput.value.trim(); });
  msgInput.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-frecuente]');
    if (b && !streaming) send(FRECUENTES[+b.dataset.frecuente].cmd);
  });

  // ── Acciones de cada respuesta ────────────────────────
  function terminarMensaje(msgEl, texto) {
    const cont = msgEl.querySelector('.msg-md');
    envolverTablas(cont);
    const acciones = document.createElement('div');
    acciones.className = 'msg-acciones';
    const boton = (label, fn) => {
      const b = document.createElement('button');
      b.className = 'd-btn d-btn--sm';
      b.textContent = label;
      b.addEventListener('click', () => fn(b));
      acciones.appendChild(b);
    };
    boton('Copiar', (b) => copiar(texto, b));
    if (/\n\|?\s*:?-{3,}/.test(texto) || texto.length > 600) {
      boton('Exportar Word', (b) => exportWord(texto, b));
      boton('Exportar PDF', (b) => exportPdf(texto, b));
    }
    msgEl.querySelector('.msg-body').appendChild(acciones);
  }

  function envolverTablas(el) {
    el.querySelectorAll('table').forEach((table) => {
      if (table.parentElement.classList.contains('table-wrap')) return;
      const wrap = document.createElement('div');
      wrap.className = 'table-wrap';
      table.parentNode.insertBefore(wrap, table);
      wrap.appendChild(table);
      const btn = document.createElement('button');
      btn.className = 'd-btn d-btn--sm copy-btn';
      btn.textContent = 'Copiar tabla';
      btn.addEventListener('click', () => copiarTabla(table, btn));
      wrap.appendChild(btn);
    });
  }

  function marcarHecho(btn, txt = 'Copiado') {
    const orig = btn.textContent;
    btn.textContent = `✓ ${txt}`;
    setTimeout(() => { btn.textContent = orig; }, 1800);
  }
  function copiar(texto, btn) {
    navigator.clipboard.writeText(texto).then(() => marcarHecho(btn)).catch(() => fallbackCopy(texto, btn));
  }
  function fallbackCopy(texto, btn) {
    const ta = document.createElement('textarea');
    ta.value = texto; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); ta.remove();
    if (btn) marcarHecho(btn);
  }
  function copiarTabla(table, btn) {
    // HTML (Word la pega como tabla con formato) + TSV de respaldo.
    const htmlTabla = `<html><body><style>table{border-collapse:collapse;font-family:Calibri,sans-serif;font-size:11pt}
      th{background:#1a1a1a;color:#fff;padding:6px 12px;border:1px solid #333}td{padding:5px 12px;border:1px solid #ccc}</style>${table.outerHTML}</body></html>`;
    const tsv = [...table.querySelectorAll('tr')]
      .map((r) => [...r.querySelectorAll('th,td')].map((c) => c.textContent.trim()).join('\t')).join('\n');
    if (window.ClipboardItem) {
      navigator.clipboard.write([new ClipboardItem({
        'text/html': new Blob([htmlTabla], { type: 'text/html' }),
        'text/plain': new Blob([tsv], { type: 'text/plain' }),
      })]).then(() => marcarHecho(btn)).catch(() => fallbackCopy(tsv, btn));
    } else fallbackCopy(tsv, btn);
  }

  // ════════════════════════════════════════════════════════════════════════
  // Exportación
  // ════════════════════════════════════════════════════════════════════════
  function htmlParaImprimir(texto, titulo = 'Informe — Diana') {
    const fecha = new Date().toLocaleDateString('es-PE', { day: 'numeric', month: 'long', year: 'numeric' });
    return `<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"><title>${esc(titulo)}</title><style>
      *{box-sizing:border-box;margin:0;padding:0}
      body{font-family:Georgia,serif;font-size:11.5pt;color:#111;padding:40px 56px;line-height:1.65;max-width:900px;margin:0 auto}
      h1{font-size:18pt;margin-bottom:6px} h2{font-size:13pt;margin:22px 0 8px;border-bottom:1.5px solid #111;padding-bottom:4px}
      h3{font-size:12pt;margin:16px 0 6px} p{margin-bottom:9px} ul,ol{padding-left:20px;margin-bottom:9px} li{margin-bottom:3px}
      table{border-collapse:collapse;width:100%;margin:10px 0;font-size:9.5pt} th{background:#111;color:#fff;padding:6px 9px;text-align:left}
      td{border:1px solid #ccc;padding:6px 9px;vertical-align:top} tr:nth-child(even) td{background:#f6f6f6}
      a{color:#8c1227} hr{border:none;border-top:1px solid #ccc;margin:18px 0}
      .ftr{margin-top:36px;border-top:1px solid #ccc;padding-top:10px;font-size:8.5pt;color:#666}
      @media print{body{padding:16px 24px}}
    </style></head><body>${md(texto)}
    <div class="ftr">Generado por Diana — fuentes oficiales del Congreso de la República del Perú · ${fecha}</div></body></html>`;
  }
  async function conBoton(btn, tarea) {
    const orig = btn?.textContent;
    if (btn) { btn.disabled = true; btn.textContent = 'Generando…'; }
    try { await tarea(); if (btn) marcarHecho(btn, 'Listo'); }
    catch (e) { alert('No se pudo exportar: ' + e.message); }
    finally { if (btn) { btn.disabled = false; setTimeout(() => { btn.textContent = orig; }, 1800); } }
  }
  function exportPdf(texto, btn) {
    return conBoton(btn, async () => {
      const h = htmlParaImprimir(texto);
      if (api) return api.exportPDF(h);
      const w = window.open('', '_blank');
      w.document.write(h); w.document.close(); setTimeout(() => w.print(), 400);
    });
  }
  function exportWord(texto, btn, nombre) {
    return conBoton(btn, async () => {
      if (api) return api.exportWord(texto);
      const r = await fetch('/export/docx', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content: texto }) });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const url = URL.createObjectURL(await r.blob());
      const a = Object.assign(document.createElement('a'), { href: url, download: `${nombre || 'Diana-' + new Date().toISOString().slice(0, 10)}.docx` });
      a.click(); URL.revokeObjectURL(url);
    });
  }

  // ════════════════════════════════════════════════════════════════════════
  // PDFs (adjuntar, arrastrar, abrir desde Documentos)
  // ════════════════════════════════════════════════════════════════════════
  async function cargarPdf(origen, titulo) {
    if (streaming) return;
    irA('diana', { sinFoco: true });
    if (!getActive() || getActive().messages.length) newChat();
    const conv = getActive();
    conv.title = '📄 ' + titulo.slice(0, 44);
    chatArea.innerHTML = '';
    appendUser(`📄 ${titulo}`);
    const msgEl = appendBotPendiente();
    pintarEstado(msgEl, 'Leyendo el documento…');
    setStreaming(true);
    try {
      let data;
      if (typeof origen === 'string') {
        data = await getJSON('/load-pdf-url', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: origen }) });
      } else {
        const form = new FormData();
        form.append('file', origen);
        const r = await fetch('/upload-pdf', { method: 'POST', body: form });
        data = await r.json();
      }
      if (!data.ok) throw new Error(data.error || 'No se pudo leer el PDF');
      conv.messages.push({ role: 'user', content: `He cargado el documento "${titulo}" (${data.pages} páginas).\n\nContenido:\n${data.text}\n\n¿Qué quieres analizar?` });
      const aviso = /Páginas sin texto extraíble/.test(data.text)
        ? '\n\n_Ojo: algunas páginas no tienen texto extraíble (probablemente escaneadas) y su contenido no está disponible._' : '';
      const recorte = /Texto recortado/.test(data.text) ? '\n\n_El documento es largo: se leyó solo una parte (te digo hasta qué página si preguntás)._' : '';
      const msg = `Listo, leí **${esc(titulo.slice(0, 80))}** (${data.pages} págs.). Preguntame lo que necesites: cito la página de donde sale cada dato.${aviso}${recorte}`;
      conv.messages.push({ role: 'assistant', content: msg });
      saveConvs(); renderConvs();
      pintar(msgEl, msg);
      terminarMensaje(msgEl, msg);
    } catch (err) {
      pintar(msgEl, `**No se pudo cargar el PDF:** ${err.message}`);
    } finally {
      setStreaming(false);
      msgInput.focus();
    }
  }
  $('pdf-input').addEventListener('change', (e) => {
    const f = e.target.files[0]; e.target.value = '';
    if (f) cargarPdf(f, f.name);
  });
  const dropHint = $('drop-hint');
  document.addEventListener('dragover', (e) => { if ([...(e.dataTransfer?.types || [])].includes('Files')) { e.preventDefault(); dropHint.hidden = false; } });
  document.addEventListener('dragleave', (e) => { if (!e.relatedTarget) dropHint.hidden = true; });
  document.addEventListener('drop', (e) => {
    e.preventDefault(); dropHint.hidden = true;
    const f = [...(e.dataTransfer?.files || [])].find((x) => x.name.toLowerCase().endsWith('.pdf'));
    if (f) cargarPdf(f, f.name);
  });

  // ════════════════════════════════════════════════════════════════════════
  // Inicio
  // ════════════════════════════════════════════════════════════════════════
  const NOMBRE_KEY = 'diana_nombre';
  function nombreUsuario() {
    try {
      const v = localStorage.getItem(NOMBRE_KEY);
      // La versión anterior saludaba siempre a "Julio César": se conserva
      // como valor inicial, editable desde Ajustes.
      return v === null ? 'Julio César' : v;
    } catch { return ''; }
  }
  function saludo() {
    const h = new Date().getHours();
    const s = h < 12 ? 'Buenos días' : h < 19 ? 'Buenas tardes' : 'Buenas noches';
    const n = nombreUsuario().trim();
    return n ? `${s}, ${n}` : s;
  }

  function filaSesion(s) {
    const meta = [s.camara, s.lugar].filter(Boolean).map(esc).join(' · ');
    return `<button class="lista-item" data-preguntar="${esc(`¿Qué se va a tratar en "${s.tema}" (${s.fecha} ${s.hora})?`)}">
      <span class="lista-cuando">${esc(s.dia)}<small>${esc(s.hora)}</small></span>
      <span class="lista-texto"><span class="t">${esc(s.tema)}</span>${meta ? `<span class="m">${meta}</span>` : ''}</span><span></span></button>`;
  }
  function filaProyecto(p) {
    return `<button class="lista-item" data-expediente="${esc(p.numero)}">
      <span class="lista-num">${esc(p.numero)}</span>
      <span class="lista-texto"><span class="t">${esc(p.titulo.charAt(0) + p.titulo.slice(1).toLowerCase())}</span>
        <span class="m">${esc([p.camara, p.estado, p.fecha_presentacion].filter(Boolean).join(' · '))}</span></span><span></span></button>`;
  }
  const vacio = (titulo, texto) => `<div class="d-empty"><strong>${esc(titulo)}</strong>${texto ? `<span>${esc(texto)}</span>` : ''}</div>`;

  async function cargarInicio() {
    $('inicio-saludo').textContent = saludo();
    const f = new Date().toLocaleDateString('es-PE', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
    $('inicio-fecha').textContent = f.charAt(0).toUpperCase() + f.slice(1);
    $('inicio-frecuentes').innerHTML = FRECUENTES.map((f, i) => `<button class="chip" data-frecuente="${i}">${esc(f.t)}</button>`).join('');

    // Consultas recientes (locales, al instante)
    const recientes = convs.filter((c) => c.messages.length).slice(0, 5);
    $('inicio-consultas').innerHTML = recientes.length
      ? recientes.map((c) => `<button class="lista-item" data-conv="${esc(c.id)}"><span class="lista-cuando">${esc(cuando(c.ts))}</span>
          <span class="lista-texto"><span class="t">${esc(c.title)}</span></span><span></span></button>`).join('')
      : vacio('Sin consultas todavía', 'Lo que le preguntes a Diana queda acá.');

    // Transcripciones guardadas
    getJSON('/transcripts').then(({ sesiones }) => {
      $('inicio-transcripciones').innerHTML = sesiones.length
        ? sesiones.slice(0, 5).map((s) => `<button class="lista-item" data-goto="sesiones"><span class="lista-cuando">hasta<small>${esc(s.hasta)}</small></span>
            <span class="lista-texto"><span class="t">${esc(s.titulo || s.video_id)}</span>
            <span class="m">${s.tiene_informe ? 'con informe' : 'sin informe'}${s.en_vivo ? ' · capturada en vivo' : ''}</span></span><span></span></button>`).join('')
        : vacio('Ninguna sesión transcrita', 'Abrí Sesiones para transcribir una del canal del Congreso.');
    }).catch(() => { $('inicio-transcripciones').innerHTML = vacio('No se pudieron leer las transcripciones guardadas'); });

    // Datos del Congreso (agenda + proyectos)
    try {
      const d = await getJSON('/inicio');
      $('inicio-agenda').innerHTML = d.agenda.length
        ? d.agenda.map(filaSesion).join('')
        : vacio('Sin actividades publicadas', 'La agenda oficial no tiene eventos próximos en los siguientes días.');
      if (d.proyectos) {
        $('inicio-proyectos-sub').textContent = `${d.proyectos.total} en los últimos ${d.proyectos.dias} días`;
        $('inicio-proyectos').innerHTML = d.proyectos.items.length
          ? d.proyectos.items.map(filaProyecto).join('')
          : vacio('Ningún proyecto nuevo', `SPLEY no registra proyectos en los últimos ${d.proyectos.dias} días.`);
      } else {
        $('inicio-proyectos').innerHTML = vacio('SPLEY no respondió', 'No se pudo consultar ahora. No es que no haya proyectos.');
      }
      const fallas = $('inicio-fallas');
      fallas.hidden = !d.fallas.length;
      fallas.innerHTML = d.fallas.length ? `<div class="d-alert">No se pudo consultar: ${d.fallas.map(esc).join(', ')}. Lo demás está al día.</div>` : '';
      $('inicio-pie').textContent = `Fuentes oficiales del Congreso, el Senado y la Cámara de Diputados · consultado ${d.actualizado}`;
    } catch (e) {
      const msg = vacio('No se pudo consultar', `El servidor local no respondió (${e.message}).`);
      $('inicio-agenda').innerHTML = msg;
      $('inicio-proyectos').innerHTML = msg;
    }
  }
  alEntrar.inicio = cargarInicio;

  const inicioAsk = $('inicio-ask-input');
  inicioAsk.addEventListener('input', () => autoAltura(inicioAsk));
  inicioAsk.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('inicio-ask').requestSubmit(); } });
  $('inicio-ask').addEventListener('submit', (e) => {
    e.preventDefault();
    const q = inicioAsk.value.trim();
    if (!q || streaming) return;
    inicioAsk.value = ''; autoAltura(inicioAsk);
    newChat();
    send(q);
  });
  document.addEventListener('click', (e) => {
    const c = e.target.closest('[data-conv]');
    if (c) { irA('diana'); switchConv(c.dataset.conv); return; }
    const p = e.target.closest('[data-preguntar]');
    if (p && !streaming) { newChat(); send(p.dataset.preguntar); return; }
    const x = e.target.closest('[data-expediente]');
    if (x) abrirExpediente(x.dataset.expediente);
  });

  // ── Resumen semanal ──────────────────────────────────
  const resumenModal = $('resumen-modal');
  $('btn-resumen-semanal').addEventListener('click', () => { if (!streaming) { resumenModal.hidden = false; resumenModal.querySelector('.sector').focus(); } });
  $('resumen-cancel').addEventListener('click', () => { resumenModal.hidden = true; });
  resumenModal.addEventListener('click', (e) => { if (e.target === resumenModal) resumenModal.hidden = true; });
  resumenModal.querySelectorAll('.sector').forEach((b) => b.addEventListener('click', () => {
    resumenModal.hidden = true;
    newChat();
    send('__RESUMEN_SEMANAL__', b.dataset.sector);
  }));

  // ════════════════════════════════════════════════════════════════════════
  // Agenda
  // ════════════════════════════════════════════════════════════════════════
  let agendaDatos = null;
  let agendaCamara = '';

  async function cargarAgenda(forzar = false) {
    const lista = $('agenda-lista');
    if (agendaDatos && !forzar) { pintarAgenda(); return; }
    lista.innerHTML = `<div class="d-card"><div class="lista-cargando"><span class="d-skeleton" style="width:60%"></span><span class="d-skeleton" style="width:75%"></span><span class="d-skeleton" style="width:50%"></span></div></div>`;
    $('agenda-fuente').textContent = 'Consultando la agenda oficial…';
    try {
      const d = await getJSON('/agenda?dias=14');
      if (d.error) throw new Error(d.error);
      agendaDatos = d;
      pintarAgenda();
    } catch (e) {
      agendaDatos = null;
      $('agenda-fuente').textContent = '';
      lista.innerHTML = `<div class="d-alert d-alert--danger">No se pudo consultar la agenda oficial: ${esc(e.message)}. Probá con "Actualizar" en un rato.</div>`;
    }
  }

  function pintarAgenda() {
    const lista = $('agenda-lista');
    const filtro = $('agenda-filtro').value.trim().toLowerCase();
    const sesiones = agendaDatos.sesiones.filter((s) =>
      (!agendaCamara || (s.camara || '').toLowerCase().includes(agendaCamara.toLowerCase())) &&
      (!filtro || [s.tema, s.lugar, s.organiza].join(' ').toLowerCase().includes(filtro)));
    $('agenda-fuente').textContent = `Fuente: ${agendaDatos.fuente} · consultado ${agendaDatos.actualizado} · ${sesiones.length} de ${agendaDatos.sesiones.length} actividades. Solo se muestran las que tienen fecha y hora publicadas.`;
    if (!sesiones.length) {
      lista.innerHTML = `<div class="d-card">${vacio(agendaDatos.sesiones.length ? 'Nada coincide con el filtro' : 'Sin actividades publicadas',
        agendaDatos.sesiones.length ? 'Probá con otra cámara o quitá el texto del filtro.' : 'La agenda oficial no tiene eventos en los próximos 14 días.')}</div>`;
      return;
    }
    const porDia = new Map();
    for (const s of sesiones) {
      const k = s.fecha;
      if (!porDia.has(k)) porDia.set(k, []);
      porDia.get(k).push(s);
    }
    lista.innerHTML = [...porDia.entries()].map(([fecha, ss]) => {
      const dt = new Date(ss[0].iso);
      const largo = dt.toLocaleDateString('es-PE', { weekday: 'long', day: 'numeric', month: 'long' });
      return `<div class="agenda-dia"><h3 class="agenda-dia-t">${esc(ss[0].dia)} <small>${esc(largo)}</small></h3>
        <div class="d-card">${ss.map((s) => {
          // Hora ya pasada: se muestra atenuada, sin afirmar que se realizó
          // (la fuente no lo dice).
          const pasada = new Date(s.iso) < new Date();
          return `
          <div class="agenda-evento${pasada ? ' is-pasada' : ''}">
            <div class="agenda-hora">${esc(s.hora)}${pasada ? '<small>hora pasada</small>' : ''}</div>
            <div>
              <div class="agenda-tema">${esc(s.tema)}</div>
              <div class="agenda-meta">${s.camara ? `<span class="d-badge">${esc(s.camara)}</span>` : ''}
                ${s.lugar ? `<span>${esc(s.lugar)}</span>` : ''}${s.organiza ? `<span>${esc(s.organiza)}</span>` : ''}</div>
              <div class="agenda-acciones"><button class="d-btn d-btn--sm" data-preguntar="${esc(`¿Qué se va a tratar en "${s.tema}" el ${s.fecha} a las ${s.hora}? Buscá la agenda y los proyectos relacionados.`)}">Preguntar a Diana</button></div>
            </div>
          </div>`; }).join('')}</div></div>`;
    }).join('');
  }
  alEntrar.agenda = () => cargarAgenda();
  $('agenda-recargar').addEventListener('click', () => cargarAgenda(true));
  $('agenda-filtro').addEventListener('input', () => agendaDatos && pintarAgenda());
  document.querySelectorAll('#agenda-camaras .seg-btn').forEach((b) => b.addEventListener('click', () => {
    document.querySelectorAll('#agenda-camaras .seg-btn').forEach((x) => x.classList.toggle('is-on', x === b));
    agendaCamara = b.dataset.camara;
    if (agendaDatos) pintarAgenda();
  }));

  // ════════════════════════════════════════════════════════════════════════
  // Ajustes
  // ════════════════════════════════════════════════════════════════════════
  const aiProvider = $('ai-provider'), aiKey = $('ai-key'), aiGroqKey = $('ai-groq-key');
  const aiStatus = $('ai-status'), aiMsg = $('ai-msg'), aiSave = $('ai-save'), aiClear = $('ai-clear');
  const AI_KEY_URLS = {
    gemini: 'https://aistudio.google.com/app/apikey', groq: 'https://console.groq.com/keys',
    openai: 'https://platform.openai.com/api-keys', cerebras: 'https://cloud.cerebras.ai',
  };
  function aiSetMsg(text, kind) { aiMsg.textContent = text || ''; aiMsg.className = 'ai-msg' + (kind ? ' ' + kind : ''); }

  async function aiRefresh() {
    const server = await refrescarEstado();
    const local = api?.aiSettingsGet ? await api.aiSettingsGet() : null;
    if (local?.provider) aiProvider.value = local.provider;
    else if (server?.provider) aiProvider.value = server.provider;
    aiClear.hidden = !local?.hasKey;
    aiKey.placeholder = local?.hasKey ? `Guardada (termina en …${local.keyHint}). Pegá otra para cambiarla` : 'Pegá tu API key';
    aiGroqKey.placeholder = local?.hasGroqKey ? 'Guardada. Pegá otra para cambiarla' : 'Solo para la transcripción de sesiones';
    if (!server) {
      aiStatus.innerHTML = '<div class="d-alert d-alert--danger">No se pudo consultar el servidor local.</div>';
    } else if (!server.ready) {
      aiStatus.innerHTML = '<div class="d-alert d-alert--danger">Sin API key: Diana no puede responder hasta que pegues una.</div>';
    } else {
      aiStatus.innerHTML = `<div class="d-alert d-alert--ok">Funcionando con ${esc(NOMBRE_PROV[server.provider] || server.provider)}${local?.hasKey ? ` (tu key …${esc(local.keyHint)})` : ''}.`
        + (server.transcripcion ? ' Transcripción de sesiones activa.' : ' La transcripción está desactivada: falta la key de Groq.') + '</div>';
    }
    return server;
  }

  async function aiTest(provider, key) {
    return getJSON('/settings/ai/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider, api_key: key }) });
  }
  async function saveAiSettings() {
    const provider = aiProvider.value;
    const key = aiKey.value.trim();
    const groqKey = aiGroqKey.value.trim();
    const local = await api.aiSettingsGet();
    if (!key && local.hasKey && local.provider !== provider) { aiSetMsg(`Pegá una API key de ${NOMBRE_PROV[provider]}.`, 'bad'); return; }
    if (!key && !groqKey) { aiSetMsg('Pegá una API key.', 'bad'); return; }
    aiSave.disabled = true;
    try {
      if (key) {
        aiSetMsg('Probando la key…');
        const t = await aiTest(provider, key);
        if (!t.ok) { aiSetMsg(t.error || 'La key no funcionó.', 'bad'); return; }
      }
      if (groqKey) {
        aiSetMsg('Probando la key de Groq…');
        const t = await aiTest('groq', groqKey);
        if (!t.ok) { aiSetMsg('Key de Groq: ' + (t.error || 'no funcionó.'), 'bad'); return; }
      }
      aiSetMsg('Guardando y reiniciando el asistente…');
      const res = await api.aiSettingsSave({ provider, apiKey: key, groqKey });
      if (!res.ok) { aiSetMsg(res.error || 'No se pudo guardar.', 'bad'); return; }
      aiKey.value = ''; aiGroqKey.value = '';
      await aiRefresh();
      aiSetMsg('Listo. Diana ya puede responder.', 'ok');
    } catch (e) {
      aiSetMsg('Error: ' + e.message, 'bad');
    } finally {
      aiSave.disabled = false;
    }
  }

  if (api?.aiSettingsGet) {
    aiSave.addEventListener('click', saveAiSettings);
    aiKey.addEventListener('keydown', (e) => { if (e.key === 'Enter') saveAiSettings(); });
    $('ai-key-help').addEventListener('click', () => api.openExternal(AI_KEY_URLS[aiProvider.value]));
    aiClear.addEventListener('click', async () => {
      if (!confirm('¿Borrar tus API keys de esta computadora?')) return;
      aiClear.disabled = true;
      aiSetMsg('Borrando…');
      await api.aiSettingsClear();
      aiClear.disabled = false;
      const s = await aiRefresh();
      aiSetMsg(s?.ready ? 'Listo.' : 'Keys borradas. Pegá una para volver a usar el chat.', s?.ready ? 'ok' : 'bad');
    });
  } else {
    // Fuera de Electron (navegador, pruebas) las keys no se pueden guardar.
    [aiProvider, aiKey, aiGroqKey, aiSave].forEach((el) => { el.disabled = true; });
    $('ai-solo-electron').hidden = false;
  }

  const ajNombre = $('aj-nombre');
  ajNombre.value = nombreUsuario();
  ajNombre.addEventListener('input', () => { try { localStorage.setItem(NOMBRE_KEY, ajNombre.value.trim()); } catch { /* */ } });

  async function cargarDatosGuardados() {
    const n = convs.filter((c) => c.messages.length).length;
    $('datos-consultas').textContent = `${n} consulta${n === 1 ? '' : 's'}`;
    try {
      const { sesiones } = await getJSON('/transcripts');
      $('datos-transcripciones').textContent = `${sesiones.length} sesión${sesiones.length === 1 ? '' : 'es'}`;
    } catch { $('datos-transcripciones').textContent = 'no disponible'; }
  }
  $('datos-borrar-consultas').addEventListener('click', () => {
    if (!convs.length || !confirm('¿Borrar todo el historial de consultas? No se puede deshacer.')) return;
    convs = []; activeId = null;
    saveConvs(); renderConvs(); renderMessages(); cargarDatosGuardados();
  });

  async function cargarDiagnostico() {
    const cuerpo = $('diag-cuerpo');
    cuerpo.innerHTML = '<dt>Comprobando…</dt><dd><span class="d-spinner"></span></dd>';
    try {
      const d = await getJSON('/diagnostico');
      const fila = (nombre, ok, texto) => `<dt>${esc(nombre)}</dt><dd><span class="d-badge d-badge--${ok ? 'ok' : 'danger'}"><span class="d-dot"></span>${ok ? 'OK' : 'Falla'}</span><span>${esc(texto)}</span></dd>`;
      cuerpo.innerHTML = [
        fila('Servidor local', true, 'respondiendo'),
        fila('Proveedor de IA', d.ia.configurada, d.ia.configurada ? `${NOMBRE_PROV[d.ia.proveedor] || d.ia.proveedor} con key configurada` : 'falta la API key'),
        fila('Transcripción (Groq)', d.transcripcion, d.transcripcion ? 'key configurada' : 'falta la key de Groq'),
        ...d.fuentes.map((f) => fila(f.nombre, f.ok, `${f.detalle} · ${f.ms} ms`)),
        fila('Base de datos local', d.base_de_datos.ok, d.base_de_datos.ok ? `${d.base_de_datos.sesiones} sesiones · ${d.base_de_datos.ruta}` : d.base_de_datos.detalle),
        `<dt>Registro técnico</dt><dd><code>${esc(d.log)}</code></dd>`,
        `<dt>Comprobado</dt><dd>${esc(d.comprobado)}</dd>`,
      ].join('');
    } catch (e) {
      cuerpo.innerHTML = `<dt>Servidor local</dt><dd><span class="d-badge d-badge--danger">Falla</span> ${esc(e.message)}</dd>`;
    }
  }
  $('diag-recargar').addEventListener('click', cargarDiagnostico);
  alEntrar.ajustes = () => { aiRefresh(); cargarDatosGuardados(); cargarDiagnostico(); };

  // ════════════════════════════════════════════════════════════════════════
  // Expediente y vistas embebidas
  // ════════════════════════════════════════════════════════════════════════
  function abrirExpediente(numero) {
    const f = $('expediente-iframe');
    if (f.dataset.numero !== numero) {
      f.dataset.numero = numero;
      f.src = `/expediente-view?numero=${encodeURIComponent(numero)}`;
    }
    $('view-expediente').dataset.title = `Expediente ${numero}`;
    irA('expediente');
  }

  // Los links a proyectos que escribe Diana apuntan al portal SPLEY: se abren
  // en la ficha interna. El número sale del TEXTO del link (la URL lleva el
  // id interno de SPLEY). Cualquier otro link va al navegador.
  const RE_NUM_PROYECTO = /^\d{1,6}[-/]\d{4}(?:-\d{4})?-[A-Z]{1,3}$/i;
  document.addEventListener('click', (e) => {
    const a = e.target.closest('a[href]');
    if (!a) return;
    if (a.href.includes('spley-portal') && RE_NUM_PROYECTO.test((a.textContent || '').trim())) {
      e.preventDefault();
      abrirExpediente(a.textContent.trim());
      return;
    }
    if (/^https?:/.test(a.href) && !a.href.startsWith(location.origin)) {
      e.preventDefault();
      if (api) api.openExternal(a.href); else window.open(a.href, '_blank', 'noopener');
    }
  });

  window.addEventListener('message', (e) => {
    if (e.origin !== location.origin) return;   // solo mensajes de la propia app
    const d = e.data;
    if (d === 'close-live' || d === 'close-expediente') { irA('diana'); return; }
    if (d?.type === 'load-pdf') { cargarPdf(d.url, d.titulo); return; }
    if (d?.type === 'query-proyecto') {
      newChat();
      send(`Dame información sobre el proyecto de ley ${d.numero}: estado actual, autores, y de qué trata.`);
      return;
    }
    if (d?.type === 'open-external') { api?.openExternal(d.url); return; }
    // Puente de exportación: los iframes no tienen electronAPI.
    const responder = (ok, err) => e.source.postMessage(ok ? { type: 'export-done', reqId: d.reqId } : { type: 'export-error', reqId: d.reqId, message: String(err) }, location.origin);
    if (d?.type === 'export-pdf') {
      if (api) api.exportPDF(d.html).then(() => responder(true)).catch((err) => responder(false, err));
      else { const w = window.open('', '_blank'); if (w) { w.document.write(d.html); w.document.close(); setTimeout(() => w.print(), 400); } responder(true); }
    }
    if (d?.type === 'export-word') {
      exportWord(d.content, null, d.filename).then(() => responder(true)).catch((err) => responder(false, err));
    }
  });

  // ════════════════════════════════════════════════════════════════════════
  // Búsqueda global y atajos
  // ════════════════════════════════════════════════════════════════════════
  $('global-search').addEventListener('submit', (e) => {
    e.preventDefault();
    const inp = $('global-search-input');
    const q = inp.value.trim();
    if (!q || streaming) return;
    inp.value = ''; inp.blur();
    // Un número de proyecto abre directo su expediente.
    if (RE_NUM_PROYECTO.test(q)) { abrirExpediente(q.toUpperCase()); return; }
    newChat();
    send(`Buscá en las fuentes oficiales del Congreso: ${q}`);
  });
  document.addEventListener('keydown', (e) => {
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key.toLowerCase() === 'k') { e.preventDefault(); $('global-search-input').focus(); }
    if (mod && e.key.toLowerCase() === 'n') { e.preventDefault(); if (!streaming) { newChat(); irA('diana'); } }
    if (mod && /^[1-6]$/.test(e.key)) { e.preventDefault(); irA(['inicio', 'diana', 'agenda', 'sesiones', 'documentos', 'ajustes'][+e.key - 1]); }
    if (e.key === 'Escape' && !resumenModal.hidden) resumenModal.hidden = true;
  });

  // ════════════════════════════════════════════════════════════════════════
  // Arranque
  // ════════════════════════════════════════════════════════════════════════
  async function init() {
    if (api?.loadHistory) {
      try {
        const fromFile = await api.loadHistory();
        const valid = Array.isArray(fromFile) && fromFile.length > 0 &&
          fromFile.every((c) => c && typeof c.id === 'string' && Array.isArray(c.messages));
        if (valid) { convs = fromFile; try { localStorage.setItem(STORE, JSON.stringify(convs)); } catch { /* */ } }
        else loadConvsLocal();
      } catch { loadConvsLocal(); }
    } else loadConvsLocal();
    convs.sort((a, b) => (b.ts || 0) - (a.ts || 0));
    renderConvs();
    renderMessages();

    const estado = await refrescarEstado();
    // Sin key, lo primero es configurarla: Diana no puede responder.
    if (estado && !estado.ready) { irA('ajustes'); return; }
    let ultima = 'inicio';
    try { ultima = localStorage.getItem('diana_vista') || 'inicio'; } catch { /* */ }
    irA(ultima === 'ajustes' ? 'inicio' : ultima, { sinFoco: true });
  }
  init();
})();
