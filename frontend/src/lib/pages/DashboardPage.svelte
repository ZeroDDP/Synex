<script>
  import { chart, metrics, selectedServerId, servers } from '../stores.js';

  export let onAddServer = () => {};

  const RADIUS = 27;
  const CIRCUMFERENCE = 2 * Math.PI * RADIUS;
  const chartRanges = [
    { id: '1m', label: '1 min' },
    { id: '5m', label: '5 min' },
    { id: '1h', label: '1 hr' }
  ];
  let selectedRange = '5m';
  $: rangePoints = selectedRange === '1m' ? 120 : selectedRange === '1h' ? 7200 : 600;
  $: rangeStartLabel = selectedRange === '1m' ? '-1 min' : selectedRange === '1h' ? '-1 hr' : '-5 min';
  $: rangeMiddleLabel = selectedRange === '1m' ? '-30 sec' : selectedRange === '1h' ? '-30 min' : '-2:30 min';

  /** @param {{ value: number|null, max: number }} card */
  function fraction(card) {
    if (card.value === null || card.value === undefined) return 0;
    return Math.max(0, Math.min(1, card.value / card.max));
  }

  /** @param {{ value: number|null, max: number }} card */
  function offset(card) {
    return CIRCUMFERENCE * (1 - fraction(card));
  }

  /** @param {{ value: number|null, crit: number, warn: number }} card */
  function level(card) {
    if (card.value === null || card.value === undefined) return 'ok';
    if (card.value >= card.crit) return 'critical';
    if (card.value >= card.warn) return 'warning';
    return 'ok';
  }

  /** @param {(number|null)[]} series @param {number} sampleCount */
  function points(series, sampleCount) {
    const visibleSeries = series.slice(-sampleCount);
    const offset = sampleCount - visibleSeries.length;
    return visibleSeries
      .map((v, i) => {
        if (v === null || v === undefined) return null;
        const x = ((offset + i + 1) / sampleCount) * 600;
        const y = 200 - (v / 100) * 200;
        return x + ',' + y;
      })
      .filter(Boolean)
      .join(' ');
  }

  /** @param {(number|null)[]} series @param {number} sampleCount */
  function lastTop(series, sampleCount) {
    const lastValue = series
      .slice(-sampleCount)
      .reverse()
      .find((value) => value !== null && value !== undefined);
    return lastValue === undefined ? 100 : 100 - lastValue;
  }

  /** @param {string} serverId */
  function selectServer(serverId) {
    if ($selectedServerId === serverId) return;
    selectedServerId.set(serverId);
    chart.set({ cpu: [], ram: [], network: [] });
  }

  const yLabels = [100, 90, 70, 50, 0];
</script>

