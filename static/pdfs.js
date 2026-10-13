/* ── Tema (sincronizado en vivo con el chat principal) ──── */
window.addEventListener("message", (e) => {
  if (e.origin !== location.origin) return;   // solo mensajes de la propia app
  if (e.data && e.data.type === "theme") {
    document.documentElement.dataset.theme = e.data.dark ? "dark" : "light";
  }
});

let pdfs = [];
let selected = null;

function normalizePdf(p) {
  const t = (p.tipo || '').toLowerCase();
  let tipo = 'destacado';
  if (t.startsWith('cit')) tipo = 'citacion';
  else if (t.includes('proyecto') || t.includes('ley')) tipo = 'proyecto';
  else if (t.includes('ref')) tipo = 'referencia';
  else if (t.includes('com')) tipo = 'comision';
  else if (t.includes('ses') || t.includes('plen')) tipo = 'sesion';
  else if (t.includes('agenda')) tipo = 'agenda';
  else if (t.includes('doc')) tipo = 'documento';
  return { titulo: p.titulo, url: p.enlace || p.url || '', tipo };
}

async function loadPdfs() {
  const list = document.getElementById('p-list');
  const btn = document.getElementById('refresh-btn');
  btn.classList.add('spinning');
  list.innerHTML = `<div class="p-loading"><span class="dot"></span><span class="dot"></span><span class="dot"></span><span>Cargando documentos...</span></div>`;
  hidePanel();

  try {
    // Carga rápida: PDFs del congreso + referencias
    const res = await fetch('/congreso-pdfs');
    const data = await res.json();
    const raw = Array.isArray(data) ? data : (data.pdfs || []);
    pdfs = raw.map(normalizePdf);
    renderList();
    btn.classList.remove('spinning');

    // Carga en segundo plano: proyectos SPLEY
    fetch('/congreso-proyectos')
      .then(r => r.json())
      .then(d => {
        const extra = (Array.isArray(d) ? d : (d.pdfs || [])).map(normalizePdf);
        if (extra.length) {
          pdfs = [...pdfs, ...extra];
          renderList();
        }
      })
      .catch(() => {});
  } catch (e) {
    list.innerHTML = `<div class="p-no-items">No se pudo cargar la lista.<br>Verifica la conexión.</div>`;
    btn.classList.remove('spinning');
  }
}

function renderList() {
  const list = document.getElementById('p-list');
  if (!pdfs.length) {
    list.innerHTML = `<div class="p-no-items">No hay documentos disponibles en este momento.</div>`;
    return;
  }

  const GROUPS = [
    { tipo: 'destacado',  label: 'Destacados' },
    { tipo: 'citacion',   label: 'Citaciones' },
    { tipo: 'sesion',     label: 'Sesiones / Pleno' },
    { tipo: 'comision',   label: 'Comisiones' },
    { tipo: 'agenda',     label: 'Agenda' },
    { tipo: 'proyecto',   label: 'Proyectos de Ley recientes' },
    { tipo: 'referencia', label: 'Documentos de referencia' },
    { tipo: 'documento',  label: 'Otros documentos' },
  ];
  let html = '';
  let globalIdx = 0;

  for (const g of GROUPS) {
    const items = pdfs.filter(p => p.tipo === g.tipo);
    if (!items.length) continue;
    html += `<div class="p-section-label">${g.label}</div>`;
    for (const p of items) {
      const actualIdx = pdfs.indexOf(p);
      html += itemHtml(p, actualIdx);
      globalIdx++;
    }
  }

  list.innerHTML = html;
  list.querySelectorAll('.p-item').forEach(el => {
    el.addEventListener('click', () => selectPdf(parseInt(el.dataset.idx)));
  });
}

const TIPO_LABELS = {
  destacado: 'Destacado', citacion: 'Citación', comision: 'Comisión',
  sesion: 'Sesión', agenda: 'Agenda', documento: 'Documento',
  referencia: 'Referencia', proyecto: 'Proyecto de Ley',
};

function itemHtml(p, idx) {
  return `<div class="p-item ${p.tipo}" data-idx="${idx}">
    <div class="p-item-icon">
      <svg viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round">
        <path d="M4 4a2 2 0 0 1 2-2h5l5 5v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4z"/>
        <path d="M11 2v5h5"/>
      </svg>
    </div>
    <div class="p-item-text">
      <div class="p-item-nombre">${escHtml(p.titulo)}</div>
      <div class="p-item-tipo">${TIPO_LABELS[p.tipo] || p.tipo}</div>
    </div>
  </div>`;
}

