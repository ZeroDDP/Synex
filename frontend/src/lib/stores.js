import { writable } from 'svelte/store';

export const NETWORK_MAX_MBPS = 1000;

/**
 * @typedef {object} MetricCard
 * @property {string} id
 * @property {string} label
 * @property {number|null} value
 * @property {string} unit
 * @property {string} info
 * @property {string} [infoTitle]
 * @property {number} max
 * @property {number} warn
 * @property {number} crit
 */

/** @type {MetricCard[]} */
const initialMetrics = [
  {
    id: 'cpu',
    label: 'CPU',
    value: null,
    unit: '%',
    info: 'Waiting for metrics',
    max: 100,
    warn: 90,
    crit: 95
  },
  {
    id: 'ram',
    label: 'RAM',
    value: null,
    unit: '%',
    info: 'Waiting for metrics',
    max: 100,
    warn: 90,
    crit: 95
  },
  {
    id: 'network',
    label: 'NETWORK',
    value: null,
    unit: 'Mbit/s',
    info: 'Waiting for metrics',
    max: NETWORK_MAX_MBPS,
    warn: 900,
    crit: 950
  },
  {
    id: 'disk',
    label: 'DISK',
    value: null,
    unit: '%',
    info: 'Waiting for metrics',
    max: 100,
    warn: 90,
    crit: 95
  }
];

export const metrics = writable(initialMetrics);

/**
 * @typedef {object} MetricChart
 * @property {(number|null)[]} cpu
 * @property {(number|null)[]} ram
 * @property {(number|null)[]} network
 */

/** @type {MetricChart} */
const initialChart = {
  cpu: [],
  ram: [],
  network: []
};

export const chart = writable(initialChart);

/**
 * @typedef {object} DashboardServer
 * @property {string} id
 * @property {string} name
 * @property {string} host
 * @property {number|null} cpu
 * @property {number|null} ram
 * @property {number|null} disk
 * @property {string} status
 */

/** @type {DashboardServer[]} */
const initialServers = [];

export const servers = writable(initialServers);

/** @type {import('svelte/store').Writable<Record<string, string>>} */
export const serverUsernames = writable({});

/** @param {unknown[]} sessions */
export function setServerUsernames(sessions) {
  /** @type {Record<string, string>} */
  const usernames = {};
  for (const session of sessions) {
    if (
      session &&
      typeof session === 'object' &&
      typeof session.host === 'string' &&
      typeof session.username === 'string' &&
      Number.isInteger(session.port)
    ) {
      const key = `${session.host.toLowerCase()}:${session.port}`;
      if (typeof usernames[key] !== 'string') usernames[key] = session.username;
    }
  }
  serverUsernames.set(usernames);
}

function storedServerId() {
  try {
    return localStorage.getItem('synex.selectedServerId') || '';
  } catch (error) {
    console.warn('Could not read the selected server from local storage:', error);
    return '';
  }
}

export const selectedServerId = writable(storedServerId());
selectedServerId.subscribe((serverId) => {
  try {
    if (serverId) localStorage.setItem('synex.selectedServerId', serverId);
    else localStorage.removeItem('synex.selectedServerId');
  } catch (error) {
    console.warn('Could not save the selected server to local storage:', error);
  }
});

export const sshRecoveryStatus = writable({ state: 'idle', message: '' });
export const sshSessionReplacement = writable(null);