<div class="dashboard">
  <ul class="metrics-grid">
    {#each $metrics as card (card.id)}
      <li class="metric-card" data-level={level(card)}>
        <svg class="ring" viewBox="0 0 64 64" aria-hidden="true">
          <circle
            class="ring-track"
            cx="32"
            cy="32"
            r={RADIUS}
            fill="none"
            stroke-width="4"
          />
          <circle
            class="ring-fill"
            cx="32"
            cy="32"
            r={RADIUS}
            fill="none"
            stroke-width="4"
            stroke-linecap="round"
            stroke-dasharray={CIRCUMFERENCE}
            stroke-dashoffset={offset(card)}
            transform="rotate(-90 32 32)"
          />
        </svg>
        <div class="metric-text">
          <span class="metric-label">{card.label}</span>
          <span class="metric-value">
            {card.value === null || card.value === undefined ? '—' : Math.round(card.value)}
            <span class="metric-unit">{card.value === null || card.value === undefined ? '' : card.unit}</span>
          </span>
          <span class="metric-info" title={card.infoTitle}>{card.info}</span>
        </div>
      </li>
    {/each}
  </ul>

  <section class="panel">
    <header class="chart-head">
      <span class="chart-live">LIVE</span>
      <ul class="chart-legend">
        <li class="legend-item"><span class="legend-dash legend-dash-cpu"></span>CPU</li>
        <li class="legend-item"><span class="legend-dash legend-dash-ram"></span>RAM</li>
        <li class="legend-item" title="Network is scaled relative to 1000 Mbit/s">
          <span class="legend-dash legend-dash-network"></span>NET
        </li>
      </ul>
      <div class="chart-ranges">
        {#each chartRanges as range (range.id)}
          <button
            type="button"
            class="range-btn"
            class:range-btn--active={selectedRange === range.id}
            aria-pressed={selectedRange === range.id}
            on:click={() => (selectedRange = range.id)}
          >{range.label}</button>
        {/each}
      </div>
    </header>

    <div class="chart-body">
      <div class="chart-y">
        {#each yLabels as v (v)}
          <span class="chart-y-label" style="top: {100 - v}%">{v}</span>
        {/each}
      </div>
      <div class="chart-area">
        <div class="chart-band"></div>
        <div class="chart-line chart-line-crit"></div>
        <div class="chart-line chart-line-warn"></div>
        <svg class="chart-svg" viewBox="0 0 600 200" preserveAspectRatio="none" aria-hidden="true">
          <polyline class="series-network" points={points($chart.network, rangePoints)} />
          <polyline class="series-ram" points={points($chart.ram, rangePoints)} />
          <polyline class="series-cpu" points={points($chart.cpu, rangePoints)} />
        </svg>
        <span
          class="chart-dot dot-network"
          style="top: {lastTop($chart.network, rangePoints)}%"
          title="Network utilization relative to 1000 Mbit/s"
        ></span>
        <span class="chart-dot dot-ram" style="top: {lastTop($chart.ram, rangePoints)}%"></span>
        <span class="chart-dot dot-cpu" style="top: {lastTop($chart.cpu, rangePoints)}%"></span>
      </div>
    </div>

    <footer class="chart-x">
      <span>{rangeStartLabel}</span>
      <span>{rangeMiddleLabel}</span>
      <span>now</span>
    </footer>
  </section>

  <section class="panel">
    <h2 class="panel-title">SERVER STATUS</h2>
    <ul class="status-list">
      {#each $servers as server (server.id)}
        <li class:status-row--active={$selectedServerId === server.id}>
          <button
            type="button"
            class="status-row"
            aria-pressed={$selectedServerId === server.id}
            on:click={() => selectServer(server.id)}
          >
            <span class="status-id">
              <span class="status-name">{server.name}</span>
              <span class="status-host">{server.host}</span>
            </span>
            <span class="status-state" data-status={server.status}>
              <span class="status-dot"></span>
              {server.status}
            </span>
          </button>
        </li>
      {/each}
      {#if $servers.length === 0}
        <li class="status-empty">
          <span>No servers yet</span>
          <button type="button" class="add-server" on:click={onAddServer}>Add new server</button>
        </li>
      {/if}
    </ul>
  </section>
</div>

<style>
  .dashboard {
    display: flex;
    flex-direction: column;
    gap: var(--synex-space-6);
    font-family: var(--font-host);
  }

  .metrics-grid {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    align-items: center;
    justify-content: center;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: clamp(var(--synex-space-2), 1.5vw + 4px, var(--synex-space-10));
    
  }


  /* Cделать исключение для 3 карточки (mb/s) */
  .metric-card {
    display: flex;
    align-items: center;
    gap: var(--synex-space-4);
    padding: var(--synex-space-4);
    background: var(--synex-bg-surface);
    border: 1px rgba(46.18, 59.03, 79.22, 0.99) solid;
    border-radius: var(--synex-radius-md);
    height: 120px;
  }

  .ring {
    flex: 0 0 auto;
    width: 64px;
    height: 64px;
    display: block;
  }

  .ring-track {
    stroke: var(--synex-ring-off);
  }

  .ring-fill {
    stroke: var(--synex-accent);
    transition: stroke-dashoffset 0.8s ease-out, stroke 0.4s ease-out;
  }

  .metric-card[data-level='warning'] .ring-fill {
    stroke: var(--synex-warning);
  }

  .metric-card[data-level='critical'] .ring-fill {
    stroke: var(--synex-danger-fg);
  }

  .metric-text {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .metric-label {
    font-family: var(--font-host);
    font-size: var(--font-size-12);
    font-weight: 600;
    /* letter-spacing: 0.06em; */
    color: var(--synex-text-muted);
  }

  .metric-value {
    margin-bottom: 6px;
    font-family: var(--font-inter);
    font-size: var(--font-size-16);
    font-weight: 700;
    color: var(--synex-text-secondary);
  }

  .metric-unit {
    font-family: var(--font-inter);
    font-size: var(--font-size-16);
    font-weight: 400;
    color: var(--synex-text-muted);
  }

  .metric-info {
    font-size: var(--font-size-10);
    color: var(--synex-text-muted);
  }

  .panel {
    padding: var(--synex-space-6);
    background: var(--synex-bg-surface);
    border: 1px rgba(46.18, 59.03, 79.22, 0.99) solid;
    border-radius: var(--synex-radius-md);
  }

  .panel-title {
    margin: 0 0 var(--synex-space-4);
    font-size: var(--font-size-14);
    font-weight: 900;
    letter-spacing: 0.04em;
    color: var(--synex-text-muted);
  }

  .chart-head {
    display: flex;
    align-items: center;
    gap: var(--synex-space-6);
    margin-bottom: var(--synex-space-6);
  }

  .chart-live {
    font-size: var(--font-size-13);
    font-weight: 500;
    letter-spacing: 0.08em;
    color: var(--synex-text-muted);
  }

  .chart-legend {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    gap: var(--synex-space-4);
  }

  .legend-item {
    display: flex;
    align-items: center;
    gap: var(--synex-space-2);
    font-size: var(--font-size-13);
    letter-spacing: 0.05em;
    font-weight: 500;
    color: var(--synex-text-primary);
  }

  .legend-dash {
    width: 14px;
    height: 2px;
    border-radius: 1px;
  }

  .legend-dash-cpu {
    background: #ff7b00;
  }

  .legend-dash-ram {
    background: #4f8ff7;
  }

  .legend-dash-network {
    background: #cdd3dc;
  }

  .chart-ranges {
    margin-left: auto;
    display: flex;
    gap: var(--synex-space-2);
  }

  .range-btn {
    padding: 2px 10px;
    border: 1px solid var(--synex-border-strong);
    border-radius: var(--synex-radius-md);
    font-size: var(--font-size-11);
    color: var(--synex-text-muted);
  }

  .range-btn--active {
    border-color: var(--synex-accent);
    color: var(--synex-accent);
  }

  .chart-body {
    display: flex;
    gap: var(--synex-space-2);
    /* border-bottom: 1px solid white; */
  }

  .chart-y {
    position: relative;
    flex: 0 0 28px;
    height: 180px;
  }

  .chart-y-label {
    position: absolute;
    right: 0;
    transform: translateY(-50%);
    font-size: var(--font-size-10);
    color: var(--synex-text-muted);
  }

  .chart-area {
    position: relative;
    flex: 1 1 auto;
    height: 180px;
  }

  .chart-band {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 10%;
    background: linear-gradient(90deg, rgba(169.55, 46.01, 46.01, 0.40) 0%, rgba(108.25, 29.38, 29.38, 0.40) 73%, rgba(67.55, 18.33, 18.33, 0.40) 100%);
  }

  /* Переделать через bg image/svg файл для более редких пробелов в линии */
  .chart-line {
    position: absolute;
    left: 0;
    right: 0;
    border-top: 1px dashed;
  }

  .chart-line-crit {
    top: 10%;
    border-color: rgba(229, 83, 75, 0.6);
  }

  .chart-line-warn {
    top: 30%;
    border-color: rgba(255, 123, 0, 0.5);
  }

  .chart-svg {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    display: block;
  }

  .series-cpu,
  .series-ram,
  .series-network {
    fill: none;
    stroke-width: 2;
    vector-effect: non-scaling-stroke;
  }

  .series-cpu {
    stroke: #ff7b00;
  }

  .series-ram {
    stroke: #005EFF;
  }

  .series-network {
    stroke: #cdd3dc;
  }

  .chart-dot {
    position: absolute;
    right: -3px;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    transform: translateY(-50%);
  }

  .dot-cpu {
    background: #ff7b00;
  }

  .dot-ram {
    background: #005EFF;
  }

  .dot-network {
    background: #cdd3dc;
  }

  .chart-x {
    display: flex;
    justify-content: space-between;
    margin-top: var(--synex-space-2);
    font-size: var(--font-size-10);
    color: var(--synex-text-secondary);
  }

  .status-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--synex-space-2);
  }

  .status-list > li {
    border-left: 3px solid transparent;
    border-radius: var(--synex-radius-sm);
  }

  .status-list > li.status-row--active {
    border-left-color: var(--synex-accent);
    background: var(--synex-bg-active);
  }

  .status-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    padding: var(--synex-space-3) var(--synex-space-4);
    border: 0;
    border-radius: inherit;
    background: transparent;
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  .status-row:hover {
    background: var(--synex-bg-hover);
  }

  .status-row:focus-visible {
    outline: 2px solid var(--synex-accent);
    outline-offset: -2px;
  }

  .status-empty {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--synex-space-3);
    padding: var(--synex-space-3) var(--synex-space-4);
    color: var(--synex-text-muted);
    font-size: var(--font-size-12);
  }

  .add-server {
    flex: 0 0 auto;
    color: var(--synex-accent);
    font: inherit;
    font-weight: 600;
  }

  .status-id {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .status-name {
    font-size: var(--font-size-13);
    font-weight: 600;
    color: var(--synex-text-secondary);
  }

  .status-host {
    font-family: var(--font-mono);
    font-size: var(--font-size-11);
    color: var(--synex-text-muted);
  }

  .status-state {
    display: flex;
    align-items: center;
    gap: var(--synex-space-2);
    font-size: var(--font-size-12);
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .status-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: currentColor;
  }

  .status-state[data-status='online'] {
    color: var(--synex-ok);
  }

  .status-state[data-status='warning'] {
    color: var(--synex-warning);
  }

  .status-state[data-status='critical'] {
    color: var(--synex-danger-fg);
  }

  .status-state[data-status='offline'] {
    color: var(--synex-offline);
  }
</style>