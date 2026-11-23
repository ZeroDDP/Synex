import { get, writable } from 'svelte/store';
import { EVENTS_URL } from './api/config.js';
import { chart, metrics, NETWORK_MAX_MBPS, selectedServerId, servers } from './stores.js';

export const WS_URL = EVENTS_URL;

export const connection = writable('closed');

/**
 * @typedef {object} BackendState
 * @property {'connecting'|'online'|'reconnecting'|'offline'} state
 * @property {Record<string, unknown>|null} hello
 * @property {string|null} error
 */

/** @type {BackendState} */
const initialBackendState = { state: 'connecting', hello: null, error: null };
export const backendState = writable(initialBackendState);

const CONNECTION_TIMEOUT_MS = 10000;
/** @type {WebSocket|null} */
let ws = null;
let reconnectDelay = 1000;
/** @type {ReturnType<typeof setTimeout>|null} */
let reconnectTimer = null;
/** @type {ReturnType<typeof setTimeout>|null} */
let connectionTimeoutTimer = null;
let started = false;
/** @type {Map<string, Set<(data: unknown) => void>>} */
const handlers = new Map();

/** @param {string} error */
function setBackendError(error) {
  backendState.update((state) => ({ ...state, error }));
}

function clearBackendError() {
  backendState.update((state) => ({ ...state, error: null }));
}

export function dismissBackendError() {
  clearBackendError();
}

/**
 * @param {string} type
 * @param {(data: unknown) => void} handler
 */
export function onEvent(type, handler) {
  if (!handlers.has(type)) handlers.set(type, new Set());
  handlers.get(type).add(handler);
  return () => handlers.get(type)?.delete(handler);
}

/** @param {string} type @param {unknown} data */
function emit(type, data) {
  for (const handler of handlers.get(type) ?? []) {
    try {
      handler(data);
    } catch (error) {
      console.error(`WebSocket ${type} handler failed:`, error);
    }
  }
}

function scheduleReconnect() {
  if (!started || reconnectTimer !== null) return;
  connection.set('reconnecting');
  backendState.update((state) => ({ ...state, state: 'reconnecting' }));
  const delay = reconnectDelay;
  reconnectDelay = Math.min(reconnectDelay * 2, 10000);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    open();
  }, delay);
}

/** @param {string} status */
function mapBackendStatus(status) {
  if (status === 'ok' || status === 'warming_up' || status === 'waiting') return 'online';
  if (status === 'stale') return 'warning';
  if (status === 'error') return 'critical';
  return 'offline';
}

/** @param {(number|null|undefined)[]} values */
function average(values) {
  const present = values.filter((value) => value !== null && value !== undefined);
  if (present.length === 0) return null;
  return Math.round(present.reduce((sum, value) => sum + value, 0) / present.length);
}

/** @param {(number|null|undefined)[]} values */
function sum(values) {
  const present = values.filter((value) => value !== null && value !== undefined);
  if (present.length === 0) return null;
  return present.reduce((total, value) => total + value, 0);
}

