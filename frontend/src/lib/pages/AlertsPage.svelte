<script>
  import { onMount } from 'svelte';
  import { BACKEND_URL } from '../api/config.js';
  import { onEvent } from '../ws.js';
  import { selectedServerId } from '../stores.js';

  export let onOpenDashboard = () => {};
  export let onUnreadCount = () => {};

  let alerts = [];
  let counts = { active: 0, critical: 0, warning: 0, unacknowledged: 0, resolved: 0 };
  let loading = true;
  let error = '';
  let requestVersion = 0;

  function isRecord(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
  }

  function isAlert(value) {
    return (
      isRecord(value) &&
      typeof value.id === 'string' &&
      typeof value.server === 'string' &&
      typeof value.title === 'string' &&
      typeof value.message === 'string' &&
      (value.severity === 'critical' || value.severity === 'warning') &&
      (value.status === 'active' || value.status === 'resolved') &&
      typeof value.acknowledged === 'boolean'
    );
  }

  function isCounts(value) {
    return (
      isRecord(value) &&
      ['active', 'critical', 'warning', 'unacknowledged', 'resolved'].every(
        (key) => Number.isInteger(value[key]) && value[key] >= 0
      )
    );
  }

  function errorMessage(error) {
    return error instanceof Error ? error.message : 'Unexpected backend error.';
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
      throw new Error(
        typeof data?.detail === 'string' ? data.detail : `Backend returned HTTP ${response.status}.`
      );
    }
    return data;
  }

  async function loadAlerts() {
    const version = ++requestVersion;
    loading = true;
    error = '';
    try {
      const data = await request('/api/alerts?status=active');
      if (
        !isRecord(data) ||
        !isCounts(data.counts) ||
        !Array.isArray(data.alerts) ||
        data.alerts.some((alert) => !isAlert(alert))
      ) {
        throw new Error('Backend returned invalid alert data.');
      }
      if (version !== requestVersion) return false;
      alerts = data.alerts;
      counts = data.counts;
      onUnreadCount(counts.unacknowledged);
      return true;
    } catch (requestError) {
      if (version === requestVersion) error = errorMessage(requestError);
      return false;
    } finally {
      if (version === requestVersion) loading = false;
    }
  }

  async function acknowledgeActiveAlerts() {
    try {
      const data = await request('/api/alerts/ack-all', { method: 'POST' });
      if (!isRecord(data) || !isCounts(data.counts)) {
        throw new Error('Backend returned invalid acknowledgement data.');
      }
      counts = data.counts;
      alerts = alerts.map((alert) =>
        alert.status === 'active' ? { ...alert, acknowledged: true } : alert
      );
      onUnreadCount(counts.unacknowledged);
    } catch (requestError) {
      error = `Could not mark active alerts as seen: ${errorMessage(requestError)}`;
    }
  }

  async function openAlerts() {
    if (await loadAlerts()) await acknowledgeActiveAlerts();
  }

  function handleAlertEvent(event) {
    if (
      !isRecord(event) ||
      !['fired', 'updated', 'resolved', 'acknowledged'].includes(event.action) ||
      !isAlert(event.alert)
    ) {
      return;
    }
    void loadAlerts();
  }

  function openServer(alert) {
    selectedServerId.set(alert.server);
    onOpenDashboard(alert.server);
  }

  function unresolvedLabel(count) {
    return `${count} unresolved ${count === 1 ? 'problem' : 'problems'}`;
  }

  function alertIcon(severity) {
    return severity === 'critical' ? '/icons/redalert.svg' : '/icons/yellowallert.svg';
  }

  onMount(() => {
    const unsubscribe = onEvent('alert', handleAlertEvent);
    void openAlerts();
    return unsubscribe;
  });
</script>

