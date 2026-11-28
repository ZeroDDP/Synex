<script>
  import { onMount } from 'svelte';
  import { BACKEND_URL } from '../api/config.js';
  import { selectedServerId, servers } from '../stores.js';
  import { onEvent } from '../ws.js';

  let sessions = [];
  let sessionsError = '';
  let sessionOverride = '';
  let observedServerId = '';
  let containers = [];
  let stats = [];
  let dockerInfo = null;
  let loading = false;
  let error = '';
  let busyActions = {};
  let logsContainer = '';
  let logs = [];
  let logsLoading = false;
  let logsError = '';
  let requestVersion = 0;
  let sessionsRequestVersion = 0;
  let logsVersion = 0;
  let requestedSessionId = '';

  $: currentServerId = $selectedServerId || $servers[0]?.id || '';
  $: if (currentServerId !== observedServerId) {
    observedServerId = currentServerId;
    sessionOverride = '';
  }
  $: currentServer = $servers.find((server) => server.id === currentServerId) || null;
  $: matchingSession = sessions.find((session) => sessionMatchesServer(session, currentServer));
  $: currentSessionId =
    sessionOverride ||
    matchingSession?.session_id ||
    (!$selectedServerId && sessions.length === 1 ? sessions[0].session_id : '');
  $: if (currentSessionId !== requestedSessionId) {
    requestedSessionId = currentSessionId;
    void loadDocker(currentSessionId);
  }
  $: statsByContainer = new Map(
    stats.flatMap((sample) => [
      [sample.id, sample],
      [sample.name, sample]
    ])
  );
  $: serverLabel = currentServer
    ? `${currentServer.name}${currentServer.host ? ` · ${currentServer.host}` : ''}`
    : 'No server selected';

  onMount(() => {
    const unsubscribe = onEvent('ssh.session', () => void loadSessions());
    void loadSessions();
    return unsubscribe;
  });

  function isRecord(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
  }

  function errorMessage(requestError) {
    return requestError instanceof Error ? requestError.message : 'Unexpected backend error.';
  }

  function normalizedAddress(host, port) {
    return `${String(host || '').toLowerCase()}:${Number(port || 22)}`;
  }

  function sessionMatchesServer(session, server) {
    if (!server) return false;
    const addresses = [
      currentServerId,
      server.host,
      server.name,
      `${server.name}:${session.port || 22}`
    ].map((address) => String(address || '').toLowerCase());
    return addresses.includes(normalizedAddress(session.host, session.port)) ||
      addresses.includes(String(session.host || '').toLowerCase());
  }

  async function request(path, options = {}) {
    let response;
    try {
      response = await fetch(`${BACKEND_URL}${path}`, options);
    } catch (requestError) {
      throw new Error(`Could not reach backend at ${BACKEND_URL}: ${errorMessage(requestError)}`);
    }

    const data = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(
        typeof data?.detail === 'string' ? data.detail : `Backend returned HTTP ${response.status}.`
      );
    }
    return data;
  }

  async function loadSessions() {
    const version = ++sessionsRequestVersion;
    sessionsError = '';
    try {
      const data = await request('/api/ssh/sessions');
      if (
        !Array.isArray(data) ||
        data.some(
          (session) =>
            !isRecord(session) ||
            typeof session.session_id !== 'string' ||
            typeof session.host !== 'string' ||
            !Number.isInteger(session.port)
        )
      ) {
        throw new Error('Backend returned an invalid SSH session list.');
      }
      if (version !== sessionsRequestVersion) return;
      sessions = data;
      if (sessionOverride && !sessions.some((session) => session.session_id === sessionOverride)) {
        sessionOverride = '';
      }
    } catch (requestError) {
      if (version === sessionsRequestVersion) sessionsError = errorMessage(requestError);
    }
  }

  async function loadDocker(sessionId) {
    const version = ++requestVersion;
    containers = [];
    stats = [];
    dockerInfo = null;
    error = '';
    logsContainer = '';
    logs = [];
    if (!sessionId) {
      loading = false;
      return;
    }

    loading = true;
    try {
      const base = `/api/docker/sessions/${encodeURIComponent(sessionId)}`;
      const info = await request(`${base}/info`);
      if (!isRecord(info) || typeof info.status !== 'string') {
        throw new Error('Backend returned invalid Docker information.');
      }
      if (version !== requestVersion) return;
      dockerInfo = info;
      if (info.status !== 'ok') {
        error = info.detail || `Docker is ${info.status.replaceAll('_', ' ')}.`;
        return;
      }

      const [containerData, statsData] = await Promise.all([
        request(`${base}/containers?all=true`),
        request(`${base}/stats`)
      ]);
      if (
        !Array.isArray(containerData) ||
        containerData.some(
          (container) =>
            !isRecord(container) ||
            typeof container.id !== 'string' ||
            typeof container.name !== 'string' ||
            typeof container.image !== 'string' ||
            typeof container.state !== 'string'
        ) ||
        !Array.isArray(statsData)
      ) {
        throw new Error('Backend returned invalid Docker container data.');
      }
      if (version !== requestVersion) return;
      containers = containerData;
      stats = statsData;
    } catch (requestError) {
      if (version === requestVersion) error = errorMessage(requestError);
    } finally {
      if (version === requestVersion) loading = false;
    }
  }

  function refresh() {
    if (currentSessionId) void loadDocker(currentSessionId);
    else void loadSessions();
  }

  function sampleFor(container) {
    return statsByContainer.get(container.id) || statsByContainer.get(container.name) || null;
  }

  function formatCpu(sample) {
    return typeof sample?.cpu_percent === 'number' ? `${sample.cpu_percent.toFixed(1)}%` : '—';
  }

  function formatMemory(sample) {
    if (typeof sample?.memory_used !== 'number') return '—';
    const megabytes = sample.memory_used / (1024 * 1024);
    return `${megabytes < 10 ? megabytes.toFixed(1) : Math.round(megabytes)} MB`;
  }

  async function runAction(container, action) {
    if (!currentSessionId) return;
    if (
      action === 'remove' &&
      !window.confirm(`Remove container "${container.name}"? This cannot be undone.`)
    ) {
      return;
    }

    busyActions = { ...busyActions, [container.id]: true };
    error = '';
    try {
      const base =
        `/api/docker/sessions/${encodeURIComponent(currentSessionId)}` +
        `/containers/${encodeURIComponent(container.id)}`;
      if (action === 'remove') {
        await request(`${base}?force=true`, { method: 'DELETE' });
      } else {
        await request(`${base}/${action}`, { method: 'POST' });
      }
      await loadDocker(currentSessionId);
    } catch (requestError) {
      error = errorMessage(requestError);
    } finally {
      const { [container.id]: _busy, ...remaining } = busyActions;
      busyActions = remaining;
    }
  }

  async function toggleLogs(container) {
    if (logsContainer === container.id) {
      logsContainer = '';
      logs = [];
      return;
    }

    logsContainer = container.id;
    logs = [];
    logsError = '';
    logsLoading = true;
    const version = ++logsVersion;
    try {
      const data = await request(
        `/api/docker/sessions/${encodeURIComponent(currentSessionId)}` +
          `/containers/${encodeURIComponent(container.id)}/logs?tail=100`
      );
      if (
        !isRecord(data) ||
        !Array.isArray(data.lines) ||
        data.lines.some((line) => !isRecord(line) || typeof line.text !== 'string')
      ) {
        throw new Error('Backend returned invalid container logs.');
      }
      if (version === logsVersion) logs = data.lines;
    } catch (requestError) {
      if (version === logsVersion) logsError = errorMessage(requestError);
    } finally {
      if (version === logsVersion) logsLoading = false;
    }
  }

  function stateClass(state) {
    if (state === 'running') return 'running';
    if (state === 'paused' || state === 'restarting') return 'warning';
    if (state === 'dead' || state === 'removing') return 'critical';
    return 'stopped';
  }
