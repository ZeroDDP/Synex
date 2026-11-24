<script>
  import SshConnect from '../SshConnect.svelte';
  import { servers, serverUsernames } from '../stores.js';

  export let onConnected = () => {};
  export let showConnectForm = false;

  function meterWidth(value) {
    return typeof value === 'number' && Number.isFinite(value)
      ? Math.max(0, Math.min(100, value))
      : 0;
  }

  function meterLabel(value) {
    return typeof value === 'number' && Number.isFinite(value) ? `${Math.round(value)}%` : '—';
  }
</script>
<!-- Rename objects in coloumn NAME to server name -->
<div class="page-servers">
  {#if showConnectForm}
    <SshConnect onClose={() => (showConnectForm = false)} {onConnected} />
  {:else}
    <section class="panel">
      <header class="panel-head">
        <span class="panel-title">ALL SERVERS</span>
        <button type="button" class="add-server" on:click={() => (showConnectForm = true)}>
          Add new server
        </button>
      </header>

      <div class="table">
        <div class="table-row table-row--head">
          <span class="cell cell-name">NAME</span>
          <span class="cell cell-host">HOST</span>
          <span class="cell cell-meter">CPU</span>
          <span class="cell cell-meter">RAM</span>
          <span class="cell cell-meter">DISK</span>
          <span class="cell cell-status">STATUS</span>
        </div>

        {#each $servers as server (server.id)}
          <div class="table-row">
            <span class="cell cell-name">{$serverUsernames[server.id] || '—'}</span>
            <span class="cell cell-host">{server.host}</span>
            <span class="cell cell-meter">
              <span class="bar">
                <span class="bar-fill" style="width: {meterWidth(server.cpu)}%"></span>
              </span>
              <span class="bar-value">{meterLabel(server.cpu)}</span>
            </span>
            <span class="cell cell-meter">
              <span class="bar">
                <span class="bar-fill" style="width: {meterWidth(server.ram)}%"></span>
              </span>
              <span class="bar-value">{meterLabel(server.ram)}</span>
            </span>
            <span class="cell cell-meter">
              <span class="bar">
                <span class="bar-fill" style="width: {meterWidth(server.disk)}%"></span>
              </span>
              <span class="bar-value">{meterLabel(server.disk)}</span>
            </span>
            <span class="cell cell-status">
              <span class="status-dot" data-status={server.status}></span>
              <span class="status-text" data-status={server.status}>{server.status}</span>
            </span>
          </div>
        {/each}
      </div>
    </section>
  {/if}
</div>

<style>
  .page-servers {
    font-family: var(--font-host);
  }

  .panel {
    padding: var(--synex-space-6);
    background: var(--synex-bg-surface);
    border: 1px solid var(--synex-border-subtle);
    border-radius: var(--synex-radius-md);
  }

  .panel-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-bottom: var(--synex-space-4);
    border-bottom: 1px solid var(--synex-border-subtle);
  }

  .panel-title {
    font-size: var(--font-size-15);
    font-weight: 700;
    letter-spacing: 0.05em;
    color: var(--synex-text-muted);
  }

  .add-server {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: var(--synex-space-2) var(--synex-space-5);
    border-radius: var(--synex-radius-lg);
    background: var(--synex-accent);
    color: var(--synex-text-primary);
    font-size: var(--font-size-14);
    font-family: var(--font-host);
    font-weight: 600;
    width: 150px;
    height: 30px;
  }

  .add-server:hover {
    background: var(--synex-accent-hover);
  }

  .table {
    display: flex;
    flex-direction: column;
  }

  .table-row {
    display: grid;
    grid-template-columns: 1.1fr 1.3fr 1fr 1fr 1fr 1fr;
    align-items: center;
    gap: var(--synex-space-4);
    padding: var(--synex-space-3) 0;
  }

  .table-row--head {
    padding: var(--synex-space-4) 0 var(--synex-space-3);
    border-bottom: 1px solid var(--synex-border-subtle);
  }

  .table-row--head .cell {
    font-size: var(--font-size-14);
    font-family: var(--font-host);
    letter-spacing: 0.06em;
    font-weight: 700;
    color: var(--synex-text-muted);
  }

  .cell-name {
    font-size: var(--font-size-15);
    color: var(--synex-text-primary);
  }

  .cell-host {
    font-size: var(--font-size-13);
    color: var(--synex-text-secondary);
  }

  .cell-meter {
    display: flex;
    align-items: center;
    gap: var(--synex-space-2);
  }

  .bar {
    width: 72px;
    height: 4px;
    border-radius: var(--synex-radius-full);
    background: #d9d9d9;
    overflow: hidden;
  }

  .bar-fill {
    display: block;
    height: 100%;
    border-radius: var(--synex-radius-full);
    background: var(--synex-accent);
  }

  .bar-value {
    font-size: var(--font-size-12);
    color: var(--synex-accent);
  }

  .cell-status {
    display: flex;
    align-items: center;
    gap: var(--synex-space-2);
  }

  .status-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
  }

  .status-text {
    font-size: var(--font-size-12);
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .status-dot[data-status='online'] {
    background: var(--synex-ok);
  }

  .status-text[data-status='online'] {
    color: var(--synex-ok);
  }

  .status-dot[data-status='warning'] {
    background: var(--synex-warning);
  }

  .status-text[data-status='warning'] {
    color: var(--synex-warning);
  }

  .status-dot[data-status='critical'] {
    background: var(--synex-danger-fg);
  }

  .status-text[data-status='critical'] {
    color: var(--synex-danger-fg);
  }

  .status-dot[data-status='offline'] {
    background: var(--synex-offline);
  }

  .status-text[data-status='offline'] {
    color: var(--synex-offline);
  }
</style>