<div class="alerts-page">
  <section class="alerts-panel" aria-label="Active alerts">
    <header class="alerts-head">
      <div>
        <h1>Active Alerts</h1>
        <p>{unresolvedLabel(counts.active)}</p>
      </div>
    </header>

    {#if error}
      <div class="alerts-error" role="alert">{error}</div>
    {/if}

    <div class="alerts-list" aria-live="polite">
      {#if loading && alerts.length === 0}
        <p class="alerts-message">Loading alerts…</p>
      {:else if alerts.length === 0}
        <p class="alerts-message">
          No active alerts.
        </p>
      {:else}
        {#each alerts as alert (alert.id)}
          <button
            type="button"
            class="alert-card"
            class:alert-card--critical={alert.severity === 'critical'}
            class:alert-card--warning={alert.severity === 'warning'}
            on:click={() => openServer(alert)}
            aria-label={`${alert.title}. ${alert.message}. Open server ${alert.server} on Dashboard.`}
          >
            <img class="alert-icon" src={alertIcon(alert.severity)} alt="" />
            <span class="alert-copy">
              <span class="alert-title">{alert.title}</span>
              <span class="alert-description">{alert.message}</span>
            </span>
          </button>
        {/each}
      {/if}
    </div>
  </section>
</div>

<style>
  .alerts-page {
    display: flex;
    justify-content: center;
    flex: 1 1 auto;
    min-height: 0;
    padding: 6px 14px 0;
  }

  .alerts-panel {
    display: flex;
    flex-direction: column;
    width: 100%;
    max-width: 982px;
    height: min(580px, calc(100vh - 188px));
    min-height: 340px;
    overflow: hidden;
    border: 1px solid var(--synex-border-default);
    border-radius: 15px;
    background: var(--synex-bg-surface);
  }

  .alerts-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--synex-space-4);
    min-height: 76px;
    padding: 16px 34px 14px;
    border-bottom: 1px solid var(--synex-border-default);
  }

  .alerts-head h1,
  .alerts-head p {
    margin: 0;
  }

  .alerts-head h1 {
    font-size: 16px;
    font-weight: 600;
    line-height: 1.4;
  }

  .alerts-head p {
    color: var(--synex-text-muted);
    font-size: 11px;
    font-weight: 600;
    line-height: 1.4;
  }

  .alerts-error {
    margin: var(--synex-space-4) var(--synex-space-6) 0;
    padding: var(--synex-space-3) var(--synex-space-4);
    border: 1px solid var(--synex-danger);
    border-radius: var(--synex-radius-sm);
    background: var(--synex-danger-bg);
    color: var(--synex-danger-fg);
  }

  .alerts-list {
    display: flex;
    flex: 1 1 auto;
    flex-direction: column;
    gap: 30px;
    min-height: 0;
    overflow: auto;
    padding: 34px 25px;
  }

  .alert-card {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 20px;
    min-height: 101px;
    padding: 17px 22px;
    border: 1px solid;
    border-radius: 7px;
    color: var(--synex-text-primary);
    text-align: left;
    cursor: pointer;
  }

  .alert-card--critical {
    border-color: #aa2e2e;
    background: rgba(170, 46, 46, 0.12);
  }

  .alert-card--warning {
    border-color: #fc0;
    background: rgba(255, 204, 0, 0.28);
  }

  .alert-card:hover {
    filter: brightness(1.08);
  }

  .alert-icon {
    flex: 0 0 48px;
    width: 48px;
    height: 48px;
    object-fit: contain;
  }

  .alert-copy {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 2px;
  }

  .alert-title {
    font-size: 16px;
    font-weight: 500;
    line-height: 1.4;
  }

  .alert-description {
    color: var(--synex-text-secondary);
    font-size: 12px;
    line-height: 1.4;
    overflow-wrap: anywhere;
  }

  .alerts-message {
    margin: 0;
    color: var(--synex-text-muted);
    text-align: center;
  }

  @media (max-width: 700px) {
    .alerts-page {
      padding: 0;
    }

    .alerts-panel {
      height: auto;
      min-height: 360px;
    }

    .alerts-head {
      align-items: flex-start;
      padding: var(--synex-space-4);
    }

    .alerts-list {
      gap: var(--synex-space-4);
      padding: var(--synex-space-4);
    }
  }
</style>
