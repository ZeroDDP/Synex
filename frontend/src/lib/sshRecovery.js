import { get } from 'svelte/store';
import { BACKEND_URL } from './api/config.js';
import { onEvent, resumeWs } from './ws.js';
import { servers, sshRecoveryStatus, sshSessionReplacement } from './stores.js';

const STORAGE_KEY = 'synex.activeSshConnections';
const MAX_RETRY_DELAY_MS = 60000;
const RECOVERED_COOLDOWN_MS = 15000;

let started = false;
let trackedTargets = readTargets();
let statusTimer = null;
const recovering = new Set();
const retries = new Map();

function serverKey(target) {
  const host = typeof target?.host === 'string' ? target.host.trim().toLowerCase() : '';
  const port = Number(target?.port || 22);
  return host && Number.isInteger(port) && port > 0 && port <= 65535
    ? `${host}:${port}`
    : '';
}

function targetKey(target) {
  return target.connectionId || `${serverKey(target)}|${target.username || ''}`;
}

function sameTarget(left, right) {
  return (
    serverKey(left) === serverKey(right) &&
    String(left?.username || '') === String(right?.username || '')
  );
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function readTargets() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
    if (!Array.isArray(saved)) throw new Error('Stored SSH recovery state is not a list.');
    return saved.filter(
      (target) =>
        isRecord(target) &&
        typeof target.host === 'string' &&
        Number.isInteger(target.port) &&
        typeof target.username === 'string' &&
        (target.connectionId === undefined || typeof target.connectionId === 'string') &&
        (target.keyId === undefined || typeof target.keyId === 'string')
    );
  } catch (error) {
    console.warn('Could not read saved SSH recovery targets:', error);
    return [];
  }
}

function persistTargets() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(trackedTargets));
  } catch (error) {
    console.warn('Could not save SSH recovery targets:', error);
  }
}

function rememberTarget(session, profile = null) {
  if (!isRecord(session) || typeof session.host !== 'string' || typeof session.username !== 'string') {
    return null;
  }
  const target = {
    connectionId: profile?.id || session.connection_id || undefined,
    host: profile?.host || session.host,
    port: profile?.port || session.port || 22,
    username: profile?.username || session.username,
    auth: profile?.auth || session.auth,
    keyId: profile?.key_id || session.key_id || undefined
  };
  if (!serverKey(target)) return null;
  const key = targetKey(target);
  trackedTargets = [...trackedTargets.filter((item) => targetKey(item) !== key), target];
  persistTargets();
  return target;
}

function report(state, message, dismissAfter = 0) {
  clearTimeout(statusTimer);
  statusTimer = null;
  sshRecoveryStatus.set({ state, message });
  if (dismissAfter > 0) {
    statusTimer = setTimeout(() => {
      sshRecoveryStatus.set({ state: 'idle', message: '' });
      statusTimer = null;
    }, dismissAfter);
  }
}

class RecoveryRequestError extends Error {
  constructor(message, status, code) {
    super(message);
    this.name = 'RecoveryRequestError';
    this.status = status;
    this.code = code;
  }
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(`${BACKEND_URL}${path}`, options);
  } catch (error) {
    throw new Error(`Could not reach backend at ${BACKEND_URL}: ${errorMessage(error)}`);
  }
  const data = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    throw new RecoveryRequestError(
      typeof data?.detail === 'string' ? data.detail : `Backend returned HTTP ${response.status}.`,
      response.status,
      typeof data?.error === 'string' ? data.error : ''
    );
  }
  return data;
}

function errorMessage(error) {
  return error instanceof Error ? error.message : 'Unexpected backend error.';
}

function validateSessions(value) {
  if (
    !Array.isArray(value) ||
    value.some(
      (session) =>
        !isRecord(session) ||
        typeof session.session_id !== 'string' ||
        typeof session.host !== 'string' ||
        !Number.isInteger(session.port) ||
        typeof session.username !== 'string'
    )
  ) {
    throw new Error('Backend returned an invalid SSH session list.');
  }
  return value;
}