</script>

<section class="docker-page" aria-label="Docker containers">
  <div class="docker-panel">
    <header class="docker-head">
      <div class="docker-heading">
        <h1>DOCKER CONTAINERS</h1>
        <span class="server-label" title={serverLabel}>{serverLabel}</span>
      </div>
      <div class="docker-controls">
        {#if sessions.length > 1}
          <label class="session-picker">
            <span class="sr-only">SSH session</span>
            <select
              value={currentSessionId}
              on:change={(event) => (sessionOverride = event.currentTarget.value)}
            >
              <option value="">Select SSH session</option>
              {#each sessions as session (session.session_id)}
                <option value={session.session_id}>
                  {session.username}@{session.host}:{session.port}
                </option>
              {/each}
            </select>
          </label>
        {/if}
        <button type="button" class="refresh-button" on:click={refresh} disabled={loading}>
          <img src="/icons/docker/refresh_icon.svg" alt="" aria-hidden="true" />
          Refresh
        </button>
        <button
          type="button"
          class="deploy-button"
          disabled
          title="Container deployment is not available in the Docker API yet."
        >+ Deploy</button>
      </div>
    </header>

    {#if sessionsError}
      <div class="docker-message docker-error" role="alert">{sessionsError}</div>
    {:else if error}
      <div class="docker-message docker-error" role="alert">{error}</div>
    {:else if dockerInfo?.server_version}
      <div class="docker-version">Docker {dockerInfo.server_version}</div>
    {/if}

    {#if !currentSessionId && !sessionsError}
      <div class="docker-empty">
        {#if sessions.length === 0}
          <span>No active SSH sessions. Connect to a server to view its Docker containers.</span>
        {:else}
          <span>Select or connect to an SSH session for the selected server.</span>
          {#if sessions.length === 1}
            <button type="button" class="session-action" on:click={() => (sessionOverride = sessions[0].session_id)}>
              Use {sessions[0].username}@{sessions[0].host}
            </button>
          {/if}
        {/if}
      </div>
    {:else}
      <div class="docker-table" role="table" aria-label="Docker containers">
        <div class="table-row table-head" role="row">
          <span class="cell cell-container" role="columnheader">CONTAINER</span>
          <span class="cell cell-image" role="columnheader">IMAGE</span>
          <span class="cell cell-status" role="columnheader">STATUS</span>
          <span class="cell cell-cpu" role="columnheader">CPU</span>
          <span class="cell cell-memory" role="columnheader">MEMORY</span>
          <span class="cell cell-actions" role="columnheader">ACTIONS</span>
        </div>

        {#if loading && containers.length === 0}
          <p class="docker-empty table-empty" role="status">Loading containers…</p>
        {:else if containers.length === 0 && !error}
          <p class="docker-empty table-empty">No containers found on this server.</p>
        {/if}

        {#each containers as container (container.id)}
          {@const sample = sampleFor(container)}
          {@const busy = Boolean(busyActions[container.id])}
          {@const running = container.state === 'running'}
          {@const paused = container.state === 'paused'}
          <div class="container-entry">
            <div class="table-row container-row" role="row">
              <span class="cell cell-container container-name" role="cell" title={container.name}>
                {container.name}
              </span>
              <span class="cell cell-image" role="cell" title={container.image}>{container.image}</span>
              <span class="cell cell-status status-cell" role="cell">
                <span class="status-dot" data-state={stateClass(container.state)}></span>
                <span class="status-label" data-state={stateClass(container.state)}>
                  {container.state.toUpperCase()}
                </span>
              </span>
              <span class="cell cell-cpu" role="cell">{formatCpu(sample)}</span>
              <span class="cell cell-memory" role="cell">{formatMemory(sample)}</span>
              <span class="cell cell-actions action-group" role="cell">
                <button
                  type="button"
                  class="action-button action-logs"
                  aria-label={`View ${container.name} logs`}
                  title="View logs"
                  aria-pressed={logsContainer === container.id}
                  on:click={() => void toggleLogs(container)}
                >
                  <img src="/icons/docker/doc_icon.svg" alt="" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  class="action-button action-restart"
                  aria-label={`Restart ${container.name}`}
                  title="Restart"
                  disabled={busy || container.state === 'removing' || container.state === 'dead'}
                  on:click={() => void runAction(container, 'restart')}
                >
                  <img src="/icons/docker/refresh_icon.svg" alt="" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  class="action-button action-stop"
                  aria-label={`Stop ${container.name}`}
                  title="Stop"
                  disabled={busy || (!running && !paused)}
                  on:click={() => void runAction(container, 'stop')}
                >
                  <img src="/icons/docker/square_icon.svg" alt="" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  class="action-button action-start"
                  aria-label={`${paused ? 'Resume' : 'Start'} ${container.name}`}
                  title={paused ? 'Resume' : 'Start'}
                  disabled={busy || (paused ? false : running) || container.state === 'removing'}
                  on:click={() => void runAction(container, paused ? 'unpause' : 'start')}
                >
                  <img src="/icons/docker/4_triangle_inside_square_icon.svg" alt="" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  class="action-button action-remove"
                  aria-label={`Remove ${container.name}`}
                  title="Remove container"
                  disabled={busy}
                  on:click={() => void runAction(container, 'remove')}
                >
                  <img src="/icons/docker/bin_icon.svg" alt="" aria-hidden="true" />
                </button>
              </span>
            </div>
            {#if logsContainer === container.id}
              <div class="logs-row">
                {#if logsLoading}
                  <span class="logs-hint">Loading container logs…</span>
                {:else if logsError}
                  <span class="logs-error" role="alert">{logsError}</span>
                {:else if logs.length === 0}
                  <span class="logs-hint">No recent logs.</span>
                {:else}
                  {#each logs as line, index (`${line.time}:${index}`)}
                    <div class="log-line">
                      <time>{line.time}</time>
                      <span class:stderr={line.stream === 'stderr'}>{line.text}</span>
                    </div>
                  {/each}
                {/if}
              </div>
            {/if}
          </div>
        {/each}
      </div>
    {/if}
  </div>
</section>

<style>
  .docker-page {
    display: flex;
    flex: 1 1 auto;
    min-width: 0;
    min-height: 0;
    font-family: var(--font-host);
  }

  .docker-panel {
    display: flex;
    flex: 1 1 auto;
    flex-direction: column;
    min-width: 0;
    min-height: 500px;
    overflow: hidden;
    border: 1px solid var(--synex-border-default);
    border-radius: 15px;
    background: var(--synex-bg-surface);
  }

  .docker-head {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    justify-content: space-between;
    gap: var(--synex-space-4);
    min-height: 76px;
    padding: 14px 26px;
    border-bottom: 1px solid var(--synex-border-default);
  }

  .docker-heading {
    display: flex;
    align-items: baseline;
    gap: var(--synex-space-4);
    min-width: 0;
  }

  .docker-heading h1 {
    flex: 0 0 auto;
    margin: 0;
    font-family: var(--font-host);
    font-size: 16px;
    font-weight: 500;
  }

  .server-label {
    overflow: hidden;
    color: var(--synex-text-muted);
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .docker-controls {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    gap: var(--synex-space-5);
  }

  .session-picker select {
    max-width: 210px;
    padding: 7px 10px;
    border: 1px solid var(--synex-border-strong);
    border-radius: var(--synex-radius-sm);
    background: var(--synex-bg-hover);
    color: var(--synex-text-primary);
    font: inherit;
    font-size: 12px;
  }

  .refresh-button,
  .deploy-button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 9px;
    min-height: 36px;
    padding: 6px 12px;
    border: 1px solid var(--synex-border-strong);
    border-radius: 7px;
    color: var(--synex-text-primary);
    font-family: var(--font-host);
    font-size: 14px;
    font-weight: 600;
    white-space: nowrap;
  }

  .refresh-button {
    background: #222b38;
  }

  .refresh-button img {
    width: 16px;
    height: 17px;
  }

  .refresh-button:hover:not(:disabled) {
    border-color: var(--synex-text-secondary);
    background: #2a3442;
  }

  .refresh-button:disabled {
    cursor: wait;
    opacity: 0.65;
  }

  .deploy-button {
    min-width: 112px;
    border-color: var(--synex-ok);
    background: var(--synex-ok-bg);
    color: var(--synex-ok);
  }

  .deploy-button:disabled {
    cursor: not-allowed;
    opacity: 0.8;
  }

  .docker-version {
    padding: 7px 22px 0;
    color: var(--synex-text-muted);
    font-size: 11px;
  }

  .docker-message {
    margin: 12px 22px 0;
    padding: 9px 12px;
    border: 1px solid var(--synex-danger);
    border-radius: var(--synex-radius-sm);
    background: var(--synex-danger-bg);
    color: var(--synex-danger-fg);
    font-size: 13px;
  }

  .docker-table {
    flex: 1 1 auto;
    min-width: 0;
    overflow: auto;
  }

  .table-row {
    display: grid;
    grid-template-columns: minmax(130px, 1.15fr) minmax(100px, 1fr) minmax(100px, .9fr) minmax(55px, .55fr) minmax(80px, .75fr) minmax(175px, 1.2fr);
    align-items: center;
    gap: clamp(8px, 1.5vw, 22px);
    min-width: 760px;
    padding: 13px 22px;
  }

  .table-head {
    position: sticky;
    top: 0;
    z-index: 1;
    min-height: 60px;
    border-bottom: 1px solid var(--synex-border-default);
    background: var(--synex-bg-surface);
  }

  .cell {
    min-width: 0;
  }

  .table-head .cell {
    color: var(--synex-text-muted);
    font-size: 14px;
    font-weight: 700;
  }

  .container-entry:nth-child(odd) .container-row {
    background: var(--synex-bg-hover);
  }

  .container-row {
    min-height: 48px;
    color: var(--synex-text-secondary);
    font-size: 15px;
  }

  .container-name,
  .cell-image {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .container-name {
    color: var(--synex-text-muted);
    font-weight: 600;
  }

  .status-cell {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .status-dot {
    flex: 0 0 auto;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--synex-offline);
  }

  .status-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .status-dot[data-state='running'] {
    background: var(--synex-ok);
  }

  .status-label[data-state='running'] {
    color: var(--synex-ok);
  }

  .status-dot[data-state='warning'] {
    background: var(--synex-warning);
  }

  .status-label[data-state='warning'] {
    color: var(--synex-warning);
  }

  .status-dot[data-state='critical'] {
    background: var(--synex-danger-fg);
  }

  .status-label[data-state='critical'] {
    color: var(--synex-danger-fg);
  }

  .action-group {
    display: flex;
    align-items: center;
    gap: 5px;
  }

  .action-button {
    display: inline-flex;
    flex: 0 0 30px;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    border: 1px solid var(--synex-border-default);
    border-radius: 7px;
    background: #202630;
    transition: border-color .15s ease, background .15s ease;
  }

  .action-button img {
    display: block;
    max-width: 18px;
    max-height: 18px;
  }

  .action-button:hover:not(:disabled),
  .action-button[aria-pressed='true'] {
    border-color: var(--synex-border-strong);
    background: #293240;
  }

  .action-button:disabled {
    cursor: not-allowed;
    opacity: .42;
  }

  .action-start:hover:not(:disabled) {
    border-color: var(--synex-ok);
    background: var(--synex-ok-bg);
  }

  .action-restart:hover:not(:disabled),
  .action-logs:hover:not(:disabled),
  .action-logs[aria-pressed='true'] {
    border-color: var(--synex-accent);
    background: var(--synex-bg-active);
  }

  .action-remove {
    border-color: var(--synex-danger);
    background: var(--synex-danger-bg);
  }

  .action-remove:hover:not(:disabled) {
    border-color: var(--synex-danger-fg);
    background: rgba(170, 46, 46, .22);
  }

  .logs-row {
    display: flex;
    flex-direction: column;
    gap: 4px;
    max-height: 220px;
    overflow: auto;
    padding: 10px 22px 12px;
    border-top: 1px solid var(--synex-border-subtle);
    background: #101419;
    font-family: var(--font-mono);
    font-size: 11px;
  }

  .log-line {
    display: grid;
    grid-template-columns: 220px minmax(0, 1fr);
    gap: 14px;
  }

  .log-line time,
  .logs-hint {
    color: var(--synex-text-muted);
  }

  .log-line span {
    overflow-wrap: anywhere;
    color: var(--synex-text-secondary);
  }

  .log-line span.stderr,
  .logs-error {
    color: var(--synex-danger-fg);
  }

  .docker-empty {
    display: flex;
    flex: 1 1 auto;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--synex-space-4);
    min-height: 180px;
    padding: 30px;
    color: var(--synex-text-muted);
    font-size: 14px;
    text-align: center;
  }

  .table-empty {
    min-width: 0;
  }

  .session-action {
    padding: 8px 14px;
    border: 1px solid var(--synex-accent);
    border-radius: var(--synex-radius-sm);
    color: var(--synex-accent);
    font: inherit;
  }

  .session-action:hover {
    background: var(--synex-bg-active);
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  button:focus-visible,
  select:focus-visible {
    outline: 2px solid var(--synex-accent);
    outline-offset: 2px;
  }

  @media (max-width: 1050px) {
    .docker-head {
      align-items: flex-start;
      flex-direction: column;
    }

    .docker-controls {
      flex-wrap: wrap;
    }

    .docker-heading {
      flex-wrap: wrap;
    }
  }

  @media (max-width: 700px) {
    .docker-panel {
      min-height: 420px;
    }

    .docker-head {
      padding: 12px 14px;
    }

    .docker-controls {
      gap: var(--synex-space-2);
    }

    .table-row {
      padding-right: 14px;
      padding-left: 14px;
    }

    .action-group {
      gap: 3px;
    }

    .action-button {
      flex-basis: 27px;
      width: 27px;
      height: 27px;
    }
  }
</style>
