export const BACKEND_URL = (import.meta.env.VITE_BACKEND_URL || 'http://127.0.0.1:8000')
  .replace(/\/+$/, '');

export const EVENTS_URL = BACKEND_URL.replace(/^http/, 'ws') + '/ws';