function validateConnections(value) {
  if (
    !Array.isArray(value) ||
    value.some(
      (profile) =>
        !isRecord(profile) ||
        typeof profile.id !== 'string' ||
        typeof profile.host !== 'string' ||
        !Number.isInteger(profile.port) ||
        typeof profile.username !== 'string' ||
        typeof profile.auth !== 'string'
    )
  ) {
    throw new Error('Backend returned an invalid saved connection list.');
  }
  return value;
}

async function loadContext() {
  const [sessionData, connectionData] = await Promise.all([
    request('/api/ssh/sessions'),
    request('/api/connections')
  ]);
  const sessions = validateSessions(sessionData);
  const connections = validateConnections(connectionData);
  for (const session of sessions) {
    const profile =
      connections.find((item) => item.id === session.connection_id) ||
      connections.find((item) => sameTarget(item, session));
    rememberTarget(session, profile);
  }
  return { sessions, connections };
}

function liveForTarget(sessions, target) {
  return sessions.some((session) => sameTarget(session, target));
}

function profileForTarget(connections, target) {
  return (
    connections.find((profile) => profile.id === target.connectionId) ||
    connections.find((profile) => sameTarget(profile, target)) ||
    null
  );
}

async function closeSession(sessionId) {
  try {
    await request(`/api/ssh/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
  } catch (error) {
    if (error instanceof RecoveryRequestError && error.status === 404) return;
    throw error;
  }
}

async function openSession(target, profile) {
  if (profile) {
    if (profile.auth === 'password' && profile.has_password !== true) {
      throw new Error(
        'The saved server profile has no password. Reconnect manually and save its password to enable automatic recovery.'
      );
    }
    return request(`/api/connections/${encodeURIComponent(profile.id)}/connect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    });
  }
  if (target.auth === 'key_id' && target.keyId) {
    return request('/api/ssh/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        host: target.host,
        port: target.port,
        username: target.username,
        key_id: target.keyId
      })
    });
  }
  throw new Error('No reusable saved SSH credentials are available for this server.');
}

function isPermanentFailure(error) {
  return (
    error instanceof RecoveryRequestError &&
    (error.status === 404 ||
      error.code === 'ssh_host_key_unknown' ||
      error.code === 'ssh_host_key_mismatch' ||
      error.code === 'connection_secret_required')
  );
}

async function attemptRecovery(target, force = false) {
  const key = targetKey(target);
  if (!key || recovering.has(key)) return;
  const retry = retries.get(key) || { attempts: 0, nextAt: 0, blocked: false };
  if (!force && (retry.blocked || Date.now() < retry.nextAt)) return;

  recovering.add(key);
  report('recovering', `Restoring SSH connection to ${target.host}:${target.port}…`);
  try {
    const { sessions, connections } = await loadContext();
    const profile = profileForTarget(connections, target);
    const oldSessions = sessions.filter((session) => sameTarget(session, target));
    let newSession;
    try {
      newSession = await openSession(target, profile);
    } catch (error) {
      if (!(error instanceof RecoveryRequestError) || error.status !== 429 || oldSessions.length === 0) {
        throw error;
      }
      for (const session of oldSessions) await closeSession(session.session_id);
      newSession = await openSession(target, profile);
    }
    if (
      !isRecord(newSession) ||
      typeof newSession.session_id !== 'string' ||
      typeof newSession.host !== 'string' ||
      !Number.isInteger(newSession.port) ||
      typeof newSession.username !== 'string'
    ) {
      throw new Error('Backend connected but returned invalid SSH session details.');
    }

    const oldSessionIds = oldSessions
      .map((session) => session.session_id)
      .filter((sessionId) => sessionId !== newSession.session_id);
    rememberTarget(newSession, profile);
    if (oldSessionIds.length > 0) {
      sshSessionReplacement.set({ oldSessionIds, newSession });
    }
    for (const sessionId of oldSessionIds) await closeSession(sessionId);

    retries.set(key, { attempts: 0, nextAt: Date.now() + RECOVERED_COOLDOWN_MS, blocked: false });
    report('recovered', `SSH connection restored to ${target.host}:${target.port}.`, 8000);
  } catch (error) {
    const attempts = retry.attempts + 1;
    const permanent =
      isPermanentFailure(error) ||
      /No reusable saved SSH credentials|no password/i.test(errorMessage(error));
    const delay = Math.min(1000 * 2 ** Math.min(attempts, 6), MAX_RETRY_DELAY_MS);
    retries.set(key, {
      attempts,
      nextAt: permanent ? Number.POSITIVE_INFINITY : Date.now() + delay,
      blocked: permanent
    });
    report('error', `Could not restore ${target.host}:${target.port}: ${errorMessage(error)}`);
    console.warn(`SSH recovery failed for ${target.host}:${target.port}:`, error);
  } finally {
    recovering.delete(key);
  }
}

