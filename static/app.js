(() => {
  // ── Modo oscuro ───────────────────────────────────
  const DARK_KEY = 'congreso_dark';
  const html     = document.documentElement;

  function applyTheme(dark) {
    html.dataset.theme = dark ? 'dark' : 'light';
    // Dos controles muestran el estado del tema: el ítem del menú de perfil
    // (con rótulo "Modo oscuro") y el botón suelto de la esquina superior
    // derecha. El del menú usa la luna como "activar modo oscuro"; el de la
    // esquina, en cambio, indica el tema vigente — sol en claro, luna en
    // oscuro (ver captura del diseño).
    const icon = (id, visible) => {
      const el = document.getElementById(id);
      if (el) el.style.display = visible ? '' : 'none';
    };
    icon('dark-icon-moon',  !dark);
    icon('dark-icon-sun',    dark);
    icon('theme-icon-sun',  !dark);
    icon('theme-icon-moon',  dark);
    localStorage.setItem(DARK_KEY, dark ? '1' : '0');
    // Live y PDFs viven en iframes con su propio document: no heredan el
    // [data-theme] del padre solos. Les avisamos por postMessage para que
    // el cambio se vea al toque si ya están abiertos (si no, leen el mismo
    // localStorage al cargar — ver live.html/pdfs.html).
    for (const id of ['live-iframe', 'pdfs-iframe', 'expediente-iframe']) {
      document.getElementById(id)?.contentWindow?.postMessage({ type: 'theme', dark }, '*');
    }
  }

  applyTheme(localStorage.getItem(DARK_KEY) === '1');

  const toggleTheme = () => applyTheme(html.dataset.theme !== 'dark');
  document.getElementById('dark-toggle').addEventListener('click', toggleTheme);
  document.getElementById('theme-btn')?.addEventListener('click', toggleTheme);

  // ── Color de acento (configurable) ────────────────
  const ACCENT_KEY     = 'diana_accent';
  const ACCENT_DEFAULT = '#111111';

  function hexToRgb(hex) {
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
    return m ? { r: parseInt(m[1], 16), g: parseInt(m[2], 16), b: parseInt(m[3], 16) } : { r: 17, g: 17, b: 17 };
  }
  function shade(hex, percent) {
    const { r, g, b } = hexToRgb(hex);
    const f = v => Math.max(0, Math.min(255, Math.round(v + (percent / 100) * v)));
    return `rgb(${f(r)}, ${f(g)}, ${f(b)})`;
  }
  // shade() multiplica el valor actual, así que con negro puro (0,0,0) no
  // aclara nada (0 * cualquier % sigue siendo 0). Para eso este otro: mezcla
  // hacia blanco de forma aditiva, sí funciona con negro.
  function mixToward(hex, targetRgb, amount) {
    const { r, g, b } = hexToRgb(hex);
    const mix = (v, t) => Math.round(v + (t - v) * amount);
    return { r: mix(r, targetRgb[0]), g: mix(g, targetRgb[1]), b: mix(b, targetRgb[2]) };
  }
  function relativeLuminance({ r, g, b }) {
    const lin = c => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  }

  function applyAccent(color) {
    document.documentElement.style.setProperty('--accent', color);
    const { r, g, b } = hexToRgb(color);
    document.documentElement.style.setProperty('--accent-rgb', `${r},${g},${b}`);
    document.documentElement.style.setProperty('--accent-hover', shade(color, -18));

    // El acento se usa como color de ícono/texto sobre tarjetas oscuras
    // (modo oscuro) y como color de un glow — si el usuario elige negro (o
    // cualquier color muy oscuro), ese texto/glow se vuelve invisible sobre
    // un fondo que también es casi negro. --accent-fg es una variante que
    // se aclara hacia blanco solo cuando hace falta, para que el botón
    // "Nuevo chat" y los íconos sigan viéndose sin importar el acento
    // elegido. En modo claro no hace falta (fondo claro, negro se ve bien),
    // así que el CSS solo la usa dentro de [data-theme="dark"].
    const lum = relativeLuminance({ r, g, b });
    const fg = lum < 0.22 ? mixToward(color, [255, 255, 255], 0.7) : { r, g, b };
    document.documentElement.style.setProperty('--accent-fg-rgb', `${fg.r},${fg.g},${fg.b}`);
    document.documentElement.style.setProperty('--accent-fg', `rgb(${fg.r},${fg.g},${fg.b})`);

    localStorage.setItem(ACCENT_KEY, color);
    syncAccentUI(color);
  }

  function syncAccentUI(color) {
    document.querySelectorAll('.accent-swatch').forEach(sw => {
      sw.classList.toggle('active', sw.dataset.color.toLowerCase() === color.toLowerCase());
    });
    const customInput = document.getElementById('accent-custom-input');
    if (customInput) customInput.value = color;
  }

  // Sin acento elegido no se toca ninguna variable: manda el CSS, que define
  // --accent claro u oscuro según el tema. Fijarlo inline acá (como se hacía
  // antes, cuando el default era rojo y se veía en los dos temas) dejaría el
  // negro por defecto invisible sobre el fondo del modo oscuro.
  const storedAccent = localStorage.getItem(ACCENT_KEY);
  if (storedAccent) applyAccent(storedAccent);
  else              syncAccentUI(ACCENT_DEFAULT);

  // ── Color de cada tarjeta de métrica (independiente del acento) ───
  // Antes eran fijos (rosa/violeta/índigo, ver nuevaimagenfront.png) — el
  // usuario pidió poder cambiarlos, uno por uno, sin que dependan del
  // selector de acento general.
  const METRIC_COLOR_DEFAULTS = {
    sesion:     '#111111',
    proyectos:  '#111111',
    comisiones: '#111111',
    citaciones: '#111111',
  };
  const METRIC_COLOR_KEY = 'diana_metric_colors';

  // Solo lo que el usuario eligió — sin mezclar los valores de fábrica. Si
  // se mezclaran, al tocar UNA tarjeta se persistirían las cuatro y las tres
  // no elegidas perderían el fallback por tema.
  function loadMetricColors() {
    try   { return JSON.parse(localStorage.getItem(METRIC_COLOR_KEY) || '{}'); }
    catch { return {}; }
  }

  function applyMetricColor(key, color) {
    document.documentElement.style.setProperty(`--c-metric-${key}`, color);
    const colors = loadMetricColors();
    colors[key] = color;
    localStorage.setItem(METRIC_COLOR_KEY, JSON.stringify(colors));
    const input = document.getElementById(`metric-color-${key}`);
    if (input) input.value = color;
  }

  const initialMetricColors = loadMetricColors();
  for (const key of Object.keys(METRIC_COLOR_DEFAULTS)) {
    const input = document.getElementById(`metric-color-${key}`);
    if (initialMetricColors[key]) applyMetricColor(key, initialMetricColors[key]);
    else if (input) input.value = METRIC_COLOR_DEFAULTS[key];
    if (input) input.addEventListener('input', (e) => applyMetricColor(key, e.target.value));
  }

  const settingsBtn    = document.getElementById('settings-btn');
  const accentPopover  = document.getElementById('accent-popover');
  const accentMenuBtn  = document.getElementById('accent-menu-btn');

  function toggleAccentPopover(show) {
    const isOpen = show !== undefined ? show : accentPopover.style.display === 'none';
    accentPopover.style.display = isOpen ? 'block' : 'none';
  }

  // #settings-btn se sacó del header con el rediseño (su lugar lo ocupa el
  // toggle de tema). El popover se abre desde el menú de perfil → "Color de
  // acento"; el listener queda por si el botón vuelve.
  settingsBtn?.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleAccentPopover();
  });
  accentMenuBtn.addEventListener('click', () => {
    closeProfileMenu();
    toggleAccentPopover(true);
  });
  document.querySelectorAll('.accent-swatch').forEach(sw => {
    sw.addEventListener('click', () => applyAccent(sw.dataset.color));
  });
  document.getElementById('accent-custom-input').addEventListener('input', (e) => {
    applyAccent(e.target.value);
  });
  document.addEventListener('click', (e) => {
    if (!accentPopover.contains(e.target) && e.target !== settingsBtn && !settingsBtn?.contains(e.target)) {
      toggleAccentPopover(false);
    }
  });

  // ── Menú de perfil (chevron en el sidebar) ────────
  const profileMenuBtn  = document.getElementById('profile-menu-btn');
  const profileMenu     = document.getElementById('profile-menu');
  const profileChevron  = document.getElementById('profile-chevron-btn');

  function closeProfileMenu() {
    profileMenu.style.display = 'none';
    profileChevron.classList.remove('open');
  }
  function toggleProfileMenu() {
    const isOpen = profileMenu.style.display !== 'none';
    profileMenu.style.display = isOpen ? 'none' : 'block';
    profileChevron.classList.toggle('open', !isOpen);
  }
  profileMenuBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleProfileMenu();
  });
  document.addEventListener('click', (e) => {
    if (!profileMenuBtn.contains(e.target) && !profileMenu.contains(e.target)) closeProfileMenu();
  });

  // ── DOM refs ─────────────────────────────────────
  const chatArea      = document.getElementById('chat-area');
  const msgInput      = document.getElementById('msg-input');
  const sendBtn       = document.getElementById('send-btn');
  const chatList      = document.getElementById('chat-list');
  const newChatBtn    = document.getElementById('new-chat-btn');
  const newChatWideBtn = document.getElementById('new-chat-wide-btn');
  const cmdChips      = document.getElementById('cmd-chips');
  const mainEl        = document.querySelector('.main');
  const sidebar       = document.querySelector('.sidebar');
  const sidebarToggle = document.getElementById('sidebar-toggle');
  const sidebarOpen   = document.getElementById('sidebar-open');

  // ── Sidebar toggle ────────────────────────────────
  function toggleSidebar(open) {
    const isOpen = open !== undefined ? open : sidebar.classList.contains('collapsed');
    sidebar.classList.toggle('collapsed', !isOpen);
    sidebarOpen.style.display = isOpen ? 'none' : 'flex';
  }

  sidebarToggle.addEventListener('click', () => toggleSidebar());
  sidebarOpen.addEventListener('click',   () => toggleSidebar(true));

  // ── Conversations (archivo JSON via IPC, localStorage como fallback) ──
  const STORE = 'congreso_convs';
  let convs    = [];
  let activeId = null;
  let streaming= false;
  // Controlador para poder cortar una respuesta en curso. `streaming` bloquea
  // TODA la interfaz (chips, tarjetas de métricas, resumen y enviar), y desde
  // que las respuestas largas —un cuadro de 89 proposiciones son ~14 mil
  // tokens— pueden tardar más de un minuto, quedarse sin salida durante todo
  // ese rato hace que la app parezca colgada. Con esto el botón de enviar se
  // convierte en "detener" mientras trabaja.
  let abortCtl = null;

  function loadConvs() {
    try { convs = JSON.parse(localStorage.getItem(STORE)) || []; }
    catch { convs = []; }
  }

  function saveConvs() {
    // localStorage tiene ~5 MB: unas pocas respuestas de expediente completo
    // lo llenan y setItem tira QuotaExceededError. Si eso escala, congela el
    // chat entero, así que acá se descartan las conversaciones más viejas.
    try {
      localStorage.setItem(STORE, JSON.stringify(convs));
    } catch (e) {
      console.warn('localStorage lleno, recortando historial:', e.name);
      try {
        convs = convs.slice(0, 10);
        localStorage.setItem(STORE, JSON.stringify(convs));
      } catch {
        localStorage.removeItem(STORE);
      }
    }
    if (window.electronAPI?.saveHistory) {
      window.electronAPI.saveHistory(convs).catch(() => {});
    }
  }

  function getActive() {
    return convs.find(c => c.id === activeId) || null;
  }

  function newChat() {
    const id = Date.now().toString();
    convs.unshift({ id, title: 'Nueva conversación', messages: [], ts: Date.now() });
    activeId = id;
    saveConvs();
    renderSidebar();
    showWelcome();
  }

  function switchConv(id) {
    if (id === activeId) return;
    chatArea.classList.add('fading');
    setTimeout(() => {
      activeId = id;
      renderSidebar();
      renderMessages();
      chatArea.classList.remove('fading');
    }, 160);
  }

  function deleteConv(id) {
    convs = convs.filter(c => c.id !== id);
    if (activeId === id) activeId = convs[0]?.id || null;
    saveConvs();
    renderSidebar();
    if (activeId) renderMessages(); else showWelcome();
  }

  function renameConvInline(id, span) {
    const conv = convs.find(c => c.id === id);
    if (!conv) return;

    const input = document.createElement('input');
    input.className = 'chat-item-rename';
    input.value     = conv.title;
    span.replaceWith(input);
    input.focus();
    input.select();

    function commit() {
      const val = input.value.trim();
      if (val) conv.title = val;
      saveConvs();
      renderSidebar();
    }
    input.addEventListener('blur',   commit);
    input.addEventListener('keydown', ev => {
      if (ev.key === 'Enter')  { ev.preventDefault(); input.blur(); }
      if (ev.key === 'Escape') { input.value = conv.title; input.blur(); }
    });
    input.addEventListener('click', ev => ev.stopPropagation());
  }

  function autoTitle(text) {
    return text.slice(0, 38) + (text.length > 38 ? '…' : '');
  }

  // Delegación de eventos en el sidebar — un solo listener para delete y rename
  chatList.addEventListener('click', e => {
    const delBtn = e.target.closest('.chat-item-del');
    if (delBtn) {
      e.stopPropagation();
      const id = delBtn.closest('.chat-item')?.dataset.id;
      if (id) deleteConv(id);
      return;
    }
  });
  chatList.addEventListener('dblclick', e => {
    const title = e.target.closest('.chat-item-title');
    if (title) {
      const id = title.closest('.chat-item')?.dataset.id;
      if (!id) return;
      // Si no es la activa, activarla primero y luego renombrar después del re-render
      if (id !== activeId) {
        activeId = id;
        renderSidebar();
        renderMessages();
        const newTitle = chatList.querySelector(`.chat-item[data-id="${id}"] .chat-item-title`);
        if (newTitle) renameConvInline(id, newTitle);
      } else {
        renameConvInline(id, title);
      }
    }
  });

  // ── Sidebar render ────────────────────────────────
  function formatItemTime(ts) {
    const DAY = 86400000;
    const age = Date.now() - ts;
    if (age < DAY) return new Date(ts).toLocaleTimeString('es-PE', { hour: '2-digit', minute: '2-digit' });
    if (age < 2 * DAY) return 'Ayer';
    const days = Math.floor(age / DAY);
    if (days < 7) return `${days} días`;
    return new Date(ts).toLocaleDateString('es-PE', { day: '2-digit', month: '2-digit' });
  }

  function getPreview(conv) {
    const first = conv.messages.find(m => m.role === 'user');
    if (!first) return '';
    const text = first.content.replace(/^__RESUMEN_SEMANAL__:?/, '').replace(/\s+/g, ' ').trim();
    return text.length > 46 ? text.slice(0, 46) + '…' : text;
  }

  function renderSidebar() {
    chatList.innerHTML = '';

    if (!convs.length) {
      chatList.innerHTML = '<div style="padding:16px 10px;font-size:12px;color:var(--text-dim)">Sin conversaciones</div>';
      return;
    }

    // El rótulo "Historial" ya está fijo en index.html, justo encima de
    // #chat-list. Agregar otro acá lo mostraba dos veces apenas existía una
    // conversación (con la lista vacía solo se veía el de index.html, por eso
    // pasaba desapercibido).
    for (const c of convs) {
      const el = document.createElement('div');
      el.className = 'chat-item' + (c.id === activeId ? ' active' : '');
      el.dataset.id = c.id;
      const preview = getPreview(c);
      el.innerHTML = `
        <div class="chat-item-row1">
          <span class="chat-item-icon">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 4h14a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H7l-4 2V5a1 1 0 0 1 1-1z"/>
            </svg>
          </span>
          <span class="chat-item-title" title="Doble clic para renombrar">${escHtml(c.title)}</span>
          <span class="chat-item-time">${formatItemTime(c.ts)}</span>
          <button class="chat-item-del" title="Eliminar conversación" data-del-id="${c.id}">
            <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="pointer-events:none">
              <path d="M2 2l10 10M12 2L2 12"/>
            </svg>
          </button>
        </div>
        ${preview ? `<div class="chat-item-preview">${escHtml(preview)}</div>` : ''}`;
      el.querySelector('.chat-item-del').addEventListener('click', (e) => {
        e.stopPropagation();
        deleteConv(c.id);
      });
      el.addEventListener('click', (e) => {
        if (e.target.closest('.chat-item-del')) return;
        el.classList.add('clicking');
        el.addEventListener('animationend', () => el.classList.remove('clicking'), { once: true });
        switchConv(c.id);
      });
      chatList.appendChild(el);
    }
  }

  // ── Message rendering ─────────────────────────────
  const USER_FIRST_NAME = 'Julio César';

  // Íconos de las 4 tarjetas — mismo lenguaje visual que .chip-card-icon.
  const METRIC_ICONS = {
    sesion: '<svg viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="2" y="4" width="14" height="12" rx="1.5"/><path d="M6 2v4M12 2v4M2 8h14"/></svg>',
    proyectos: '<svg viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 2a1.5 1.5 0 0 0-1.5 1.5v11A1.5 1.5 0 0 0 4 16h10a1.5 1.5 0 0 0 1.5-1.5V6L11 2z"/><path d="M11 2v4h4"/></svg>',
    comisiones: '<svg viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="6" cy="6" r="2.3"/><circle cx="12" cy="6" r="2.3"/><path d="M2 15c0-2.1 1.8-3.8 4-3.8M12 11.2c2.2 0 4 1.7 4 3.8M9 11v4"/></svg>',
    citaciones: '<svg viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 2l1.8 3.7 4 .6-3 2.9.7 4-3.5-1.9-3.5 1.9.7-4-3-2.9 4-.6z"/></svg>',
  };

  // Comando real que dispara cada tarjeta al hacer clic en "Ver más" — los
  // mismos textos que usan los chips de Acciones rápidas, para que la
  // respuesta sea idéntica a la que ya está verificada ahí.
  const METRIC_CMD = {
    sesion: 'Dame la agenda de sesiones de comisiones de los próximos 2 días en SENADO y DIPUTADOS, en un cuadro con fecha, hora, lugar y link',
    proyectos: 'Revisa los proyectos de ley de los últimos 15 días calendario, y entrégame un cuadro resumen dividido por temas',
    comisiones: 'Dame los accesos al Cuadro de Comisiones y miembros de la Comisión Permanente del SENADO y DIPUTADOS',
    citaciones: 'Dame el resumen y el acceso a descarga de documentos en DESTACADO y CITACIONES de DIPUTADOS, SENADO y CONGRESO',
  };
  const METRIC_VER_LABEL = {
    sesion: 'Ver agenda', proyectos: 'Ver proyectos',
    comisiones: 'Ver comisiones', citaciones: 'Ver citaciones',
  };

  function metricCardHTML(key, label) {
    return `
      <div class="metric-card is-loading" data-metric="${key}">
        <span class="metric-card-icon">${METRIC_ICONS[key]}</span>
        <span class="metric-card-label">${label}</span>
        <span class="metric-card-value">…</span>
      </div>`;
  }

  async function loadDashboardMetrics() {
    let data;
    try {
      const res = await fetch('/dashboard-metrics');
      if (!res.ok) throw new Error('bad status');
      data = await res.json();
    } catch {
      // Sin datos reales disponibles: se ocultan las tarjetas en vez de
      // inventar un número — mismo criterio que el resto de la app.
      document.querySelector('.welcome-metrics')?.remove();
      return;
    }

    const fill = (key, valueHTML, subText) => {
      const card = document.querySelector(`.metric-card[data-metric="${key}"]`);
      if (!card) return;
      card.classList.remove('is-loading');
      card.querySelector('.metric-card-value').innerHTML = valueHTML;
      // El valor se reemplaza (innerHTML), pero el subtítulo y el botón se
      // AGREGAN. Si fill() corre más de una vez sobre la misma tarjeta —
      // /dashboard-metrics tarda ~20 s con caché fría, así que un segundo
      // showWelcome() puede resolver encima del primero — la tarjeta termina
      // con el pie repetido N veces (visto en vivo: 3 copias por tarjeta).
      // Limpiar antes de agregar hace que fill() sea idempotente pase lo que
      // pase con el orden de las respuestas.
      card.querySelectorAll('.metric-card-sub, .metric-card-ver').forEach(n => n.remove());
      if (subText) {
        const sub = document.createElement('span');
        sub.className = 'metric-card-sub';
        sub.textContent = subText;
        card.appendChild(sub);
      }
      const ver = document.createElement('button');
      ver.className = 'metric-card-ver';
      ver.type = 'button';
      ver.innerHTML = `${METRIC_VER_LABEL[key]} <svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M1.5 6h9M7 2.5L10.5 6 7 9.5"/></svg>`;
      ver.addEventListener('click', () => { if (!streaming) send(METRIC_CMD[key]); });
      card.appendChild(ver);
    };

    if (data.proxima_sesion) {
      const s = data.proxima_sesion;
      fill('sesion', `${escHtml(s.etiqueta_fecha)} · ${escHtml(s.hora)}`, s.camara || '');
    } else {
      fill('sesion', 'Sin sesiones', 'próximos 14 días');
    }

    if (data.proyectos_ingresados) {
      const p = data.proyectos_ingresados;
      fill('proyectos', String(p.total), `últimos ${p.dias} días`);
    }

    if (data.comisiones_registradas) {
      const c = data.comisiones_registradas;
      fill('comisiones', String(c.total), `${c.senado} Senado · ${c.diputados} Diputados`);
    }

    if (data.citaciones) {
      fill('citaciones', String(data.citaciones.total), 'esta semana');
    }
  }

  function saludoSegunHora() {
    const h = new Date().getHours();
    if (h < 12) return 'Buenos días';
    if (h < 19) return 'Buenas tardes';
    return 'Buenas noches';
  }

  function showWelcome() {
    mainEl.classList.remove('chat-mode');
    chatArea.innerHTML = `
      <div class="welcome">
        <div class="welcome-heading">${saludoSegunHora()}, ${escHtml(USER_FIRST_NAME)} <span class="wave">👋</span></div>
        <div class="welcome-subtitle">Tu asistente especializado en información legislativa del Congreso</div>
        <div class="welcome-metrics">
          ${metricCardHTML('sesion', 'Próxima sesión')}
          ${metricCardHTML('proyectos', 'Proyectos ingresados')}
          ${metricCardHTML('comisiones', 'Comisiones registradas')}
          ${metricCardHTML('citaciones', 'Citaciones')}
        </div>
      </div>`;
    cmdChips.style.display = 'flex';
    loadDashboardMetrics();
  }

  function renderMessages() {
    const conv = getActive();
    if (!conv || !conv.messages.length) { showWelcome(); return; }
    mainEl.classList.add('chat-mode');
    chatArea.innerHTML = '';
    cmdChips.style.display = 'none';
    for (const m of conv.messages) {
      if (m.role === 'user')      appendUserBubble(m.content);
      else if (m.role === 'assistant') appendAssistantBubble(m.content);
    }
    scrollBottom();
  }

  // ── Estado de "generando" ─────────────────────────
  const SVG_ENVIAR  = '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M10 2l8 16-8-4-8 4z"/></svg>';
  const SVG_DETENER = '<svg viewBox="0 0 20 20" fill="currentColor"><rect x="5" y="5" width="10" height="10" rx="2"/></svg>';

  function setStreaming(activo) {
    streaming = activo;
    if (activo) {
      sendBtn.disabled  = false;             // habilitado, pero ahora detiene
      sendBtn.innerHTML = SVG_DETENER;
      sendBtn.title     = 'Detener la respuesta';
      sendBtn.classList.add('is-stop');
    } else {
      abortCtl = null;
      sendBtn.innerHTML = SVG_ENVIAR;
      sendBtn.title     = 'Enviar';
      sendBtn.classList.remove('is-stop');
      sendBtn.disabled  = !msgInput.value.trim();
    }
  }

  function detenerRespuesta() {
    if (abortCtl) abortCtl.abort();
  }

  // ── Chat send ─────────────────────────────────────
  async function send(textOverride, sectorOverride) {
    let text = (textOverride || msgInput.value).trim();
    if (!text || streaming) return;

    const isResumen = text === '__RESUMEN_SEMANAL__';
    const sector    = sectorOverride || null;

    // Create conversation if none active
    if (!activeId || !getActive()) newChat();
    const conv = getActive();

    // First message → auto-title. El resumen manda un centinela como texto, así
    // que sin este caso el título del chat quedaba como "__RESUMEN_SEMANAL__".
    if (!conv.messages.length) {
      conv.title = isResumen
        ? 'Resumen Semanal' + (sector && sector !== 'general' ? ' — ' + sector : '')
        : autoTitle(text);
    }

    mainEl.classList.add('chat-mode');
    chatArea.querySelector('.welcome')?.remove();
    cmdChips.style.display = 'none';

    const userContent = isResumen
      ? `__RESUMEN_SEMANAL__${sector ? ':' + sector : ''}`
      : text;
    conv.messages.push({ role: 'user', content: userContent });
    saveConvs();
    renderSidebar();
    appendUserBubble(isResumen
      ? `📄 Resumen ejecutivo${sector && sector !== 'general' ? ' — ' + sector : ''}`
      : text);

    msgInput.value = '';
    msgInput.style.height = 'auto';
    abortCtl = new AbortController();
    setStreaming(true);
    const assistantEl = appendAssistantTyping();
    let fullText   = '';
    let toolCalled = false;  // true si el modelo consultó una herramienta

    try {
      const resp = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: conv.messages }),
        signal: abortCtl.signal,
      });

      const reader  = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop();

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const raw = line.slice(6).trim();
          if (raw === '[DONE]') continue;
          try {
            const obj = JSON.parse(raw);
            if (obj.error)  { renderContent(assistantEl, `**Error:** ${obj.error}`); break; }
            if (obj.status) {
              toolCalled = true;
              assistantEl.innerHTML = `<span class="fetch-status">${escHtml(obj.status)}</span>`;
              scrollBottom();
            }
            if (obj.text)   {
              fullText += obj.text;
              renderContent(assistantEl, fullText);
              scrollBottom();
            }
          } catch { /* ignore */ }
        }
      }
    } catch (err) {
      if (err.name === 'AbortError') {
        // Cortada a propósito: lo que ya llegó se conserva, no es un error.
        renderContent(assistantEl, fullText
          ? fullText + '\n\n_(respuesta detenida)_'
          : '_Respuesta detenida._');
      } else {
        renderContent(assistantEl, `**Error de conexión:** ${err.message}`);
      }
    } finally {
      // Ojo: esto va sí o sí en el finally. Si `streaming` se queda en true
      // (por ej. si saveConvs revienta con el localStorage lleno), los chips
      // y el botón de enviar dejan de responder para siempre, sin ningún error
      // visible, hasta recargar la ventana.
      try {
        if (fullText) {
          conv.messages.push({ role: 'assistant', content: fullText });
          saveConvs();
          addCopyBtns(assistantEl);
          addMessageCopyBtns(assistantEl.closest('.message'), fullText);
          if (toolCalled && hasExportableContent(fullText)) {
            addExportBtns(assistantEl.closest('.message'), fullText);
          }
        }
      } catch (e) {
        console.error('Error al guardar/renderizar la respuesta:', e);
      }

      setStreaming(false);
      scrollBottom();
    }
  }

  // ── DOM helpers ───────────────────────────────────
  const SVG_USER = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/></svg>`;
  const SVG_BOT  = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="20" width="18" height="2" rx=".5"/><rect x="5" y="9" width="3" height="11" rx=".5"/><rect x="10.5" y="9" width="3" height="11" rx=".5"/><rect x="16" y="9" width="3" height="11" rx=".5"/><path d="M1 9h22M12 2L1 9h22L12 2z"/></svg>`;

  function appendUserBubble(text) {
    const d = document.createElement('div');
    d.className = 'message user';
    d.innerHTML = `<div class="msg-avatar">${SVG_USER}</div><div class="msg-content">${escHtml(text)}</div>`;
    chatArea.appendChild(d);
    scrollBottom();
  }

  function appendAssistantTyping() {
    const d = document.createElement('div');
    d.className = 'message assistant';
    d.innerHTML = `
      <div class="msg-avatar">${SVG_BOT}</div>
      <div class="msg-content">
        <span class="typing-dot"></span>
        <span class="typing-dot"></span>
        <span class="typing-dot"></span>
      </div>`;
    chatArea.appendChild(d);
    scrollBottom();
    return d.querySelector('.msg-content');
  }

  function appendAssistantBubble(md) {
    const d = document.createElement('div');
    d.className = 'message assistant';
    d.innerHTML = `<div class="msg-avatar">${SVG_BOT}</div><div class="msg-content"></div>`;
    chatArea.appendChild(d);
    const el = d.querySelector('.msg-content');
    renderContent(el, md);
    addCopyBtns(el);
    addMessageCopyBtns(d, md);
    addExportBtns(d, md);
    return el;
  }

  function renderContent(el, md) {
    el.innerHTML = parseMarkdown(md);
  }

  // ── Modal de resumen ─────────────────────────────
  const resumenModal = document.getElementById('resumen-modal');

  function openResumenModal() {
    resumenModal.style.display = 'flex';
  }
  function closeResumenModal() {
    resumenModal.style.display = 'none';
  }

  document.getElementById('resumen-cancel').addEventListener('click', closeResumenModal);
  resumenModal.addEventListener('click', e => {
    if (e.target === resumenModal) closeResumenModal();
  });

  // Cada botón de sector dispara la generación del resumen con ese contexto
  resumenModal.querySelectorAll('.sector-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const sector = btn.dataset.sector;
      closeResumenModal();
      send('__RESUMEN_SEMANAL__', sector);
    });
  });


  // Devuelve true si el texto tiene contenido exportable (tablas, listas de datos)
  function hasExportableContent(md) {
    return md.includes('|') && md.includes('---');  // markdown table
  }

  // Botones de descarga que aparecen DESPUÉS de que termina el resumen
  function addExportBtns(msgDiv, md) {
    const wrap = document.createElement('div');
    wrap.className = 'export-btns';
    wrap.innerHTML = `
      <button class="export-btn" data-type="pdf">PDF</button>
      <button class="export-btn" data-type="word">Word</button>`;
    const pdfBtn  = wrap.querySelector('[data-type="pdf"]');
    const wordBtn = wrap.querySelector('[data-type="word"]');
    pdfBtn._orig  = pdfBtn.innerHTML;
    wordBtn._orig = wordBtn.innerHTML;
    pdfBtn.addEventListener('click',  () => exportPdf(md,  pdfBtn));
    wordBtn.addEventListener('click', () => exportWord(md, wordBtn));
    const content = msgDiv.querySelector('.msg-content') || msgDiv;
    content.appendChild(wrap);
  }

  function _buildPrintHtml(md) {
    const html = parseMarkdown(md);
    const date = new Date().toLocaleDateString('es-PE', { day:'numeric', month:'long', year:'numeric' });
    return `<!DOCTYPE html><html lang="es"><head>
<meta charset="UTF-8"><title>Resumen Ejecutivo — Congreso del Perú</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Georgia',serif;font-size:12pt;color:#111;padding:40px 60px;line-height:1.7;max-width:900px;margin:0 auto}
  h1{font-size:18pt;font-weight:bold;margin-bottom:4px}
  h2{font-size:13pt;font-weight:bold;margin:24px 0 8px;border-bottom:1.5px solid #111;padding-bottom:4px}
  h3{font-size:12pt;font-weight:bold;margin:16px 0 6px}
  p{margin-bottom:10px} ul,ol{padding-left:20px;margin-bottom:10px} li{margin-bottom:4px}
  table{border-collapse:collapse;width:100%;margin:12px 0;font-size:10pt}
  th{background:#111;color:#fff;padding:6px 10px;text-align:left;font-weight:bold}
  td{border:1px solid #ccc;padding:6px 10px}
  tr:nth-child(even) td{background:#f7f7f7}
  a{color:#111} hr{border:none;border-top:1px solid #ccc;margin:20px 0}
  .ftr{margin-top:40px;border-top:1px solid #ccc;padding-top:12px;font-size:9pt;color:#666}
  strong{font-weight:bold} em{font-style:italic}
  @media print{body{padding:20px 30px} a{text-decoration:none}}
</style></head><body>
${html}
<div class="ftr">Generado por Diana — Sistema de Monitoreo Parlamentario · ${date}</div>
</body></html>`;
  }

  function exportPdf(md, btn) {
    const html = _buildPrintHtml(md);
    if (window.electronAPI) {
      if (btn) { btn.disabled = true; btn.textContent = 'Generando…'; }
      window.electronAPI.exportPDF(html)
        .then(r => { if (btn) { btn.disabled = false; btn.innerHTML = btn._orig; } })
        .catch(e => { alert('Error al generar PDF: ' + e.message); if (btn) { btn.disabled = false; btn.innerHTML = btn._orig; } });
    } else {
      const w = window.open('', '_blank');
      w.document.write(html);
      w.document.close();
      setTimeout(() => w.print(), 400);
    }
  }

  function exportWord(md, btn) {
    if (window.electronAPI) {
      if (btn) { btn.disabled = true; btn.textContent = 'Generando…'; }
      window.electronAPI.exportWord(md)
        .then(r => { if (btn) { btn.disabled = false; btn.innerHTML = btn._orig; } })
        .catch(e => { alert('Error al generar Word: ' + e.message); if (btn) { btn.disabled = false; btn.innerHTML = btn._orig; } });
    } else {
      fetch('/export/docx', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: md }),
      })
      .then(r => r.blob())
      .then(blob => {
        const url = URL.createObjectURL(blob);
        const a   = document.createElement('a');
        a.href     = url;
        a.download = `Resumen-Congreso-${new Date().toISOString().slice(0,10)}.docx`;
        a.click();
        URL.revokeObjectURL(url);
      })
      .catch(() => alert('Error generando el Word.'));
    }
  }

  function scrollBottom() {
    chatArea.scrollTop = chatArea.scrollHeight;
  }

  function addCopyBtns(el) {
    el.querySelectorAll('table').forEach(table => {
      const wrap = document.createElement('div');
      wrap.className = 'table-wrap';
      table.parentNode.insertBefore(wrap, table);
      wrap.appendChild(table);
      const btn = document.createElement('button');
      btn.className = 'copy-btn';
      btn.textContent = 'Copiar tabla';
      btn.addEventListener('click', () => {
        // HTML rico: Word lo pega como tabla con formato completo
        const htmlTable = `
<html><body>
<style>
  table{border-collapse:collapse;font-family:Calibri,sans-serif;font-size:11pt}
  th{background:#1a1a1a;color:#fff;padding:6px 12px;font-weight:bold;border:1px solid #333}
  td{padding:5px 12px;border:1px solid #ccc}
  tr:nth-child(even) td{background:#f5f5f5}
</style>
${table.outerHTML}
</body></html>`;

        // Texto plano como fallback (TSV pega como tabla en Word también)
        const rows = Array.from(table.querySelectorAll('tr'));
        const tsv  = rows.map(r =>
          Array.from(r.querySelectorAll('th,td')).map(c => c.textContent.trim()).join('\t')
        ).join('\n');

        if (window.ClipboardItem) {
          const item = new ClipboardItem({
            'text/html':  new Blob([htmlTable], { type: 'text/html' }),
            'text/plain': new Blob([tsv],       { type: 'text/plain' }),
          });
          navigator.clipboard.write([item]).catch(() => fallbackCopy(tsv));
        } else {
          fallbackCopy(tsv);
        }

        btn.textContent = '✓ Copiado';
        setTimeout(() => { btn.textContent = 'Copiar tabla'; }, 2000);
      });
      wrap.appendChild(btn);
    });
  }

  function addMessageCopyBtns(msgDiv, fullText) {
    // Separar contenido principal de fuentes
    const sepIdx = fullText.search(/\n---\n\*\*Fuentes:/);
    const mainText    = sepIdx >= 0 ? fullText.slice(0, sepIdx).trim() : fullText.trim();
    const sourcesText = sepIdx >= 0 ? fullText.slice(sepIdx).trim() : null;

    const toolbar = document.createElement('div');
    toolbar.className = 'msg-toolbar';

    const copyBtn = document.createElement('button');
    copyBtn.className = 'msg-action-btn';
    copyBtn.textContent = 'Copiar mensaje';
    copyBtn.addEventListener('click', () => {
      navigator.clipboard.writeText(mainText).catch(() => fallbackCopy(mainText));
      copyBtn.textContent = '✓ Copiado';
      setTimeout(() => { copyBtn.textContent = 'Copiar mensaje'; }, 2000);
    });
    toolbar.appendChild(copyBtn);

    if (sourcesText) {
      const srcBtn = document.createElement('button');
      srcBtn.className = 'msg-action-btn';
      srcBtn.textContent = 'Copiar fuentes';
      srcBtn.addEventListener('click', () => {
        navigator.clipboard.writeText(sourcesText).catch(() => fallbackCopy(sourcesText));
        srcBtn.textContent = '✓ Copiado';
        setTimeout(() => { srcBtn.textContent = 'Copiar fuentes'; }, 2000);
      });
      toolbar.appendChild(srcBtn);
    }

    // Insertar el toolbar dentro del msg-content, al final
    const content = msgDiv.querySelector('.msg-content');
    if (content) content.appendChild(toolbar);
  }

  function fallbackCopy(text) {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }

  // ── Events ────────────────────────────────────────
  // ── Drag & drop PDF ───────────────────────────────
  const dropZone = document.querySelector('.main');
  dropZone.addEventListener('dragover', e => {
    e.preventDefault();
    dropZone.classList.add('drag-over');
  });
  dropZone.addEventListener('dragleave', e => {
    if (!dropZone.contains(e.relatedTarget)) dropZone.classList.remove('drag-over');
  });
  dropZone.addEventListener('drop', e => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    const file = Array.from(e.dataTransfer.files).find(f => f.name.endsWith('.pdf'));
    if (file) uploadPdfFile(file);
  });

  // ── Panel PDFs del Congreso ───────────────────────
  const congresoPanel  = document.getElementById('congreso-pdf-panel');
  const cpdfList       = document.getElementById('cpdf-list');
  let cpdfLoaded = false;

  document.getElementById('congreso-pdf-btn').addEventListener('click', async () => {
    const visible = congresoPanel.style.display !== 'none';
    congresoPanel.style.display = visible ? 'none' : 'block';
    if (!visible && !cpdfLoaded) {
      cpdfLoaded = true;
      cpdfList.innerHTML = '<div class="cpdf-loading">Cargando PDFs del Congreso...</div>';
      try {
        const res  = await fetch('/congreso-pdfs');
        const data = await res.json();
        if (!data.pdfs.length) {
          cpdfList.innerHTML = '<div class="cpdf-loading">No hay PDFs disponibles ahora.</div>';
          return;
        }
        cpdfList.innerHTML = '';
        for (const pdf of data.pdfs) {
          const el = document.createElement('div');
          el.className = 'cpdf-item';
          el.innerHTML = `<span class="cpdf-tipo">${pdf.tipo}</span><span class="cpdf-title">${escHtml(pdf.titulo)}</span>`;
          el.addEventListener('click', async () => {
            congresoPanel.style.display = 'none';
            await loadPdfFromUrl(pdf.enlace, pdf.titulo);
          });
          cpdfList.appendChild(el);
        }
      } catch {
        cpdfList.innerHTML = '<div class="cpdf-loading">Error al cargar los PDFs.</div>';
      }
    }
  });

  document.getElementById('cpdf-close').addEventListener('click', () => {
    congresoPanel.style.display = 'none';
  });

  async function loadPdfFromUrl(url, titulo) {
    if (!activeId || !getActive()) newChat();
    const conv = getActive();
    if (!conv.messages.length) conv.title = '📄 ' + titulo.slice(0, 40);

    mainEl.classList.add('chat-mode');
    chatArea.querySelector('.welcome')?.remove();
    cmdChips.style.display = 'none';
    appendUserBubble(`📄 ${titulo}`);
    const assistantEl = appendAssistantTyping();
    setStreaming(true);

    try {
      const res  = await fetch('/load-pdf-url', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error);
      const ctx = `He cargado el documento "${titulo}" (${data.pages} páginas).\n\nContenido:\n${data.text}\n\n¿Qué quieres analizar?`;
      conv.messages.push({ role: 'user', content: ctx });
      saveConvs(); renderSidebar();
      const msg = `PDF cargado — **${titulo.slice(0, 60)}** (${data.pages} págs.). ¿Qué quieres analizar?`;
      renderContent(assistantEl, msg);
      addMessageCopyBtns(assistantEl.closest('.message'), msg);
    } catch (err) {
      renderContent(assistantEl, `**Error al cargar el PDF:** ${err.message}`);
    }
    setStreaming(false); scrollBottom();
  }

  // ── PDF upload (archivo local) ────────────────────
  async function uploadPdfFile(file) {
    if (!activeId || !getActive()) newChat();
    const conv = getActive();
    if (!conv.messages.length) conv.title = '📄 ' + file.name;
    mainEl.classList.add('chat-mode');
    chatArea.querySelector('.welcome')?.remove();
    cmdChips.style.display = 'none';
    appendUserBubble(`📄 ${file.name}`);
    const assistantEl = appendAssistantTyping();
    setStreaming(true);
    try {
      const form = new FormData();
      form.append('file', file);
      const res  = await fetch('/upload-pdf', { method: 'POST', body: form });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || 'Error al leer el PDF');
      const ctx = `He cargado el documento "${file.name}" (${data.pages} páginas).\n\nContenido:\n${data.text}\n\n¿Qué quieres analizar?`;
      conv.messages.push({ role: 'user', content: ctx });
      saveConvs(); renderSidebar();
      const msg = `PDF cargado — **${file.name}** (${data.pages} págs.). ¿Qué quieres analizar?`;
      renderContent(assistantEl, msg);
      addMessageCopyBtns(assistantEl.closest('.message'), msg);
    } catch (err) {
      renderContent(assistantEl, `**Error al cargar el PDF:** ${err.message}`);
    }
    setStreaming(false); scrollBottom();
  }

  document.getElementById('pdf-input').addEventListener('change', e => {
    const file = e.target.files[0];
    e.target.value = '';
    if (file) uploadPdfFile(file);
  });

  newChatBtn.addEventListener('click', newChat);
  newChatWideBtn.addEventListener('click', newChat);

  sendBtn.addEventListener('click', () => (streaming ? detenerRespuesta() : send()));

  msgInput.addEventListener('input', () => {
    msgInput.style.height = 'auto';
    msgInput.style.height = Math.min(msgInput.scrollHeight, 160) + 'px';
    sendBtn.disabled = !msgInput.value.trim();
  });

  msgInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });

  // ── Placeholder animado — sugiere preguntas sobre el Congreso ──
  const TW_PHRASES = [
    '¿Qué proyectos de ley se presentaron esta semana en Diputados?',
    '¿Cuál es la agenda de comisiones para mañana en el Senado?',
    'Resume las sesiones plenarias más recientes del Congreso',
    '¿Qué dictámenes están agendados en el Pleno esta semana?',
    '¿Cuál es el estatus del proyecto de ley N° 1234/2024-CR?',
    'Dame los destacados y citaciones más recientes del Congreso',
    '¿Qué comisiones sesionan esta semana y quiénes las integran?',
    'Resume las novedades legislativas de los últimos 7 días',
  ];
  let twTimer = null;
  function startTypewriter() {
    clearTimeout(twTimer);
    let pi = 0, ci = 0, deleting = false;
    function tick() {
      if (msgInput.value) { twTimer = setTimeout(tick, 600); return; } // pausa si el usuario está escribiendo
      const phrase = TW_PHRASES[pi];
      if (!deleting) {
        ci++;
        msgInput.placeholder = phrase.slice(0, ci);
        if (ci === phrase.length) { twTimer = setTimeout(() => { deleting = true; tick(); }, 2200); return; }
        twTimer = setTimeout(tick, 42 + Math.random() * 28);
      } else {
        ci--;
        msgInput.placeholder = phrase.slice(0, ci);
        if (ci === 0) {
          deleting = false;
          pi = (pi + 1) % TW_PHRASES.length;
          twTimer = setTimeout(tick, 380);
          return;
        }
        twTimer = setTimeout(tick, 22);
      }
    }
    twTimer = setTimeout(tick, 1200);
  }
  startTypewriter();

  document.querySelectorAll('.chip').forEach(btn => {
    if (btn.id === 'resumen-btn') return;
    btn.addEventListener('click', () => { if (!streaming) send(btn.dataset.cmd); });
  });

  document.getElementById('resumen-btn').addEventListener('click', () => {
    if (!streaming) openResumenModal();
  });



  // ── Markdown parser ───────────────────────────────
  // marked + DOMPurify: más robusto que el parser manual y XSS-safe.
  // El guard importa: si alguna de las dos librerías no cargó, una excepción
  // acá aborta el resto del script y deja sin cablear los listeners de más
  // abajo (navegación), con la app aparentemente viva pero sin botones.
  const MD_OK = typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined';
  if (MD_OK) {
    marked.setOptions({ breaks: true, gfm: true });
  } else {
    console.error('marked/DOMPurify no cargaron — se muestra el texto sin formato.');
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function parseMarkdown(md) {
    // Degradar a texto plano escapado antes que romper el chat.
    if (!MD_OK) return `<p>${escapeHtml(md).replace(/\n/g, '<br>')}</p>`;
    const raw = marked.parse(md);
    return DOMPurify.sanitize(raw, {
      ADD_ATTR: ['target', 'rel'],
      ALLOWED_TAGS: ['p','br','h1','h2','h3','h4','h5','h6',
        'strong','em','code','pre','blockquote',
        'table','thead','tbody','tr','th','td',
        'ul','ol','li','a','hr','span'],
    });
  }

  function escHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ── Navegación Chat / Live / PDFs ────────────────
  const navChat    = document.getElementById('nav-chat');
  const navLive    = document.getElementById('nav-live');
  const navPdfs    = document.getElementById('nav-pdfs');
  const viewLive   = document.getElementById('view-live');
  const viewPdfs   = document.getElementById('view-pdfs');
  const viewExp    = document.getElementById('view-expediente');
  const liveIframe = document.getElementById('live-iframe');
  const pdfsIframe = document.getElementById('pdfs-iframe');
  const expIframe  = document.getElementById('expediente-iframe');
  const chatArea2  = document.getElementById('chat-area');
  const inputArea  = document.querySelector('.input-area');

  function setNavActive(btn) {
    [navChat, navLive, navPdfs].forEach(b => b && b.classList.remove('active'));
    if (btn) btn.classList.add('active');
  }

  function switchToChat() {
    setNavActive(navChat);
    if (viewLive) viewLive.style.display = 'none';
    if (viewPdfs) viewPdfs.style.display = 'none';
    if (viewExp)  viewExp.style.display  = 'none';
    chatArea2.style.display = '';
    inputArea.style.display = '';
  }

  // ── Vista de expediente ──────────────────────────
  // Se abre al clickear el número/sumilla de una proposición en una respuesta
  // del chat. Antes ese link salía al portal del Congreso en el navegador
  // (main.js lo mandaba a shell.openExternal): se perdía el hilo y había que
  // volver a la app a mano.
  function switchToExpediente(numero) {
    setNavActive(null);
    chatArea2.style.display = 'none';
    inputArea.style.display = 'none';
    if (viewLive) viewLive.style.display = 'none';
    if (viewPdfs) viewPdfs.style.display = 'none';
    viewExp.style.display = '';

    // Recargar siempre: a diferencia de Live y PDFs, esta vista depende del
    // proyecto pedido, así que reusar el iframe mostraría el expediente
    // anterior.
    const destino = `/expediente-view?numero=${encodeURIComponent(numero)}`;
    if (expIframe.dataset.numero !== numero) {
      expIframe.dataset.numero = numero;
      expIframe.src = destino;
    }
  }

  // Los iframes arrancan en about:blank a propósito. Con src="" el navegador
  // resuelve al documento actual, así que cada uno cargaba LA APP ENTERA
  // dentro de sí mismo: tres copias de app.js corriendo a la vez, tres
  // llamadas a /dashboard-metrics y las tarjetas del panel duplicadas.
  const necesitaCarga = (f) =>
    !f.src || f.src === 'about:blank' || f.src === window.location.href;

  function switchToLive() {
    setNavActive(navLive);
    chatArea2.style.display = 'none';
    inputArea.style.display = 'none';
    if (viewPdfs) viewPdfs.style.display = 'none';
    if (viewExp)  viewExp.style.display  = 'none';
    viewLive.style.display = '';
    if (necesitaCarga(liveIframe)) liveIframe.src = '/live';
  }

  function switchToPdfs() {
    setNavActive(navPdfs);
    chatArea2.style.display = 'none';
    inputArea.style.display = 'none';
    if (viewLive) viewLive.style.display = 'none';
    if (viewExp)  viewExp.style.display  = 'none';
    viewPdfs.style.display = '';
    if (necesitaCarga(pdfsIframe)) pdfsIframe.src = '/pdfs';
  }

  // Los links a proposiciones que el modelo escribe en sus respuestas
  // (workflow_proyectos.md los arma como `[numero](enlace)`) apuntan al portal
  // del Congreso. Interceptarlos acá los abre en la ficha interna; si el link
  // es otra cosa, se deja pasar y sigue yendo al navegador como antes.
  //
  // El número sale del TEXTO del link, no de la URL: la URL del portal lleva
  // el `pleyNum` interno de SPLEY, mientras que /expediente necesita el número
  // público (00088-2026-2031-CD), que es justo lo que el link muestra.
  const RE_NUM_PROYECTO = /^\d{1,6}[-/]\d{4}(?:-\d{4})?-[A-Z]{1,3}$/i;

  document.addEventListener('click', (e) => {
    const a = e.target.closest('a[href]');
    if (!a || !a.href.includes('spley-portal')) return;

    const numero = (a.textContent || '').trim();
    if (!RE_NUM_PROYECTO.test(numero)) return;   // ej. "Ver en SPLEY" → al navegador

    e.preventDefault();
    e.stopPropagation();
    switchToExpediente(numero);
  });

  navChat.addEventListener('click', switchToChat);
  if (navLive) navLive.addEventListener('click', switchToLive);
  if (navPdfs) navPdfs.addEventListener('click', switchToPdfs);

  window.addEventListener('message', (e) => {
    if (e.data === 'close-live') { switchToChat(); return; }
    if (e.data === 'close-expediente') { switchToChat(); return; }
    if (e.data && e.data.type === 'load-pdf') {
      switchToChat();
      loadPdfFromUrl(e.data.url, e.data.titulo);
    }
    if (e.data && e.data.type === 'query-proyecto') {
      switchToChat();
      const query = `Dame información sobre el proyecto de ley ${e.data.numero}: estado actual, autores, y de qué trata.`;
      send(query);
    }
    if (e.data && e.data.type === 'open-external') {
      window.electronAPI?.openExternal(e.data.url);
    }
    // Puente de exportación: los iframes (Live, PDFs) no tienen acceso a
    // window.electronAPI ni pueden abrir ventanas (setWindowOpenHandler las
    // deniega), así que delegan la exportación a este frame de nivel superior.
    if (e.data && e.data.type === 'export-pdf') {
      const { html, reqId } = e.data;
      if (window.electronAPI) {
        window.electronAPI.exportPDF(html)
          .then(() => e.source.postMessage({ type: 'export-done', reqId }, '*'))
          .catch(err => e.source.postMessage({ type: 'export-error', reqId, message: String(err) }, '*'));
      } else {
        const w = window.open('', '_blank');
        if (w) { w.document.write(html); w.document.close(); setTimeout(() => w.print(), 400); }
        e.source.postMessage({ type: 'export-done', reqId }, '*');
      }
    }
    if (e.data && e.data.type === 'export-word') {
      const { content, filename, reqId } = e.data;
      if (window.electronAPI) {
        window.electronAPI.exportWord(content)
          .then(() => e.source.postMessage({ type: 'export-done', reqId }, '*'))
          .catch(err => e.source.postMessage({ type: 'export-error', reqId, message: String(err) }, '*'));
      } else {
        fetch('/export/docx', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content }),
        })
          .then(r => r.blob())
          .then(blob => {
            const url = URL.createObjectURL(blob);
            const a   = document.createElement('a');
            a.href     = url;
            a.download = `${filename}.docx`;
            a.click();
            URL.revokeObjectURL(url);
            e.source.postMessage({ type: 'export-done', reqId }, '*');
          })
          .catch(err => e.source.postMessage({ type: 'export-error', reqId, message: String(err) }, '*'));
      }
    }
  });

  // ── Init ──────────────────────────────────────────
  async function init() {
    // Intentar cargar desde archivo en disco (más confiable que localStorage)
    if (window.electronAPI?.loadHistory) {
      try {
        const fromFile = await window.electronAPI.loadHistory();
        const valid = Array.isArray(fromFile) && fromFile.length > 0
          && fromFile.every(c => c && typeof c.id === 'string' && Array.isArray(c.messages));
        if (valid) {
          convs = fromFile;
          // Sincronizar también a localStorage
          localStorage.setItem(STORE, JSON.stringify(convs));
        } else {
          loadConvs(); // fallback a localStorage
        }
      } catch {
        loadConvs();
      }
    } else {
      loadConvs();
    }

    // Arrancar siempre en la pantalla principal: no reabrir la última
    // conversación automáticamente. El historial sigue en el sidebar, un
    // clic la reabre.
    renderSidebar();
    showWelcome();
  }

  init();
})();
