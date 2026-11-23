<script>
  import { onMount } from 'svelte';
  import { BACKEND_URL } from '../api/config.js';
  import { selectedServerId, servers } from '../stores.js';

  export let onAddServer = () => {};

  const LOG_LINES = 200;
  const MAX_LOG_ENTRIES = 2000;
  const severityFilters = [
    { id: 'all', label: 'ALL' },
    { id: 'info', label: 'INFO' },
    { id: 'warn', label: 'WARN' },
    { id: 'error', label: 'ERROR' }
  ];

  let sourceInfo = null;
  let selectedSeverity = 'all';
  let entries = [];
  let sourceLoading = false;
  let streamStatus = '';
  let sourcesError = '';
  let sourceRequest = 0;
  let streamSocket = null;
  let reconnectTimer = null;
  let reconnectDelay = 1000;
  let streamVersion = 0;
  let streamFatal = false;
  let mounted = false;
  let loadingOlder = false;
  let hasMore = false;
  let cursor = '';
  let logViewport;

  $: currentServerId = $selectedServerId || $servers[0]?.id || '';
  $: currentServer = $servers.find((server) => server.id === currentServerId) || null;
  $: serverLabel = currentServer?.host || currentServerId;

  $: if (currentServerId !== requestedServerId) {
    requestedServerId = currentServerId;
    void loadSources(currentServerId);
  }

  let requestedServerId = '';

  function isRecord(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
  }

  function errorMessage(error) {
    return error instanceof Error ? error.message : 'Unexpected backend error.';
  }

  async function request(path) {
    let response;
    try {
      response = await fetch(`${BACKEND_URL}${path}`);
    } catch (error) {
      throw new Error(`Could not reach backend at ${BACKEND_URL}: ${errorMessage(error)}`);
    }

    const data = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(
        typeof data?.detail === 'string' ? data.detail : `Backend returned HTTP ${response.status}.`
      );
    }
    return data;
  }

  function sourceAvailable() {
    return sourceInfo?.journal?.available === true;
  }

  function sourcePath(serverId, kind) {
    return `${BACKEND_URL}/api/logs/servers/${encodeURIComponent(serverId)}/${kind}`;
  }

  function stopStream() {
    streamVersion += 1;
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
    const socket = streamSocket;
    streamSocket = null;
    if (socket) socket.close();
  }

  function resetStreamData() {
    entries = [];
    cursor = '';
    hasMore = false;
    loadingOlder = false;
    streamStatus = '';
    if (logViewport) logViewport.scrollTop = 0;
  }

  async function loadSources(serverId) {
    const version = ++sourceRequest;
    stopStream();
    resetStreamData();
    sourceInfo = null;
    sourcesError = '';

    if (!serverId) {
      sourceLoading = false;
      return;
    }

    sourceLoading = true;
    try {
      const data = await request(`/api/logs/servers/${encodeURIComponent(serverId)}/sources`);
      if (
        !isRecord(data) ||
        !isRecord(data.journal) ||
        typeof data.journal.available !== 'boolean' ||
        !Array.isArray(data.files)
      ) {
        throw new Error('Backend returned invalid log source data.');
      }
      if (version !== sourceRequest) return;
      sourceInfo = data;
      if (sourceAvailable()) startStream();
    } catch (error) {
      if (version === sourceRequest) sourcesError = errorMessage(error);
    } finally {
      if (version === sourceRequest) sourceLoading = false;
    }
  }

  function streamUrl(serverId) {
    const query = new URLSearchParams({ lines: String(LOG_LINES) });
    if (selectedSeverity !== 'all') query.set('severity', selectedSeverity);
    if (cursor) query.set('after', cursor);
    return `${sourcePath(serverId, 'journal/ws')}?${query}`;
  }

  function startStream() {
    stopStream();
    streamFatal = false;
    reconnectDelay = 1000;
    openStream(streamVersion);
  }

  function openStream(version) {
    if (!mounted || version !== streamVersion || !currentServerId || !sourceAvailable()) return;

    const endpoint = streamUrl(currentServerId);
    streamStatus = 'Connecting…';
    const url = endpoint.replace(/^http/, 'ws');
    let socket;
    try {
      socket = new WebSocket(url);
    } catch (error) {
      streamStatus = `Could not connect to log stream: ${errorMessage(error)}`;
      scheduleReconnect(version);
      return;
    }

    streamSocket = socket;
    socket.onopen = () => {
      if (streamSocket === socket) streamStatus = 'Live';
    };
    socket.onmessage = (event) => {
      if (streamSocket !== socket) return;
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        streamStatus = 'Log stream returned invalid JSON.';
        return;
      }
      handleStreamMessage(message);
    };
    socket.onerror = () => {
      if (streamSocket === socket) streamStatus = 'Log stream connection failed.';
      socket.close();
    };
    socket.onclose = (event) => {
      if (streamSocket !== socket) return;
      streamSocket = null;
      if (streamFatal || !mounted || version !== streamVersion) return;
      streamStatus = `Log stream disconnected (code ${event.code}); reconnecting…`;
      scheduleReconnect(version);
    };
  }

  function scheduleReconnect(version) {
    if (reconnectTimer || streamFatal || !mounted || version !== streamVersion) return;
    const delay = reconnectDelay;
    reconnectDelay = Math.min(reconnectDelay * 2, 10000);
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      openStream(version);
    }, delay);
  }

  function addJournalEntries(incoming) {
    if (!Array.isArray(incoming)) {
      streamStatus = 'Log stream returned invalid journal entries.';
      return;
    }

    const known = new Set(entries.map((entry) => entry.cursor));
    const added = [];
    for (const entry of incoming) {
      if (
        !isRecord(entry) ||
        typeof entry.cursor !== 'string' ||
        typeof entry.time !== 'number' ||
        typeof entry.severity !== 'string' ||
        typeof entry.message !== 'string'
      ) {
        streamStatus = 'Log stream returned malformed journal entries.';
        continue;
      }
      if (!known.has(entry.cursor)) {
        known.add(entry.cursor);
        added.push(entry);
      }
    }
    entries = [...entries, ...added].slice(-MAX_LOG_ENTRIES);
    if (entries.length > 0) {
      hasMore = hasMore || entries.length >= LOG_LINES;
    }
  }

  function handleStreamMessage(message) {
    if (!isRecord(message) || typeof message.type !== 'string' || !isRecord(message.data)) {
      streamStatus = 'Log stream returned an invalid message.';
      return;
    }

    if (message.type === 'hello') {
      reconnectDelay = 1000;
      streamStatus = 'Live';
      return;
    }

    if (message.type === 'entries') {
      const data = message.data;
      addJournalEntries(data.entries);
      if (typeof data.cursor === 'string') cursor = data.cursor;
      if (data.truncated === true) hasMore = true;
      return;
    }

    if (message.type === 'error') {
      const detail = typeof message.data.detail === 'string'
        ? message.data.detail
        : 'Log stream error.';
      streamStatus = detail;
      if (message.data.fatal === true) {
        streamFatal = true;
        streamSocket?.close();
      }
      return;
    }

    if (message.type === 'closed') {
      if (message.data.reason === 'server_disconnected') {
        streamFatal = true;
        streamStatus = 'SSH session to this server was closed.';
        streamSocket?.close();
      }
    }
  }

  async function loadOlder() {
    if (
      !hasMore ||
      loadingOlder ||
      !entries.length ||
      !entries[0].cursor
    ) {
      return;
    }

    const before = entries[0].cursor;
    loadingOlder = true;
    try {
      const query = new URLSearchParams({ lines: String(LOG_LINES), before });
      if (selectedSeverity !== 'all') query.set('severity', selectedSeverity);
      const data = await request(
        `/api/logs/servers/${encodeURIComponent(currentServerId)}/journal?${query}`
      );
      if (!isRecord(data) || !Array.isArray(data.entries) || typeof data.more !== 'boolean') {
        throw new Error('Backend returned an invalid older log page.');
      }
      const known = new Set(entries.map((entry) => entry.cursor));
      const older = data.entries.filter(
        (entry) => isRecord(entry) && typeof entry.cursor === 'string' && !known.has(entry.cursor)
      );
      entries = [...older, ...entries].slice(-MAX_LOG_ENTRIES);
      hasMore = data.more;
    } catch (error) {
      streamStatus = `Could not load older log entries: ${errorMessage(error)}`;
    } finally {
      loadingOlder = false;
    }
  }

  function handleScroll(event) {
    if (event.currentTarget.scrollTop <= 0) void loadOlder();
  }

  function changeSeverity(severity) {
    if (selectedSeverity === severity) return;
    selectedSeverity = severity;
    resetStreamData();
    if (sourceAvailable()) startStream();
  }

  function formatTime(value) {
    if (!Number.isFinite(value)) return '—';
    const date = new Date(value);
    return [date.getHours(), date.getMinutes(), date.getSeconds()]
      .map((part) => String(part).padStart(2, '0'))
      .join(':');
  }

  function entrySource(entry) {
    return entry.unit || entry.identifier || '—';
  }

  function severityClass(severity) {
    if (severity === 'error') return 'error';
    if (severity === 'warn') return 'warn';
    if (severity === 'debug') return 'debug';
    return 'info';
  }

  onMount(() => {
    mounted = true;
    if (sourceInfo && sourceAvailable()) startStream();
    return () => {
      mounted = false;
      stopStream();
    };
  });
