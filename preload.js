const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  exportPDF:     (html)    => ipcRenderer.invoke('export-pdf',    html),
  exportWord:    (content) => ipcRenderer.invoke('export-word',   content),
  openSessions:  ()        => ipcRenderer.invoke('open-sessions'),
  openExternal:  (url)     => ipcRenderer.invoke('open-external', url),
  saveHistory:   (data)    => ipcRenderer.invoke('save-history',  data),
  loadHistory:   ()        => ipcRenderer.invoke('load-history'),
  aiSettingsGet:   ()      => ipcRenderer.invoke('ai-settings-get'),
  aiSettingsSave:  (s)     => ipcRenderer.invoke('ai-settings-save', s),
  aiSettingsClear: ()      => ipcRenderer.invoke('ai-settings-clear'),
});
