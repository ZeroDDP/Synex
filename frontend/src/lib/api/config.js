const fromEnv = import.meta.env.VITE_BACKEND_URL;
const devServer = location.port === '5173';

export const BACKEND_URL = (fromEnv || (devServer ? 'http://127.0.0.1:8000' : location.origin))
  .replace(/\/+$/, '');

export const EVENTS_URL = BACKEND_URL.replace(/^http/, 'ws') + '/ws';
