/**
 * Markdown → HTML seguro, el mismo en todas las vistas (chat, expediente,
 * sesiones, live).
 *
 * Antes había tres parsers distintos: marked+DOMPurify en el chat y el
 * expediente, y dos hechos a mano con regex en sessions.js y live.js. El de
 * Sesiones metía en el HTML el texto del modelo sin escapar (títulos,
 * negritas, links), y el de Live no dibujaba tablas. Ahora todos pasan por
 * acá: tablas GFM siempre, y nada de lo que diga el modelo o una fuente se
 * ejecuta como HTML.
 *
 * Requiere marked.min.js y purify.min.js cargados antes. Si faltan, degrada a
 * texto plano escapado en vez de romper la vista.
 */
(function () {
  'use strict';

  const OK = typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined';
  if (OK) marked.setOptions({ breaks: true, gfm: true });
  else console.error('marked/DOMPurify no cargaron — se muestra el texto sin formato.');

  const ALLOWED_TAGS = ['p', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'strong', 'em', 'code', 'pre', 'blockquote',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'ul', 'ol', 'li', 'a', 'hr', 'span'];

  // Marca de tramo/tiempo en una línea sola: "[00:10 – 01:10]" o
  // "[01:02:03 – 01:05:00]" (resumen de Live). Se dibuja como encabezado.
  const TS_LINE = /^\[(\d{1,2}(?::\d{2}){1,2})\s*[–-]\s*(\d{1,2}(?::\d{2}){1,2})\]$/;

  function esc(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function render(md, opts = {}) {
    const texto = String(md ?? '');
    if (!OK) return `<p>${esc(texto).replace(/\n/g, '<br>')}</p>`;
    let fuente = texto;
    if (opts.tramos) {
      fuente = texto.split('\n').map(l => {
        const t = l.trim();
        return TS_LINE.test(t) ? `\n<h3 class="ts">${esc(t)}</h3>\n` : l;
      }).join('\n');
    }
    return DOMPurify.sanitize(marked.parse(fuente), {
      ALLOWED_TAGS,
      ADD_ATTR: ['target', 'rel'],
    });
  }

  window.DianaMD = { render, esc, ok: OK };
})();
