<script>
  import { onMount } from 'svelte';
  import Sidebar from './lib/Sidebar.svelte';
  import PageStub from './lib/PageStub.svelte';
  import AlertsPage from './lib/pages/AlertsPage.svelte';
  import DashboardPage from './lib/pages/DashboardPage.svelte';
  import DockerPage from './lib/pages/DockerPage.svelte';
  import FilesPage from './lib/pages/FilesPage.svelte';
  import LogsPage from './lib/pages/LogsPage.svelte';
  import ServersPage from './lib/pages/ServersPage.svelte';
  import TerminalPage from './lib/pages/TerminalPage.svelte';
  import { BACKEND_URL } from './lib/api/config.js';
  import { startSshRecovery } from './lib/sshRecovery.js';
  import  {
    backendState,
    connectWs,
    dismissBackendError,
    disconnectWs,
    onEvent
  } from './lib/ws.js';
  import { selectedServerId, sshRecoveryStatus } from './lib/stores.js';

  function connectionLabel(state) {
    if (state === 'online') return 'Backend connected';
    if (state === 'reconnecting') return 'Reconnecting to backend';
    if (state === 'offline') return 'Backend is offline';
    return 'Connecting to backend';
  }

  const navItems = [
    { id: 'dashboard', label: 'Dashboard', icon: '/icons/_Dashboard%20icons/dashboard.svg' },
    { id: 'servers',   label: 'Servers',   icon: '/icons/_Dashboard%20icons/servers.svg' },
    { id: 'docker',    label: 'Docker',    icon: '/icons/_Dashboard%20icons/icons(docker).svg' },
    { id: 'terminal',  label: 'Terminal',  icon: '/icons/_Dashboard%20icons/icons(terminal).svg' },
    { id: 'logs',      label: 'Logs',      icon: '/icons/_Dashboard%20icons/icons(logs).svg' },
    { id: 'alerts',    label: 'Alerts',    icon: '/icons/_Dashboard%20icons/icons(alerts).svg' },
    { id: 'files',     label: 'Files',     icon: '/icons/_Dashboard%20icons/files.svg' }
  ];

  let activeTab = 'dashboard';
  let connectionReturnTab = 'dashboard';
  let terminalSessionId = '';
  let showConnectForm = false;
  let unreadAlertCount = 0;
  let alertCountError = '';

  onMount(() => {
    const unsubscribe = onEvent('alert', () => void refreshAlertCount());
    const stopSshRecovery = startSshRecovery();
    connectWs();
    void refreshAlertCount();
    return () => {
      unsubscribe();
      stopSshRecovery();
      disconnectWs();
    };
  });

  function labelOf(id) {
    const item = navItems.find((i) => i.id === id);
    return item ? item.label : id;
  }

  async function refreshAlertCount() {
    try {
      const response = await fetch(`${BACKEND_URL}/api/alerts?status=active`);
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(
          typeof data?.detail === 'string' ? data.detail : `Backend returned HTTP ${response.status}.`
        );
      }
      if (!Number.isInteger(data?.counts?.unacknowledged) || data.counts.unacknowledged < 0) {
        throw new Error('Backend returned invalid alert counts.');
      }
      unreadAlertCount = data.counts.unacknowledged;
      alertCountError = '';
    } catch (error) {
      alertCountError = `Could not load alert count: ${
        error instanceof Error ? error.message : 'Unexpected backend error.'
      }`;
    }
  }

  function handleSelect(id) {
    activeTab = id;
  }

  function openDashboardForServer(serverId) {
    selectedServerId.set(serverId);
    activeTab = 'dashboard';
  }

  function openConnectionForm() {
    connectionReturnTab = activeTab;
    activeTab = 'servers';
    showConnectForm = true;
  }

  function handleActiveSessionSelected(session) {
    selectedServerId.set(`${session.host}:${session.port}`);
    if (connectionReturnTab === 'terminal') terminalSessionId = session.session_id;
    showConnectForm = false;
    activeTab = connectionReturnTab;
  }

  function handleSshConnected(session) {
    terminalSessionId = session.session_id;
    // Switch to the terminal tab when SSH connection is established,
    // we can change this behavior later if needed.
    activeTab = 'terminal';
  }
</script>