async function reconcile(recoverMissing) {
  try {
    const { sessions } = await loadContext();
    if (!recoverMissing) return;
    for (const target of trackedTargets) {
      if (!liveForTarget(sessions, target)) void attemptRecovery(target);
    }
  } catch (error) {
    report('error', `Could not check SSH sessions: ${errorMessage(error)}`);
  }
}

function handleMetrics(snapshot) {
  if (!Array.isArray(snapshot?.servers)) return;
  for (const server of snapshot.servers) {
    if (server.status !== 'error' && !(server.status === 'stale' && server.busy !== true)) continue;
    for (const target of trackedTargets) {
      if (serverKey(target) === serverKey({ host: server.host, port: server.port })) {
        void attemptRecovery(target);
      }
    }
  }
}

function handleSessionEvent(event) {
  if (!isRecord(event) || typeof event.action !== 'string') return;
  if (event.action === 'opened') {
    void reconcile(false);
    return;
  }
  if (event.action !== 'closed' || typeof event.host !== 'string') return;
  const target = trackedTargets.find(
    (item) =>
      serverKey(item) === serverKey({ host: event.host, port: event.port }) &&
      item.username === event.username &&
      (!event.connection_id || !item.connectionId || item.connectionId === event.connection_id)
  );
  if (!target) return;
  if (event.reason === 'closed' && !recovering.has(targetKey(target))) {
    void removeIfNoLiveSession(target);
  } else {
    void attemptRecovery(target);
  }
}

async function removeIfNoLiveSession(target) {
  try {
    const sessions = validateSessions(await request('/api/ssh/sessions'));
    if (liveForTarget(sessions, target)) return;
    trackedTargets = trackedTargets.filter((item) => targetKey(item) !== targetKey(target));
    persistTargets();
  } catch (error) {
    report('error', `Could not update saved SSH sessions: ${errorMessage(error)}`);
  }
}

function onResume() {
  if (document.visibilityState !== 'visible') return;
  resumeWs();
  void reconcile(true);
}

export function startSshRecovery() {
  if (started) return () => {};
  started = true;
  const unsubscribeMetrics = onEvent('metrics', handleMetrics);
  const unsubscribeSessions = onEvent('ssh.session', handleSessionEvent);
  const unsubscribeHello = onEvent('hello', () => void reconcile(true));
  document.addEventListener('visibilitychange', onResume);
  window.addEventListener('pageshow', onResume);
  void reconcile(true);

  return () => {
    started = false;
    unsubscribeMetrics();
    unsubscribeSessions();
    unsubscribeHello();
    document.removeEventListener('visibilitychange', onResume);
    window.removeEventListener('pageshow', onResume);
    clearTimeout(statusTimer);
    statusTimer = null;
  };
}

export async function reconnectUnavailableServers() {
  if (trackedTargets.length === 0) await reconcile(false);
  const unavailable = new Set(
    get(servers)
      .filter((server) => server.status !== 'online')
      .map((server) => server.id.toLowerCase())
  );
  const targets = trackedTargets.filter((target) => unavailable.has(serverKey(target)));
  if (targets.length === 0) {
    report('idle', 'There are no unavailable servers with saved SSH sessions.', 5000);
    return;
  }
  await Promise.all(targets.map((target) => attemptRecovery(target, true)));
}
