<script>
  import { metrics, servers, chart } from '../stores.js';

  const RADIUS = 27;
  const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

  function fraction(card) {
    return Math.max(0, Math.min(1, card.value / card.max));
  }

  function offset(card) {
    return CIRCUMFERENCE * (1 - fraction(card));
  }

  function level(card) {
    if (card.value >= card.crit) return 'critical';
    if (card.value >= card.warn) return 'warning';
    return 'ok';
  }

  function points(series) {
    const n = series.length;
    return series
      .map((v, i) => {
        const x = (i / (n - 1)) * 600;
        const y = 200 - (v / 100) * 200;
        return x + ',' + y;
      })
      .join(' ');
  }

  function lastTop(series) {
    return 100 - series[series.length - 1];
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
            // transform="rotate(-90 32 32)"
          />
        </svg>
        <div class="metric-text">
          <span class="metric-label">{card.label}</span>
          <span class="metric-value">
            {card.value}<span class="metric-unit">{card.unit}</span>
          </span>
          <span class="metric-info">{card.info}</span>
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
        <li class="legend-item"><span class="legend-dash legend-dash-gpu"></span>GPU</li>
      </ul>
      <div class="chart-ranges">
        <button type="button" class="range-btn">1 min</button>
        <button type="button" class="range-btn range-btn--active">5 min</button>
        <button type="button" class="range-btn">1 hr</button>
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
          <polyline class="series-gpu" points={points($chart.gpu)} />
          <polyline class="series-ram" points={points($chart.ram)} />
          <polyline class="series-cpu" points={points($chart.cpu)} />
        </svg>
        <span class="chart-dot dot-gpu" style="top: {lastTop($chart.gpu)}%"></span>
        <span class="chart-dot dot-ram" style="top: {lastTop($chart.ram)}%"></span>
        <span class="chart-dot dot-cpu" style="top: {lastTop($chart.cpu)}%"></span>
      </div>
    </div>

    <footer class="chart-x">
      <span>-5 min</span>
      <span>-2:30 min</span>
      <span>now</span>
    </footer>
  </section>

  <section class="panel">
    <h2 class="panel-title">SERVER STATUS</h2>
    <ul class="status-list">
      {#each $servers as server (server.id)}
        <li class="status-row">
          <div class="status-id">
            <span class="status-name">{server.name}</span>
            <span class="status-host">{server.host}</span>
          </div>
          <span class="status-state" data-status={server.status}>
            <span class="status-dot"></span>
            {server.status}
          </span>
        </li>
      {/each}
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

  .legend-dash-gpu {
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
  .series-gpu {
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

  .series-gpu {
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

  .dot-gpu {
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

  .status-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
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