<div class="shell">
  <Sidebar
    items={navItems}
    active={activeTab}
    alertCount={unreadAlertCount}
    onSelect={handleSelect}
  />

  <main class="workarea">
    <header class="topbar">
      <nav class="breadcrumbs" aria-label="Breadcrumb">
        <span class="crumb">synex</span>
        <span class="sep">/</span>
        <span class="crumb crumb--current">{labelOf(activeTab)}</span>
      </nav>
      <span
        class="connection-indicator"
        data-state={$backendState.state}
        role="img"
        aria-label={connectionLabel($backendState.state)}
        title={connectionLabel($backendState.state)}
      ></span>
    </header>

    <section class="content">
      {#if $backendState.error}
        <div class="backend-error" role="alert">
          <span>{ $backendState.error }</span>
          <button type="button" aria-label="Dismiss message" on:click={dismissBackendError}>
            ×
          </button>
        </div>
      {/if}
      {#if alertCountError}
        <div class="backend-error" role="alert">
          <span>{alertCountError}</span>
          <button type="button" aria-label="Dismiss alert count error" on:click={() => (alertCountError = '')}>
            ×
          </button>
        </div>
      {/if}
      {#if $sshRecoveryStatus.message}
        <div
          class="recovery-message"
          class:recovery-message--error={$sshRecoveryStatus.state === 'error'}
          class:recovery-message--success={$sshRecoveryStatus.state === 'recovered'}
          role="status"
        >
          <span>{$sshRecoveryStatus.message}</span>
          <button
            type="button"
            aria-label="Dismiss SSH recovery message"
            on:click={() => sshRecoveryStatus.set({ state: 'idle', message: '' })}
          >×</button>
        </div>
      {/if}
      {#if activeTab === 'dashboard'}
        <DashboardPage onAddServer={openConnectionForm} />
      {:else if activeTab === 'servers'}
        <ServersPage
          bind:showConnectForm
          onConnected={handleSshConnected}
          onSessionSelected={handleActiveSessionSelected}
        />
      {:else if activeTab === 'docker'}
        <DockerPage />
      {:else if activeTab === 'logs'}
        <LogsPage onAddServer={openConnectionForm} />
      {:else if activeTab === 'alerts'}
        <AlertsPage
          onUnreadCount={(count) => (unreadAlertCount = count)}
          onOpenDashboard={openDashboardForServer}
        />
      {:else if activeTab === 'files'}
        <FilesPage onAddSession={openConnectionForm} />
      {/if}
      <TerminalPage
        initialSessionId={terminalSessionId}
        hidden={activeTab !== 'terminal'}
        onNewSession={() => {
          connectionReturnTab = 'terminal';
          activeTab = 'servers';
          showConnectForm = true;
        }}
      />
    </section>
  </main>
</div>

<style>
  .shell {
    display: flex;
    height: 100%;
    flex-direction: row;
  }

  .workarea {
    flex: 1 1 auto;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    background: var(--synex-bg-app);
  }

  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex: 0 0 auto;
    height: 78px;
    padding-left: 30px;
    padding-right: var(--synex-space-6);
    border-bottom: 1px solid var(--synex-border-subtle);
  }

  .connection-indicator {
    flex: 0 0 auto;
    width: 8px;
    height: 8px;
    margin-left: auto;
    border-radius: 50%;
    background: var(--synex-offline);
  }

  .connection-indicator[data-state='online'] {
    background: var(--synex-ok);
  }

  .connection-indicator[data-state='reconnecting'],
  .connection-indicator[data-state='connecting'] {
    background: var(--synex-warning);
  }

  .connection-indicator[data-state='offline'] {
    background: var(--synex-danger-fg);
  }

  .backend-error {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--synex-space-4);
    margin-bottom: var(--synex-space-4);
    padding: var(--synex-space-3) var(--synex-space-4);
    border: 1px solid var(--synex-danger);
    border-radius: var(--synex-radius-md);
    background: var(--synex-danger-bg);
    color: var(--synex-danger-fg);
  }

  .backend-error button {
    flex: 0 0 auto;
    color: inherit;
    font-size: 18px;
    line-height: 1;
  }

  .recovery-message {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--synex-space-4);
    margin-bottom: var(--synex-space-4);
    padding: var(--synex-space-3) var(--synex-space-4);
    border: 1px solid var(--synex-warning);
    border-radius: var(--synex-radius-md);
    background: var(--synex-warning-bg);
    color: var(--synex-text-secondary);
  }

  .recovery-message--error {
    border-color: var(--synex-danger);
    background: var(--synex-danger-bg);
    color: var(--synex-danger-fg);
  }

  .recovery-message--success {
    border-color: var(--synex-ok);
    background: var(--synex-ok-bg);
    color: var(--synex-ok);
  }

  .recovery-message button {
    flex: 0 0 auto;
    color: inherit;
    font-size: 18px;
    line-height: 1;
  }

  .breadcrumbs {
    display: flex;
    gap: var(--synex-space-2);
    font-size: 12px;
    color: var(--synex-border-default);
  }

  .crumb--current {
    color: var(--synex-text-muted);
  }

  .content {
    flex: 1 1 auto;
    overflow-y: auto;
    min-height: 0;
    padding: 26px 24px 24px 30px;
    display: flex;
    flex-direction: column;
  }
</style>