function selectPdf(idx) {
  selected = pdfs[idx];

  // mark active
  document.querySelectorAll('.p-item').forEach(el => {
    el.classList.toggle('active', el.dataset.idx === String(idx));
  });

  // show panel
  const panel  = document.getElementById('p-panel');
  const empty  = document.getElementById('p-empty');
  const badge  = document.getElementById('p-badge');
  const title  = document.getElementById('p-doc-title');
  const link   = document.getElementById('p-pdf-link');
  const loadBtn = document.getElementById('p-load-btn');

  empty.style.display = 'none';
  panel.style.display = 'flex';

  badge.className = 'p-badge ' + selected.tipo;
  badge.textContent = TIPO_LABELS[selected.tipo] || selected.tipo;
  title.textContent = selected.titulo;

  const isProyecto = selected.tipo === 'proyecto';

  // Vista previa
  loadPreview(selected.url, isProyecto);

  if (selected.url) {
    link.textContent = isProyecto ? 'Ver en SPLEY' : 'Ver PDF';
    link.style.display = 'inline-flex';
    link.onclick = (ev) => {
      ev.preventDefault();
      window.parent.postMessage({ type: 'open-external', url: selected.url }, location.origin);
    };
  } else {
    link.style.display = 'none';
    link.onclick = null;
  }

  loadBtn.disabled = false;
  loadBtn.classList.remove('loading');
  const loadLabel = isProyecto ? 'Preguntar a Diana' : 'Analizar con Diana';
  loadBtn.innerHTML = `<svg viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="2" y="2" width="14" height="14" rx="2"/><path d="M5 6h8M5 9h8M5 12h5"/></svg> ${loadLabel}`;

  loadBtn.onclick = () => {
    if (isProyecto) {
      // Extract project number from title for a chat query
      const match = selected.titulo.match(/\[([^\]]+)\]/);
      const num = match ? match[1] : selected.titulo.slice(0, 40);
      window.parent.postMessage({ type: 'query-proyecto', numero: num, titulo: selected.titulo }, location.origin);
    } else {
      cargarAlChat(selected);
    }
  };
}

function loadPreview(url, isProyecto) {
  const wrap    = document.getElementById('p-preview-wrap');
  const loading = document.getElementById('p-preview-loading');
  const img     = document.getElementById('p-preview-img');
  const ph      = document.getElementById('p-preview-placeholder');

  // Reset — NO usar img.src='' porque en Chromium dispara onerror
  img.onload  = null;
  img.onerror = null;
  img.removeAttribute('src');
  loading.style.display = 'flex';
  img.style.display     = 'none';
  ph.style.display      = 'none';

  if (isProyecto || !url || !url.toLowerCase().endsWith('.pdf')) {
    loading.style.display = 'none';
    ph.style.display      = 'flex';
    return;
  }

  const thumbUrl = `/pdf-thumbnail?url=${encodeURIComponent(url)}`;
  img.onload = () => {
    loading.style.display = 'none';
    ph.style.display      = 'none';
    img.style.display     = 'block';
  };
  img.onerror = () => {
    loading.style.display = 'none';
    ph.style.display      = 'flex';
  };
  img.src = thumbUrl;

  // Lightbox on click
  img.onclick = () => {
    const lb = document.createElement('div');
    lb.className = 'p-lightbox';
    lb.innerHTML = `<img src="${thumbUrl}" alt="Vista previa">`;
    lb.addEventListener('click', () => lb.remove());
    document.body.appendChild(lb);
  };
}

function hidePanel() {
  document.getElementById('p-panel').style.display = 'none';
  document.getElementById('p-empty').style.display = 'flex';
  selected = null;
}

function cargarAlChat(pdf) {
  const btn = document.getElementById('p-load-btn');
  btn.disabled = true;
  btn.classList.add('loading');
  btn.innerHTML = `<svg viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M1 9a8 8 0 1 0 1.6-4.8"/><path d="M1 4v4h4"/></svg> Cargando…`;

  window.parent.postMessage({
    type: 'load-pdf',
    url: pdf.url,
    titulo: pdf.titulo
  }, location.origin);
}

function escHtml(str) {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

document.getElementById('refresh-btn').addEventListener('click', loadPdfs);
loadPdfs();
