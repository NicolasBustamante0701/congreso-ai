/**
 * Ficha del expediente + chat del proyecto.
 *
 * Vive en un iframe dentro de la ventana principal (ver #view-expediente en
 * index.html), así que todo lo que necesita del host — abrir un PDF en el
 * visor, volver al chat, abrir un link externo — sale por postMessage. El
 * iframe no tiene acceso a window.electronAPI.
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const MD_OK = typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined';

  let expediente = null;   // la ficha ya cargada
  let historial  = [];     // turnos del chat de este proyecto
  let enviando   = false;

  // ── Utilidades ────────────────────────────────────
  function esc(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function md(texto) {
    if (!MD_OK) return `<p>${esc(texto).replace(/\n/g, '<br>')}</p>`;
    return DOMPurify.sanitize(marked.parse(texto), {
      ALLOWED_TAGS: ['p','br','h1','h2','h3','h4','h5','h6','strong','em','code','pre',
                     'blockquote','table','thead','tbody','tr','th','td','ul','ol','li','a','hr','span'],
      ADD_ATTR: ['target','rel'],
    });
  }

  // Un valor vacío se pinta como "- -" igual que la ficha oficial: que un
  // campo no tenga dato también es información, y dejarlo en blanco parece
  // un error de carga.
  function valor(v) {
    const t = (v ?? '').toString().trim();
    return t
      ? `<div class="exp-field-value">${esc(t)}</div>`
      : '<div class="exp-field-value empty">- -</div>';
  }

  function campo(label, v, clases = '') {
    return `<div class="exp-field ${clases}">
      <div class="exp-field-label">${esc(label)}</div>
      ${valor(v)}
    </div>`;
  }

  // "A, B, C" -> lista con viñetas, como el portal muestra autores y coautores.
  function campoLista(label, crudo) {
    const items = (crudo || '').split(',').map(s => s.trim()).filter(Boolean);
    const cuerpo = items.length
      ? `<ul class="exp-list">${items.map(i => `<li>${esc(i)}</li>`).join('')}</ul>`
      : '<div class="exp-field-value empty">- -</div>';
    return `<div class="exp-field">
      <div class="exp-field-label">${esc(label)}</div>
      ${cuerpo}
    </div>`;
  }

  function seccion(titulo, contenidoHTML) {
    return `<div class="exp-section">
      <div class="exp-section-tab">${esc(titulo)}</div>
      <div class="exp-card">${contenidoHTML}</div>
    </div>`;
  }

  const ICONO_PDF = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 1.5H4A1.5 1.5 0 0 0 2.5 3v10A1.5 1.5 0 0 0 4 14.5h8a1.5 1.5 0 0 0 1.5-1.5V5.5z"/><path d="M9.5 1.5v4h4"/></svg>`;

  // Los adjuntos se guardan en un registro y el botón referencia su índice:
  // meter la URL en un atributo del HTML obliga a escaparla bien en dos
  // contextos distintos y es una vía fácil de romper.
  const adjuntos = [];
  function botonAdjunto(a) {
    if (!a || !a.url) return '';
    const i = adjuntos.push(a) - 1;
    const nombre = a.descripcion || a.nombre || 'Adjunto';
    // Solo el ícono, como la ficha oficial: el nombre del archivo es largo
    // ("PROYECTO DE LEY-00088-2026-2031-CD") y como texto del botón desbordaba
    // la tabla. Va en el title, que es donde el portal también lo pone.
    return `<button class="exp-adj" data-adj="${i}" title="${esc(nombre)}" aria-label="${esc(nombre)}">
      ${ICONO_PDF}
    </button>`;
  }

  // ── Render de la ficha ────────────────────────────
  function render(d) {
    const partes = [];

    // Bloque 1: la ficha general
    partes.push(seccion(`Proposición Nº ${d.numero || ''}`, `
      <div class="exp-grid exp-grid-4" style="margin-bottom:16px">
        ${campo('Periodo parlamentario', d.periodo_parlamentario)}
        ${campo('Legislatura', d.legislatura)}
        ${campo('Fecha de presentación', d.fecha_presentacion)}
        ${campo('Proponente', d.proponente)}
      </div>
      <div class="exp-grid" style="margin-bottom:16px">
        ${campo('Título', d.titulo, 'hl')}
        ${campo('Sumilla', d.sumilla, 'hl')}
      </div>
      <div class="exp-grid exp-grid-3" style="margin-bottom:16px">
        ${campoLista('Autor principal', d.autor_principal)}
        ${campoLista('Coautores', d.coautores)}
        ${campoLista('Adherentes', d.adherentes)}
      </div>
      <div class="exp-grid exp-grid-2">
        ${campo('Grupo parlamentario', d.grupo_parlamentario)}
        ${campo('Último estado', d.estado, 'hl estado')}
      </div>
    `));

    // Bloque 2: seguimientos
    const seg = d.seguimiento || [];
    partes.push(seccion('Seguimientos', seg.length ? `
      <div class="exp-table-wrap"><table class="exp-table">
        <thead><tr>
          <th class="col-corta">Fecha</th><th class="col-corta">Estado procesal</th>
          <th class="col-corta">Comisión</th>
          <th class="col-detalle">Detalle</th><th class="col-adj">Adjuntos</th>
        </tr></thead>
        <tbody>${seg.map(s => `<tr>
          <td class="col-corta">${esc(s.fecha)}</td>
          <td class="col-corta">${esc(s.estado)}</td>
          <td class="col-corta">${esc(s.comision) || '<span style="color:var(--dim)">—</span>'}</td>
          <td class="col-detalle">${esc(s.detalle)}</td>
          <td class="col-adj">${(s.adjuntos || []).map(botonAdjunto).join('') || '<span style="color:var(--dim)">—</span>'}</td>
        </tr>`).join('')}</tbody>
      </table></div>
    ` : '<div class="exp-empty">No se encontraron registros.</div>'));

    // Bloque 3: proyectos acumulados
    const acu = d.proyectos_acumulados || [];
    partes.push(seccion('Proyectos acumulados', acu.length ? `
      <div class="exp-table-wrap"><table class="exp-table">
        <thead><tr><th>Proposición</th><th>Fecha</th><th>Título</th><th>Estado</th></tr></thead>
        <tbody>${acu.map(p => `<tr>
          <td>${esc(p.numero)}</td>
          <td>${esc(p.fecha_presentacion)}</td>
          <td>${esc(p.titulo)}</td>
          <td>${esc(p.estado)}</td>
        </tr>`).join('')}</tbody>
      </table></div>
    ` : '<div class="exp-empty">No se encontraron registros.</div>'));

    // Bloque 4: documentación anexa
    const doc = d.documentacion_anexa || [];
    partes.push(seccion('Documentación anexa', doc.length ? `
      <div class="exp-table-wrap"><table class="exp-table">
        <thead><tr><th class="col-corta">Fecha</th><th>Descripción</th>
          <th class="col-adj">Adjuntos</th></tr></thead>
        <tbody>${doc.map(x => `<tr>
          <td class="col-corta">${esc(x.fecha)}</td>
          <td>${esc(x.descripcion)}</td>
          <td class="col-adj">${(x.adjuntos || []).map(botonAdjunto).join('') || '<span style="color:var(--dim)">—</span>'}</td>
        </tr>`).join('')}</tbody>
      </table></div>
    ` : '<div class="exp-empty">No se encontraron registros.</div>'));

    // Bloque 5: opinión ciudadana
    const o = d.opinion_ciudadana || {};
    const op = (color, icono, num, label) => `<div class="exp-op">
      <div class="exp-op-top">
        <div class="exp-op-dot" style="background:${color}">${icono}</div>
        <div class="exp-op-num">${esc(num ?? 0)}</div>
      </div>
      <div class="exp-op-label">${esc(label)}</div>
    </div>`;
    const I_UP   = `<svg viewBox="0 0 16 16" fill="currentColor"><path d="M6 14H4a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1h2zm1-7.2 2.6-4.5a1.4 1.4 0 0 1 2.6.9L11.7 6H14a1.3 1.3 0 0 1 1.3 1.6l-1 4.6A1.8 1.8 0 0 1 12.5 14H7z"/></svg>`;
    const I_DOWN = `<svg viewBox="0 0 16 16" fill="currentColor" style="transform:rotate(180deg)"><path d="M6 14H4a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1h2zm1-7.2 2.6-4.5a1.4 1.4 0 0 1 2.6.9L11.7 6H14a1.3 1.3 0 0 1 1.3 1.6l-1 4.6A1.8 1.8 0 0 1 12.5 14H7z"/></svg>`;
    const I_STAR = `<svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 1.6l1.9 3.9 4.3.6-3.1 3 .7 4.3L8 11.4l-3.8 2 .7-4.3-3.1-3 4.3-.6z"/></svg>`;
    const I_CHAT = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M14 7.6a5.6 5.6 0 0 1-6 5.6c-.7 0-1.3-.1-1.9-.3L2.7 14l1-3.2A5.6 5.6 0 1 1 14 7.6z"/></svg>`;

    partes.push(seccion('Opinión ciudadana', `<div class="exp-opinion">
      ${op('#22a06b', I_UP,   o.a_favor,               'A favor')}
      ${op('#5b6b7a', I_DOWN, o.en_contra,             'En contra')}
      ${op('#e8991b', I_STAR, o.propuesta_alternativa, 'Propuesta alternativa')}
      ${op('#7b8794', I_CHAT, o.total_opiniones,       'Total de opiniones')}
    </div>`));

    $('exp-body').innerHTML = partes.join('');
    $('exp-loading').style.display = 'none';
    $('exp-body').style.display    = '';
  }

  // ── Carga ─────────────────────────────────────────
  const numero = new URLSearchParams(location.search).get('numero') || '';

  function errorFicha(titulo, detalle) {
    $('exp-loading').style.display = 'none';
    $('exp-body').style.display = '';
    $('exp-body').innerHTML = `<div class="exp-error">
      <h3>${esc(titulo)}</h3><p>${esc(detalle)}</p>
    </div>`;
  }

  async function cargar() {
    if (!numero) { errorFicha('Falta el número', 'No se indicó qué proposición mostrar.'); return; }
    $('exp-topbar-num').textContent = `Proposición Nº ${numero}`;

    try {
      const r = await fetch(`/expediente?numero=${encodeURIComponent(numero)}`);
      const d = await r.json();
      if (d.error) { errorFicha('No se pudo cargar el expediente', d.error); return; }

      expediente = d;
      render(d);
      $('exp-topbar-num').textContent = `Proposición Nº ${d.numero || numero}`;
      $('exp-chat-sub').textContent =
        `Diana ya tiene la ficha de ${d.numero || numero} a la vista.`;

      const portal = $('exp-portal');
      if (d.enlace_expediente) portal.dataset.url = d.enlace_expediente;
      else portal.style.display = 'none';

      pintarChips();
    } catch (e) {
      errorFicha('No se pudo cargar el expediente',
                 `Falló la consulta a SPLEY: ${e.message}`);
    }
  }

  // ── Chat del proyecto ─────────────────────────────
  // El modelo arranca con la ficha ya "leída": sin esto, cada pregunta lo
  // obligaba a volver a llamar la herramienta del expediente para saber de qué
  // proyecto se está hablando.
  function contextoInicial() {
    const d = expediente || {};
    return [
      `Estoy viendo la ficha del expediente de la proposición legislativa ${d.numero || numero}.`,
      `Título: ${d.titulo || '—'}`,
      `Sumilla: ${d.sumilla || '—'}`,
      `Fecha de presentación: ${d.fecha_presentacion || '—'}`,
      `Estado: ${d.estado || '—'}`,
      `Proponente: ${d.proponente || '—'}`,
      `Autor principal: ${d.autor_principal || '—'}`,
      `Coautores: ${d.coautores || '—'}`,
      `Grupo parlamentario: ${d.grupo_parlamentario || '—'}`,
      `Periodo: ${d.periodo_parlamentario || '—'} | Legislatura: ${d.legislatura || '—'}`,
      '',
      'Mis próximas preguntas son sobre esta proposición salvo que diga otra cosa.',
    ].join('\n');
  }

  const CHIPS = [
    '¿De qué trata este proyecto en lenguaje simple?',
    '¿En qué estado del trámite está y qué sigue?',
    '¿A quiénes afecta y qué cambiaría en la práctica?',
    'Resumime los seguimientos hasta ahora',
  ];

  function pintarChips() {
    $('exp-chips').innerHTML = CHIPS.map(c =>
      `<button class="exp-chip">${esc(c)}</button>`).join('');
  }

  function burbuja(clase, html) {
    const d = document.createElement('div');
    d.className = `exp-msg ${clase}`;
    d.innerHTML = html;
    $('exp-chat-log').appendChild(d);
    abajo();
    return d;
  }

  function abajo() {
    const log = $('exp-chat-log');
    log.scrollTop = log.scrollHeight;
  }

  async function enviar(texto) {
    texto = (texto || '').trim();
    if (!texto || enviando) return;

    const chips = $('exp-chips');
    if (chips) chips.remove();

    enviando = true;
    $('exp-send').disabled = true;
    $('exp-input').value = '';
    $('exp-input').style.height = 'auto';

    burbuja('user', esc(texto));
    historial.push({ role: 'user', content: texto });

    const bot = burbuja('bot', '<span class="exp-status">Pensando…</span>');
    let completo = '';

    try {
      // El contexto va como primer turno de la conversación, no como parte de
      // la pregunta: así no se repite en cada mensaje ni ensucia lo que el
      // usuario escribió.
      const mensajes = [
        { role: 'user',      content: contextoInicial() },
        { role: 'assistant', content: `Listo, tengo a la vista el expediente de ${(expediente && expediente.numero) || numero}. ¿Qué querés saber?` },
        ...historial,
      ];

      const resp = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: mensajes }),
      });

      const reader  = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lineas = buf.split('\n');
        buf = lineas.pop();

        for (const linea of lineas) {
          if (!linea.startsWith('data: ')) continue;
          const crudo = linea.slice(6).trim();
          if (crudo === '[DONE]') continue;
          try {
            const obj = JSON.parse(crudo);
            if (obj.error)  { bot.innerHTML = md(`**Error:** ${obj.error}`); break; }
            if (obj.status) { bot.innerHTML = `<span class="exp-status">${esc(obj.status)}</span>`; abajo(); }
            if (obj.text)   { completo += obj.text; bot.innerHTML = md(completo); abajo(); }
          } catch { /* fragmento incompleto */ }
        }
      }

      if (completo) historial.push({ role: 'assistant', content: completo });
      else if (!bot.textContent.trim()) bot.innerHTML = md('_Sin respuesta._');
    } catch (e) {
      bot.innerHTML = md(`**Error de conexión:** ${e.message}`);
    } finally {
      // Siempre en el finally: si esto queda colgado, el chat del panel deja
      // de aceptar mensajes para siempre y sin señal visible.
      enviando = false;
      $('exp-send').disabled = false;
      abajo();
    }
  }

  // ── Eventos ───────────────────────────────────────
  $('exp-send').addEventListener('click', () => enviar($('exp-input').value));

  $('exp-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); enviar($('exp-input').value); }
  });

  $('exp-input').addEventListener('input', function () {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 130) + 'px';
  });

  $('exp-chat-log').addEventListener('click', (e) => {
    const chip = e.target.closest('.exp-chip');
    if (chip) enviar(chip.textContent);
  });

  // Adjuntos → se cargan en el chat de ESTE panel.
  //
  // Delegarlo al host con un postMessage 'load-pdf' (como hace pdfs.js) lo
  // mandaba al chat principal y de paso cerraba la ficha: el host hace
  // switchToChat() antes de cargarlo. Justo lo contrario de tener la ficha y
  // el chat lado a lado, así que el panel se ocupa del PDF por su cuenta y la
  // ficha se queda donde está.
  $('exp-scroll').addEventListener('click', (e) => {
    const btn = e.target.closest('.exp-adj');
    if (!btn) return;
    const a = adjuntos[Number(btn.dataset.adj)];
    if (a && a.url) cargarAdjunto(a);
  });

  async function cargarAdjunto(a) {
    if (enviando) return;
    const titulo = a.descripcion || a.nombre || `Adjunto de ${numero}`;

    const chips = $('exp-chips');
    if (chips) chips.remove();

    enviando = true;
    $('exp-send').disabled = true;
    burbuja('user', `📄 ${esc(titulo)}`);
    const bot = burbuja('bot', '<span class="exp-status">Leyendo el PDF…</span>');

    try {
      const res  = await fetch('/load-pdf-url', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: a.url }),
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || 'No se pudo leer el PDF');

      // El texto entra como turno del usuario para que quede en el contexto de
      // las próximas preguntas, igual que hace el chat principal.
      historial.push({
        role: 'user',
        content: `He cargado el documento "${titulo}" (${data.pages} páginas).\n\n` +
                 `Contenido:\n${data.text}\n\n¿Qué querés analizar?`,
      });
      bot.innerHTML = md(`**PDF cargado** — ${titulo} (${data.pages} págs.). Preguntame lo que quieras sobre su contenido.`);
    } catch (err) {
      bot.innerHTML = md(`**No se pudo cargar el PDF:** ${err.message}`);
    } finally {
      enviando = false;
      $('exp-send').disabled = false;
      abajo();
    }
  }

  $('exp-back').addEventListener('click', () => {
    window.parent.postMessage('close-expediente', '*');
  });

  $('exp-portal').addEventListener('click', (e) => {
    e.preventDefault();
    const url = e.currentTarget.dataset.url;
    if (url) window.parent.postMessage({ type: 'open-external', url }, '*');
  });

  window.addEventListener('message', (e) => {
    if (e.data && e.data.type === 'theme') {
      document.documentElement.dataset.theme = e.data.dark ? 'dark' : 'light';
    }
  });

  cargar();
})();