</script>

{#if $servers.length === 0}
  <div class="logs-empty">
    <span>No servers yet</span>
    <button type="button" class="logs-add-server" on:click={onAddServer}>Add new server</button>
  </div>
{:else}
  <section class="logs-page" aria-label="System logs">
    <div class="logs-panel">
      <header class="logs-head">
        <div class="logs-heading">
          <h1>System logs</h1>
          <span class="logs-server-name">{serverLabel}</span>
        </div>
        <div class="logs-controls">
          <div class="severity-filters" aria-label="Filter by severity">
            {#each severityFilters as filter (filter.id)}
              <button
                type="button"
                class:severity-filter--active={selectedSeverity === filter.id}
                aria-pressed={selectedSeverity === filter.id}
                on:click={() => changeSeverity(filter.id)}
              >{filter.label}</button>
            {/each}
          </div>
        </div>
      </header>

      {#if sourceInfo?.journal?.available && sourceInfo.journal.access !== 'all'}
        <div class="logs-access-warning" role="status">
          {sourceInfo.journal.detail || 'Journal access is limited for the SSH user. Add it to the systemd-journal or adm group.'}
        </div>
      {:else if sourceInfo && !sourceInfo.journal.available}
        <div class="logs-access-warning" role="status">
          {sourceInfo.files.length > 0
            ? 'Systemd journal is unavailable. File log selection is not available in this screen.'
            : sourceInfo.files_detail || 'No systemd journal or readable log files are available.'}
        </div>
      {/if}

      {#if sourcesError}
        <div class="logs-error" role="alert">{sourcesError}</div>
      {/if}

      <div class="logs-status" role="status" aria-live="polite">
        {sourceLoading ? 'Loading log sources…' : streamStatus}
      </div>

      <div class="logs-rows" bind:this={logViewport} on:scroll={handleScroll}>
        {#if entries.length === 0 && !sourceLoading && !sourcesError}
          <p class="logs-no-entries">
            No log entries.
          </p>
        {:else}
          {#each entries as entry, index (`${entry.cursor || entry.text}:${index}`)}
            {#if entry.marker}
              <div class="log-rotation-marker">{entry.text}</div>
            {:else}
              <div class="log-row">
                <span class="log-time">{formatTime(entry.time)}</span>
                <span class={`log-severity log-severity--${severityClass(entry.severity)}`}>
                  {String(entry.severity || 'info').toUpperCase()}
                </span>
                <span class="log-source" title={entrySource(entry)}>{entrySource(entry)}</span>
                <span class="log-message" title={entry.message || entry.text}>
                  {entry.message || entry.text}
                </span>
              </div>
            {/if}
          {/each}
          {#if loadingOlder}
            <p class="logs-loading-older">Loading older entries…</p>
          {/if}
        {/if}
      </div>

      <footer class="logs-footer">
        <span class="logs-server-chip">
          <span class="logs-live-dot" aria-hidden="true"></span>
          {serverLabel}
        </span>
        <button
          type="button"
          class="logs-add-session"
          on:click={onAddServer}
          aria-label="Add server"
          title="Add server"
        >+</button>
      </footer>
    </div>
  </section>
{/if}

<style>
  .logs-page {
    display: flex;
    flex: 1 1 auto;
    min-height: 0;
    font-family: var(--font-host);
  }

  .logs-panel {
    display: flex;
    flex: 1 1 auto;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    overflow: hidden;
    border: 1px solid var(--synex-border-default);
    border-radius: 15px;
    background: var(--synex-bg-surface);
  }

  .logs-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--synex-space-4);
    min-height: 89px;
    padding: 18px 26px 18px 34px;
    border-bottom: 1px solid var(--synex-border-default);
  }

  .logs-heading {
    display: flex;
    align-items: baseline;
    gap: var(--synex-space-2);
    min-width: 0;
  }

  .logs-heading h1 {
    flex: 0 0 auto;
    margin: 0;
    font-size: 22px;
    font-weight: 400;
    line-height: 1.3;
  }

  .logs-server-name {
    overflow: hidden;
    color: var(--synex-text-muted);
    font-size: 13px;
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .logs-controls {
    display: flex;
    align-items: center;
    gap: var(--synex-space-3);
  }

  .severity-filters {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .severity-filters button {
    min-width: 60px;
    padding: 5px 8px;
    border: 1px solid var(--synex-border-default);
    border-radius: 8px;
    color: var(--synex-text-muted);
    font-family: var(--font-host);
    font-size: 13px;
    font-weight: 600;
    line-height: 1.2;
  }

  .severity-filters button:hover {
    border-color: var(--synex-accent);
  }

  .severity-filters .severity-filter--active {
    border-color: var(--synex-accent);
    color: var(--synex-accent);
  }

  .logs-access-warning,
  .logs-error {
    margin: var(--synex-space-3) var(--synex-space-6) 0;
    padding: var(--synex-space-2) var(--synex-space-3);
    border: 1px solid var(--synex-warning);
    border-radius: var(--synex-radius-sm);
    background: var(--synex-warning-bg);
    color: var(--synex-text-secondary);
    font-size: 12px;
  }

  .logs-error {
    border-color: var(--synex-danger);
    background: var(--synex-danger-bg);
    color: var(--synex-danger-fg);
  }

  .logs-status {
    flex: 0 0 auto;
    min-height: 20px;
    padding: 4px var(--synex-space-6) 0;
    color: var(--synex-text-muted);
    font-size: 11px;
  }

  .logs-rows {
    flex: 1 1 auto;
    min-height: 0;
    overflow: auto;
    padding: 8px 26px 14px;
  }

  .log-row {
    display: grid;
    grid-template-columns: 140px 106px minmax(120px, 160px) minmax(0, 1fr);
    align-items: baseline;
    gap: 24px;
    min-height: 48px;
    padding: 11px 0;
    font-size: 16px;
  }

  .log-time {
    color: var(--synex-text-muted);
    font-weight: 700;
    text-align: left;
  }

  .log-severity {
    font-weight: 700;
    text-align: left;
  }

  .log-severity--info {
    color: #00c853;
  }

  .log-severity--warn {
    color: #ffcc00;
  }

  .log-severity--error {
    color: #e53935;
  }

  .log-severity--debug {
    color: var(--synex-text-muted);
  }

  .log-source {
    overflow: hidden;
    color: var(--synex-text-muted);
    font-weight: 700;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .log-message {
    overflow-wrap: anywhere;
    color: var(--synex-text-secondary);
  }

  .log-rotation-marker,
  .logs-loading-older,
  .logs-no-entries {
    margin: 12px 0;
    color: var(--synex-text-muted);
    font-size: 13px;
    text-align: center;
  }

  .logs-footer {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    gap: var(--synex-space-4);
    padding: 0 16px 18px;
  }

  .logs-server-chip {
    display: inline-flex;
    align-items: center;
    gap: 22px;
    min-height: 40px;
    padding: 6px 16px;
    border: 1px solid var(--synex-accent);
    border-radius: 7px;
    background: rgba(115, 68, 31, 0.6);
    color: var(--synex-text-primary);
    font-size: 16px;
    font-weight: 600;
  }

  .logs-live-dot {
    width: 13px;
    height: 13px;
    border-radius: 50%;
    background: var(--synex-ok);
  }

  .logs-add-session {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    border: 1px solid var(--synex-accent);
    border-radius: 50%;
    color: var(--synex-accent);
    font-size: 22px;
    line-height: 1;
  }

  .logs-add-session:hover {
    background: var(--synex-bg-active);
  }

  .logs-empty {
    display: flex;
    flex: 1 1 auto;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--synex-space-4);
    color: var(--synex-text-muted);
  }

  .logs-add-server {
    padding: 9px 18px;
    border: 1px solid var(--synex-accent);
    border-radius: var(--synex-radius-sm);
    background: var(--synex-accent);
    color: var(--synex-accent-on);
    font-weight: 600;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  @media (max-width: 900px) {
    .logs-head {
      align-items: flex-start;
      flex-direction: column;
    }

    .logs-controls {
      flex-wrap: wrap;
    }

    .log-row {
      grid-template-columns: 90px 70px minmax(100px, 1fr);
      gap: var(--synex-space-2);
      font-size: 13px;
    }

    .log-message {
      grid-column: 1 / -1;
      padding-left: 4px;
    }
  }
</style>
