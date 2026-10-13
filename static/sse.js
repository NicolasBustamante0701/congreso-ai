/**
 * Cliente de streaming SSE sobre fetch (POST con cuerpo JSON), el mismo para
 * el chat, el expediente y Sesiones.
 *
 * Las tres copias que había (app.js, expediente.js, sessions.js) tenían dos
 * fallas en común: no miraban resp.ok (un 500 se leía como stream vacío y la
 * pantalla quedaba en blanco) y el `break` ante un evento de error solo
 * salía del for interno, así que se seguía leyendo el resto del stream.
 *
 *   await DianaSSE.post('/chat', { messages }, {
 *     signal: abortCtl.signal,
 *     onEvent(obj) { ... },   // devolver false corta la lectura
 *   });
 *
 * Lanza Error ante HTTP no-OK o red caída; AbortError si se canceló.
 */
(function () {
  'use strict';

  async function post(url, body, { signal, onEvent } = {}) {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body ?? {}),
      signal,
    });
    if (!resp.ok) {
      let detalle = '';
      try { detalle = (await resp.text()).slice(0, 200); } catch { /* sin cuerpo */ }
      throw new Error(`el servidor respondió ${resp.status}${detalle ? `: ${detalle}` : ''}`);
    }

    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) return;
        buf += decoder.decode(value, { stream: true });
        const lineas = buf.split('\n');
        buf = lineas.pop();
        for (const linea of lineas) {
          if (!linea.startsWith('data: ')) continue;   // ": ping" y vacías
          const crudo = linea.slice(6).trim();
          if (crudo === '[DONE]') return;
          let obj;
          try { obj = JSON.parse(crudo); } catch { continue; }   // fragmento roto
          if (onEvent && onEvent(obj) === false) return;
        }
      }
    } finally {
      try { await reader.cancel(); } catch { /* ya cerrado */ }
    }
  }

  window.DianaSSE = { post };
})();
