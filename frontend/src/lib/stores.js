import { writable } from 'svelte/store';

export const metrics = writable([
  {
    id: 'cpu',
    label: 'CPU',
    value: 50,
    unit: '%',
    info: '8-core/3.2 GHz avg',
    max: 100,
    warn: 90,
    crit: 95
  },
  {
    id: 'ram',
    label: 'RAM',
    value: 50,
    unit: '%',
    info: '8 GB/16 GB used',
    max: 100,
    warn: 90,
    crit: 95
  },
  {
    id: 'network',
    label: 'NETWORK',
    value: 453,
    unit: 'MB/s',
    info: 'Connected',
    max: 1000,
    warn: 900,
    crit: 950
  },
  {
    id: 'disk',
    label: 'DISK',
    value: 87,
    unit: '%',
    info: '578 GB/2 TB',
    max: 100,
    warn: 90,
    crit: 95
  }
]);

export const chart = writable({
  cpu: [62, 65, 64, 70, 99, 99, 78, 80, 79, 84, 87, 85],
  ram: [40, 42, 41, 45, 47, 46, 50, 52, 51, 49, 50, 50],
  gpu: [10, 18, 22, 20, 35, 30, 42, 38, 55, 60, 58, 70]
});

export const servers = writable([
  {
    id: 'prod-api-01',
    name: 'prod-api-01',
    host: '195.03.059.09',
    cpu: 66,
    ram: 24,
    disk: 53,
    status: 'online'
  },
  {
    id: 'prod-api-02',
    name: 'prod-api-02',
    host: '195.05.031.93',
    cpu: 39,
    ram: 46,
    disk: 10,
    status: 'online'
  },
  {
    id: 'prod-api-03',
    name: 'prod-api-03',
    host: '155.03.031.643',
    cpu: 95,
    ram: 96,
    disk: 69,
    status: 'warning'
  }
]);

export const containers = writable([
  {
    id: 'api-gateway01',
    name: 'api-gateway01',
    image: 'doc.01',
    status: 'online',
    cpu: 0.4,
    memory: '128 MB',
    ports: ['443/tcp', '80/tcp']
  },
  {
    id: 'api-gateway02',
    name: 'api-gateway02',
    image: 'doc.01',
    status: 'online',
    cpu: 9.5,
    memory: '98 MB',
    ports: ['8080/tcp']
  },
  {
    id: 'api-gateway03',
    name: 'api-gateway03',
    image: 'doc.01',
    status: 'warning',
    cpu: 2.3,
    memory: '108 MB',
    ports: ['22/tcp']
  }
]);

export const terminal = writable({
  sessions: [
    { id: 'prod-api-01', label: 'prod-api-01' },
    { id: 'primary',     label: '/primary/' }
  ],
  lines: [
    { type: 'prompt', text: 'admin@prod-api-01: ~ code' },
    { type: 'output', text: 'admin@user — Project.' },
    { type: 'output', text: 'Active (active) Since Friday 02/02/2024' },
    { type: 'prompt', text: 'admin@prod-api-01: ~' }
  ]
});

export const logs = writable([
  {
    id: 'l1',
    time: '14:32:06',
    level: 'info',
    source: 'file.0129102',
    message: 'Backup completed successfully'
  },
  {
    id: 'l2',
    time: '14:33:05',
    level: 'warn',
    source: 'file.0124232',
    message: 'High memory usage on prod-api-03'
  },
  {
    id: 'l3',
    time: '14:35:04',
    level: 'error',
    source: 'file.1023125',
    message: 'SSH connection failed'
  }
]);

export const alerts = writable([
  {
    id: 'a1',
    title: 'SSH connection failed',
    description: 'описание самой проблемы',
    severity: 'critical',
    time: '14:35:04'
  },
  {
    id: 'a2',
    title: 'SSH connection failed',
    description: 'описание самой проблемы',
    severity: 'critical',
    time: '14:36:12'
  }
]);

export const files = writable({
  path: ['srv', 'agv'],
  tree: [
    {
      id: 'srv',
      name: 'srv',
      type: 'dir',
      children: [
        { id: 'srv/agv',    name: 'agv',    type: 'dir' },
        { id: 'srv/random', name: 'random', type: 'dir' },
        { id: 'srv/text',   name: 'text',   type: 'dir' }
      ]
    }
  ],
  entries: [
    { id: 'dir1',   name: 'dir1',    type: 'dir',  size: null },
    { id: 'files2', name: 'files2.', type: 'file', size: '2.9 MB' },
    { id: 'files3', name: 'files3.', type: 'file', size: '10.7 MB' }
  ]
});