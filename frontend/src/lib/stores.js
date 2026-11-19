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
    unit: 'MB/s',
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
export const selectedServerId = writable('');