/** @param {number} bytes */
function formatBytes(bytes) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${Number(value.toFixed(value >= 10 ? 0 : 1))} ${units[unit]}`;
}

/** @param {(number|null)[]} series @param {number|null} value */
function appendPoint(series, value) {
  return [...series, value].slice(-7200);
}

/** @param {unknown} value @returns {value is Record<string, unknown>} */
function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** @param {unknown} value @returns {value is number|null|undefined} */
function isOptionalNumber(value) {
  return value === undefined || value === null || (typeof value === 'number' && Number.isFinite(value));
}

/**
 * @typedef {object} MetricGroup
 * @property {number|null|undefined} [percent]
 * @property {number|null|undefined} [used]
 * @property {number|null|undefined} [total]
 * @property {number|null|undefined} [avg]
 * @property {number|null|undefined} [min]
 * @property {number|null|undefined} [max]
 * @property {string|null|undefined} [mount]
 */

/**
 * @typedef {object} NetworkCounter
 * @property {number|null|undefined} [total]
 * @property {number|null|undefined} [speed]
 * @property {number|null|undefined} [avg]
 * @property {number|null|undefined} [min]
 * @property {number|null|undefined} [max]
 */

/**
 * @typedef {object} BackendServer
 * @property {string} server
 * @property {string} [host]
 * @property {number|null} [port]
 * @property {string} status
 * @property {string|null} [error]
 * @property {MetricGroup|null} [cpu]
 * @property {MetricGroup|null} [memory]
 * @property {MetricGroup|null} [disk]
 * @property {{rx?: NetworkCounter|null, tx?: NetworkCounter|null}|null} [network]
 * @property {(MetricGroup & {mount?: string|null})[]|null} [disks]
 * @property {{utilization?: number|null}[]|null} [gpus]
 */

/**
 * @typedef {object} MetricsSnapshot
 * @property {BackendServer[]} servers
 */

/**
 * @param {unknown} value
 * @param {string[]} fields
 * @returns {value is MetricGroup|null|undefined}
 */
function isMetricGroup(value, fields) {
  return (
    value === null ||
    value === undefined ||
    (isRecord(value) && fields.every((field) => isOptionalNumber(value[field])))
  );
}

/** @param {unknown} disk @returns {disk is MetricGroup & {mount?: string|null}} */
function isBackendDisk(disk) {
  return (
    isRecord(disk) &&
    isOptionalNumber(disk.percent) &&
    isOptionalNumber(disk.used) &&
    isOptionalNumber(disk.total) &&
    (disk.mount === undefined || disk.mount === null || typeof disk.mount === 'string')
  );
}

/** @param {unknown} gpu @returns {gpu is {utilization?: number|null}} */
function isBackendGpu(gpu) {
  return isRecord(gpu) && isOptionalNumber(gpu.utilization);
}

/** @param {unknown} value @returns {value is BackendServer} */
function isBackendServer(value) {
  if (!isRecord(value)) return false;
  const validNetwork =
    value.network === null ||
    value.network === undefined ||
    (isRecord(value.network) &&
      isMetricGroup(value.network.rx, ['total', 'speed', 'avg', 'min', 'max']) &&
      isMetricGroup(value.network.tx, ['total', 'speed', 'avg', 'min', 'max']));
  const validDisks =
    value.disks === null ||
    value.disks === undefined ||
    (Array.isArray(value.disks) && value.disks.every(isBackendDisk));
  const validGpus =
    value.gpus === null ||
    value.gpus === undefined ||
    (Array.isArray(value.gpus) && value.gpus.every(isBackendGpu));

  return (
    typeof value.server === 'string' &&
    typeof value.status === 'string' &&
    (value.host === undefined || typeof value.host === 'string') &&
    isOptionalNumber(value.port) &&
    (value.error === undefined || value.error === null || typeof value.error === 'string') &&
    isMetricGroup(value.cpu, ['percent', 'avg', 'min', 'max']) &&
    isMetricGroup(value.memory, ['percent', 'used', 'total']) &&
    isMetricGroup(value.disk, ['percent', 'used', 'total']) &&
    validNetwork &&
    validDisks &&
    validGpus
  );
}

/** @param {unknown} value @returns {value is MetricsSnapshot} */
function isMetricsSnapshot(value) {
  return isRecord(value) && Array.isArray(value.servers) && value.servers.every(isBackendServer);
}

/** @param {BackendServer} server */
function rootDisk(server) {
  return server.disk ?? server.disks?.find((item) => item.mount === '/') ?? server.disks?.[0];
}

/** @param {MetricsSnapshot} snapshot */
function applyMetrics(snapshot) {
  const backendServers = snapshot?.servers ?? [];
  const frontendServers = backendServers.map((server) => {
    const host = server.host ?? server.server;
    return {
      id: server.server,
      name: host,
      host: server.port ? `${host}:${server.port}` : host,
      cpu: server.cpu?.percent ?? null,
      ram: server.memory?.percent ?? null,
      disk: rootDisk(server)?.percent ?? null,
      status: mapBackendStatus(server.status)
    };
  });
  servers.set(frontendServers);

  const previousServerId = get(selectedServerId);
  const selectedServer =
    backendServers.find((server) => server.server === previousServerId) ?? backendServers[0] ?? null;
  const nextServerId = selectedServer?.server ?? '';
  if (nextServerId !== previousServerId) {
    selectedServerId.set(nextServerId);
    chart.set({ cpu: [], ram: [], network: [] });
  }
  const metricServers = selectedServer ? [selectedServer] : [];

  const cpu = average(metricServers.map((server) => server.cpu?.percent));
  const cpuAverage = average(metricServers.map((server) => server.cpu?.avg));
  const cpuMaximum = average(metricServers.map((server) => server.cpu?.max));
  const ram = average(metricServers.map((server) => server.memory?.percent));
  const memoryUsed = sum(metricServers.map((server) => server.memory?.used));
  const memoryTotal = sum(metricServers.map((server) => server.memory?.total));
  const diskValues = metricServers.map((server) => rootDisk(server)?.percent);
  const disk = average(diskValues);
  const diskUsed = sum(metricServers.map((server) => rootDisk(server)?.used));
  const diskTotal = sum(metricServers.map((server) => rootDisk(server)?.total));
  const networkRx = sum(metricServers.map((server) => server.network?.rx?.speed));
  const networkTx = sum(metricServers.map((server) => server.network?.tx?.speed));
  const network =
    networkRx === null && networkTx === null
      ? null
      : ((networkRx ?? 0) + (networkTx ?? 0)) * 8 / 1e6;
  const networkChartValue =
    network === null ? null : Math.max(0, Math.min(100, (network / NETWORK_MAX_MBPS) * 100));

  metrics.update((cards) =>
    cards.map((card) => {
      if (card.id === 'cpu') {
        const info =
          cpuAverage === null || cpuMaximum === null
            ? 'Waiting for metrics'
            : `avg ${cpuAverage}% / max ${cpuMaximum}%`;
        return { ...card, value: cpu, info };
      }
      if (card.id === 'ram') {
        const info =
          memoryUsed === null || memoryTotal === null
            ? 'Waiting for metrics'
            : `${formatBytes(memoryUsed)}/${formatBytes(memoryTotal)} used`;
        return { ...card, value: ram, info };
      }
      if (card.id === 'disk') {
        const info =
          diskUsed === null || diskTotal === null
            ? 'Waiting for metrics'
            : `${formatBytes(diskUsed)}/${formatBytes(diskTotal)} used`;
        return { ...card, value: disk, info };
      }
      if (card.id === 'network') {
        const collectionError =
          typeof selectedServer?.error === 'string' ? selectedServer.error.trim() : '';
        const info =
          network !== null
            ? `↓ ${formatBytes(networkRx ?? 0)}/s / ↑ ${formatBytes(networkTx ?? 0)}/s`
            : !selectedServer
              ? 'Waiting for metrics'
              : collectionError
                ? 'Metrics unavailable'
                : selectedServer.network == null
                  ? 'Worker has no network counters'
                  : 'Waiting for first network sample';
        return { ...card, value: network, info, infoTitle: collectionError || undefined };
      }
      return card;
    })
  );
  chart.update((series) => ({
    cpu: appendPoint(series.cpu, cpu),
    ram: appendPoint(series.ram, ram),
    network: appendPoint(series.network, networkChartValue)
  }));
}

/** @param {unknown} message */
function handleMessage(message) {
  if (
    !isRecord(message) ||
    typeof message.type !== 'string' ||
    message.type.length === 0 ||
    typeof message.ts !== 'number' ||
    !Number.isFinite(message.ts)
  ) {
    setBackendError('Backend sent an invalid WebSocket message; it was ignored.');
    console.warn('WebSocket message has an invalid envelope:', message);
    return;
  }

  if (message.type === 'hello') {
    if (!isRecord(message.data)) {
      setBackendError('Backend sent an invalid connection greeting; it was ignored.');
      console.warn('WebSocket hello message has invalid data:', message.data);
      return;
    }
    clearTimeout(connectionTimeoutTimer);
    connectionTimeoutTimer = null;
    reconnectDelay = 1000;
    backendState.set({ state: 'online', hello: message.data, error: null });
  } else if (message.type === 'metrics') {
    if (!isMetricsSnapshot(message.data)) {
      setBackendError('Backend sent invalid metrics data; this update was ignored.');
      console.warn('WebSocket metrics message has invalid data:', message.data);
      return;
    }
    applyMetrics(message.data);
  } else if (message.type === 'error') {
    console.warn('WebSocket error from backend:', message.data);
    const data = message.data;
    setBackendError(
      isRecord(data) && typeof data.detail === 'string'
        ? data.detail
        : 'Backend reported a WebSocket error.'
    );
  }

  emit(message.type, message.data);
}

function open() {
  if (!started || (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING))) {
    return;
  }

  connection.set('connecting');
  backendState.update((state) => ({ ...state, state: 'connecting' }));
  /** @type {WebSocket} */
  let socket;
  try {
    socket = new WebSocket(WS_URL);
  } catch (error) {
    console.error(`Could not create WebSocket connection to ${WS_URL}:`, error);
    setBackendError(`Could not connect to backend at ${WS_URL}.`);
    scheduleReconnect();
    return;
  }
  ws = socket;
  let connectionTimedOut = false;
  connectionTimeoutTimer = setTimeout(() => {
    if (ws !== socket || socket.readyState >= WebSocket.CLOSING) return;
    connectionTimedOut = true;
    socket.close();
  }, CONNECTION_TIMEOUT_MS);

  socket.onopen = () => {
    if (ws !== socket) return;
    connection.set('open');
  };

  socket.onmessage = (event) => {
    /** @type {unknown} */
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (error) {
      console.warn('WebSocket received invalid JSON:', error, event.data);
      setBackendError('Backend sent invalid JSON; the message was ignored.');
      return;
    }
    handleMessage(message);
  };

  socket.onclose = (event) => {
    if (ws !== socket) return;
    clearTimeout(connectionTimeoutTimer);
    connectionTimeoutTimer = null;
    ws = null;
    connection.set('closed');
    if (started) {
      setBackendError(
        connectionTimedOut
          ? `Backend did not respond within ${CONNECTION_TIMEOUT_MS / 1000} seconds. Reconnecting…`
          : `WebSocket connection lost (code ${event.code}). Reconnecting…`
      );
    }
    scheduleReconnect();
  };

  socket.onerror = (error) => {
    console.warn(`WebSocket connection to ${WS_URL} failed`, error);
    socket.close();
  };
}

export function startEvents() {
  if (started) return;
  started = true;
  open();
}

export function connectWs() {
  startEvents();
}

export function disconnectWs() {
  started = false;
  clearTimeout(reconnectTimer);
  clearTimeout(connectionTimeoutTimer);
  reconnectTimer = null;
  connectionTimeoutTimer = null;
  const socket = ws;
  ws = null;
  if (socket) socket.close();
  connection.set('closed');
  backendState.update((state) => ({ ...state, state: 'offline', error: null }));
}

/** @param {unknown} data */
export function sendWs(data) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(data));
    return true;
  }
  return false;
}