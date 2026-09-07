(() => {
  const list       = document.getElementById('s-list');
  const loading    = document.getElementById('s-loading');
  const empty      = document.getElementById('s-empty');
  const panel      = document.getElementById('s-panel');
  const badge      = document.getElementById('s-video-badge');
  const title      = document.getElementById('s-video-title');
  const metaEl     = document.getElementById('s-video-meta');
  const ytLink     = document.getElementById('s-yt-link');
  const resumirBtn = document.getElementById('s-resumir-btn');
  const resumirLbl = document.getElementById('s-resumir-label');
  const result     = document.getElementById('s-result');
  const refreshBtn = document.getElementById('refresh-btn');

  let selected = null;

  // ── Cargar lista ──────────────────────────────────
  async function loadVideos() {
    loading.style.display = 'flex';
    list.innerHTML = '';
    list.appendChild(loading);
    empty.style.display = 'flex';
    panel.style.display  = 'none';
    selected = null;
    refreshBtn.classList.add('spinning');

    try {
      const r    = await fetch('/sesiones/videos');
      const data = await r.json();
      loading.style.display = 'none';
      refreshBtn.classList.remove('spinning');

      if (!data.ok || !data.videos?.length) {
        list.innerHTML = '<div style="padding:20px;font-size:13px;color:#888">No se pudieron cargar las transmisiones.</div>';
        return;
      }

      // Lives primero
      const sorted = [...data.videos].sort((a, b) => (b.en_vivo ? 1 : 0) - (a.en_vivo ? 1 : 0));
      sorted.forEach(v => renderItem(v));
    } catch {
      loading.style.display = 'none';
      refreshBtn.classList.remove('spinning');
      list.innerHTML = '<div style="padding:20px;font-size:13px;color:#c00">Error al conectar con el servidor.</div>';
    }
  }

  function renderItem(v) {
    const el = document.createElement('div');
    el.className = 's-item' + (v.en_vivo ? ' s-item--live' : '');

    const badge = v.en_vivo
      ? '<span class="badge-live">🔴 EN VIVO</span>'
      : v.fue_live
        ? '<span class="badge-past">📡 LIVE</span>'
        : '';

    el.innerHTML = `
      <img class="s-thumb" src="${esc(v.thumb)}" alt="" loading="lazy" onerror="this.style.background='#ddd'">
      <div class="s-item-info">
        <div class="s-item-title">${esc(v.titulo)}</div>
        <div class="s-item-meta">
          ${badge}
          ${v.fecha    ? `<span class="s-meta-text">${esc(v.fecha)}</span>` : ''}
          ${v.duracion ? `<span class="s-meta-text">${esc(v.duracion)}</span>` : ''}
        </div>
      </div>`;
    el.addEventListener('click', () => selectVideo(v, el));
    list.appendChild(el);
  }

  // ── Seleccionar video ─────────────────────────────
  function selectVideo(v, el) {
    document.querySelectorAll('.s-item').forEach(x => x.classList.remove('selected'));
    el.classList.add('selected');
    selected = v;
    result.innerHTML = '';

    empty.style.display = 'none';
    panel.style.display  = 'flex';

    // Badge
    badge.innerHTML = v.en_vivo
      ? '<span class="badge-live">🔴 EN VIVO</span>'
      : v.fue_live
        ? '<span class="badge-past">📡 LIVE PASADO</span>'
        : '';

    title.textContent = v.titulo;
    metaEl.textContent = [v.fecha, v.duracion].filter(Boolean).join(' · ');

    // Link YouTube
    if (v.en_vivo) {
      ytLink.href = v.url;
      ytLink.style.display = 'inline-flex';
    } else {
      ytLink.style.display = 'none';
    }

    // Texto del botón
    resumirLbl.textContent = v.en_vivo ? 'Resumir lo que va hasta ahora' : 'Resumir esta sesión';
    resumirBtn.disabled = false;
  }

  // ── Resumir ───────────────────────────────────────
  resumirBtn.addEventListener('click', async () => {
    if (!selected) return;
    resumirBtn.disabled = true;
    resumirLbl.textContent = 'Analizando...';
    result.innerHTML = `<div class="s-status">
      <span class="dot"></span><span class="dot"></span><span class="dot"></span>
      <span>Obteniendo transcript...</span>
    </div>`;

    let fullText     = '';
    let rawTranscript = '';
    let rawVideoUrl   = '';
    let rawVideoTitle = '';

    try {
      const resp = await fetch('/sesiones/resumir', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ video_id: selected.id, titulo: selected.titulo, en_vivo: selected.en_vivo || false }),
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
            if (obj.error) {
              result.innerHTML = `<p style="color:#c00;font-size:13px;padding:8px 0">${esc(obj.error)}</p>`;
              break;
            }
            if (obj.transcript_raw) {
              rawTranscript = obj.transcript_raw;
              rawVideoUrl   = obj.video_url   || '';
              rawVideoTitle = obj.video_titulo || selected.titulo;
            }
            if (obj.status) {
              result.innerHTML = `<div class="s-status">
                <span class="dot"></span><span class="dot"></span><span class="dot"></span>
                <span>${esc(obj.status)}</span>
              </div>`;
            }
            if (obj.text) {
              fullText += obj.text;
              result.innerHTML = `<div class="s-md">${parseMarkdown(fullText)}</div>`;
              addTableCopyBtns();
            }
          } catch { /* ignore */ }
        }
      }
    } catch (e) {
      result.innerHTML = `<p style="color:#c00;font-size:13px;padding:8px 0">Error: ${esc(e.message)}</p>`;
    }

    if (fullText) addExportBtns(fullText, rawTranscript, rawVideoUrl, rawVideoTitle);

    resumirBtn.disabled = false;
    resumirLbl.textContent = selected?.en_vivo ? 'Actualizar resumen' : 'Resumir de nuevo';
  });

  // ── Copy tables ───────────────────────────────────
  function addTableCopyBtns() {
    result.querySelectorAll('table').forEach(tbl => {
      if (tbl.parentElement.classList.contains('tbl-wrap')) return;
      const wrap = document.createElement('div');
      wrap.className = 'tbl-wrap';
      tbl.parentNode.insertBefore(wrap, tbl);
      wrap.appendChild(tbl);
      const btn = document.createElement('button');
      btn.className = 'tbl-copy-btn';
      btn.textContent = 'Copiar';
      btn.addEventListener('click', () => {
        const htmlStr = `<html><body><style>
          table{border-collapse:collapse;font-family:Calibri,sans-serif;font-size:11pt}
          th{background:#1a1a1a;color:#fff;padding:6px 12px;font-weight:bold;border:1px solid #333}
          td{padding:5px 12px;border:1px solid #ccc}
          tr:nth-child(even) td{background:#f5f5f5}
        </style>${tbl.outerHTML}</body></html>`;
        const rows = Array.from(tbl.querySelectorAll('tr'));
        const tsv  = rows.map(r =>
          Array.from(r.querySelectorAll('th,td')).map(c => c.textContent.trim()).join('\t')
        ).join('\n');
        if (window.ClipboardItem) {
          navigator.clipboard.write([new ClipboardItem({
            'text/html':  new Blob([htmlStr], { type: 'text/html' }),
            'text/plain': new Blob([tsv],     { type: 'text/plain' }),
          })]).catch(() => navigator.clipboard.writeText(tsv));
        } else {
          navigator.clipboard.writeText(tsv);
        }
        btn.textContent = '✓ Copiado';
        setTimeout(() => { btn.textContent = 'Copiar'; }, 2000);
      });
      wrap.appendChild(btn);
    });
  }

  // ── Export PDF / Word / TXT ───────────────────────
  function addExportBtns(md, transcript, videoUrl, videoTitle) {
    const existing = result.querySelector('.s-export');
    if (existing) existing.remove();

    const wrap = document.createElement('div');
    wrap.className = 's-export';
    wrap.innerHTML = `
      <button class="s-export-btn" data-type="pdf">
        <svg viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
          <path d="M4 2h7l4 4v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z"/>
          <path d="M11 2v4h4M6 9h6M6 12h4"/>
        </svg>
        Descargar PDF
      </button>
      <button class="s-export-btn" data-type="word">
        <svg viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
          <path d="M4 2h7l4 4v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z"/>
          <path d="M11 2v4h4M5 9h3M5 12h4M5 15h6"/>
        </svg>
        Descargar Word
      </button>
      ${transcript ? `<button class="s-export-btn" data-type="txt">
        <svg viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
          <path d="M4 2h7l4 4v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z"/>
          <path d="M11 2v4h4M5 7h8M5 10h8M5 13h5"/>
        </svg>
        Transcripción (.txt)
      </button>` : ''}`;

    wrap.querySelector('[data-type="pdf"]').addEventListener('click', () => {
      if (window.electronAPI) {
        window.electronAPI.exportPDF(buildPrintHtml(md));
      } else {
        const w = window.open('', '_blank');
        w.document.write(buildPrintHtml(md));
        w.document.close();
        setTimeout(() => w.print(), 400);
      }
    });
    wrap.querySelector('[data-type="word"]').addEventListener('click', () => {
      if (window.electronAPI) {
        window.electronAPI.exportWord(md);
      } else {
        fetch('/export/docx', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: md }),
        }).then(r => r.blob()).then(blob => {
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = `Sesion-Congreso-${new Date().toISOString().slice(0,10)}.docx`;
          a.click();
        });
      }
    });

    const txtBtn = wrap.querySelector('[data-type="txt"]');
    if (txtBtn) {
      txtBtn.addEventListener('click', () => {
        const date = new Date().toLocaleDateString('es-PE', { day:'numeric', month:'long', year:'numeric' });
        const filename = (videoTitle || 'transcripcion').replace(/[^a-zA-Z0-9\-_áéíóúñÁÉÍÓÚÑ ]/g, '').trim().replace(/\s+/g, '-').slice(0, 60);
        const content = [
          `TRANSCRIPCIÓN — ${videoTitle || 'Sesión del Congreso'}`,
          `Fecha de descarga: ${date}`,
          videoUrl ? `Video: ${videoUrl}` : '',
          '',
          '─'.repeat(60),
          '',
          transcript,
        ].filter(l => l !== undefined).join('\n');

        const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `${filename}-${new Date().toISOString().slice(0,10)}.txt`;
        a.click();
        URL.revokeObjectURL(a.href);
      });
    }

    result.appendChild(wrap);
  }

  function buildPrintHtml(md) {
    const date = new Date().toLocaleDateString('es-PE', { day:'numeric', month:'long', year:'numeric' });
    return `<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
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
  .ftr{margin-top:40px;border-top:1px solid #ccc;padding-top:12px;font-size:9pt;color:#666;text-align:center}
</style></head><body>
${parseMarkdown(md)}
<div class="ftr">Diana — Sistema de Monitoreo Parlamentario · ${date}</div>
</body></html>`;
  }

  // ── Markdown parser (simple) ──────────────────────
  function parseMarkdown(md) {
    let h = md;
    h = h.replace(/^\|(.+)\|\s*\n\|[-| :]+\|\s*\n((?:\|.+\|\s*\n?)*)/gm, (_, hdr, body) => {
      const ths  = hdr.split('|').filter(s => s.trim()).map(s => `<th>${esc(s.trim())}</th>`).join('');
      const rows = body.trim().split('\n').filter(Boolean).map(row => {
        const cells = row.split('|').slice(1, -1).map(s => `<td>${esc(s.trim())}</td>`).join('');
        return `<tr>${cells}</tr>`;
      }).join('');
      return `<table><thead><tr>${ths}</tr></thead><tbody>${rows}</tbody></table>`;
    });
    h = h.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    h = h.replace(/^## (.+)$/gm,  '<h2>$1</h2>');
    h = h.replace(/^# (.+)$/gm,   '<h1>$1</h1>');
    h = h.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
    h = h.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    h = h.replace(/\*(.+?)\*/g,     '<em>$1</em>');
    h = h.replace(/(^[-*] .+$(\n^[-*] .+$)*)/gm, block => {
      const items = block.split('\n').map(l => `<li>${l.replace(/^[-*] /, '')}</li>`).join('');
      return `<ul>${items}</ul>`;
    });
    h = h.replace(/^---$/gm, '<hr>');
    h = h.replace(/\n{2,}/g, '</p><p>').replace(/\n/g, '<br>');
    h = `<p>${h}</p>`;
    h = h.replace(/<p>(<(?:table|ul|ol|h[1-6]|hr)[^>]*>)/g, '$1');
    h = h.replace(/(<\/(?:table|ul|ol|h[1-6]|hr)>)<\/p>/g, '$1');
    h = h.replace(/<p><\/p>/g, '').replace(/<p><br><\/p>/g, '');
    return h;
  }

  function esc(s) {
    return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  // ── Init ─────────────────────────────────────────
  refreshBtn.addEventListener('click', loadVideos);
  loadVideos();
})();
