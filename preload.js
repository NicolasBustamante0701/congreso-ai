const { contextBridge, ipcRenderer } = require('electron');

// Marca la plataforma para que el CSS deje sitio a los botones de ventana de Mac.
if (process.platform === 'darwin') {
  window.addEventListener('DOMContentLoaded', () => document.documentElement.classList.add('is-mac'));
}

contextBridge.exposeInMainWorld('electronAPI', {
  exportPDF:     (html)    => ipcRenderer.invoke('export-pdf',    html),
  exportWord:    (content) => ipcRenderer.invoke('export-word',   content),
  openExternal:  (url)     => ipcRenderer.invoke('open-external', url),
  saveHistory:   (data)    => ipcRenderer.invoke('save-history',  data),
  loadHistory:   ()        => ipcRenderer.invoke('load-history'),
  aiSettingsGet:   ()      => ipcRenderer.invoke('ai-settings-get'),
  aiSettingsSave:  (s)     => ipcRenderer.invoke('ai-settings-save', s),
  aiSettingsClear: ()      => ipcRenderer.invoke('ai-settings-clear'),
  actualizacionEstado: ()   => ipcRenderer.invoke('actualizacion-estado'),
  actualizarAhora:     ()   => ipcRenderer.invoke('actualizar-ahora'),
  onActualizacionLista: (cb) => ipcRenderer.on('actualizacion-lista', (_e, version) => cb(version)),
});
