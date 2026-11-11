import { writable } from 'svelte/store';
import { metrics, servers } from './stores.js';

export const WS_URL = 'ws://localhost:8000/ws';

export const connection = writable('closed');

let ws = null;
let reconnectDelay = 1000;
let reconnectTimer = null;
let pingInterval = null;
let manualClose = false;

function scheduleReconnect() {
  if (manualClose) return;
  connection.set('reconnecting');
  reconnectTimer = setTimeout(() => {
    connectWs();
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, 15000);
}

function stopPing() {
  clearInterval(pingInterval);
  pingInterval = null;
}

function startPing() {
  stopPing();
  pingInterval = setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'ping', data: Date.now() }));
    }
  }, 10000);
}

function mapBackendStatus(status) {
  if (status === 'ok' || status === 'warming_up' || status === 'waiting') return 'online';
  if (status === 'stale') return 'warning';
  if (status === 'error') return 'critical';
  return 'offline';
}

function applyMessage(message) {
  switch (message.type) {
    case 'hello':
      break;
    case 'metrics': {
      const snapshot = message.data;
      const backendServers = snapshot?.servers || [];
      const frontendServers = backendServers.map((s) => ({
        id: s.server,
        name: s.host,
        host: s.host,
        cpu: Math.round(s.cpu?.percent ?? 0),
        ram: Math.round(s.memory?.percent ?? 0),
        disk: Math.round(s.disks?.[0]?.percent ?? 0),
        status: mapBackendStatus(s.status)
      }));
      servers.set(frontendServers);
      if (frontendServers.length > 0) {
        const avg = (key) =>
          Math.round(frontendServers.reduce((acc, s) => acc + s[key], 0) / frontendServers.length);
        metrics.update((cards) =>
          cards.map((card) => {
            if (card.id === 'cpu') return { ...card, value: avg('cpu') };
            if (card.id === 'ram') return { ...card, value: avg('ram') };
            if (card.id === 'disk') return { ...card, value: avg('disk') };
            return card;
          })
        );
      }
      break;
    }
    case 'pong':
      break;
    case 'error':
      console.warn('WS error from server:', message.data);
      break;
    default:
      break;
  }
}

export function connectWs() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  manualClose = false;
  connection.set('connecting');
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    connection.set('open');
    reconnectDelay = 1000;
    startPing();
  };

  ws.onmessage = (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      console.warn('ws: invalid JSON', event.data);
      return;
    }
    applyMessage(message);
  };

  ws.onclose = () => {
    stopPing();
    connection.set('closed');
    scheduleReconnect();
  };

  ws.onerror = () => {
    ws.close();
  };
}

export function disconnectWs() {
  manualClose = true;
  stopPing();
  clearTimeout(reconnectTimer);
  if (ws) ws.close();
}

export function sendWs(data) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(data));
  }
}