<script>
  import { onMount, onDestroy } from 'svelte';
  import Sidebar from './lib/Sidebar.svelte';
  import PageStub from './lib/PageStub.svelte';
  import DashboardPage from './lib/pages/DashboardPage.svelte';
  import ServersPage from './lib/pages/ServersPage.svelte';
  import TerminalPage from './lib/pages/TerminalPage.svelte';
  import {
    backendState,
    connectWs,
    dismissBackendError,
    disconnectWs
  } from './lib/ws.js';

  function connectionLabel(state) {
    if (state === 'online') return 'Подключение к backend активно';
    if (state === 'reconnecting') return 'Переподключение к backend';
    if (state === 'offline') return 'Нет соединения с backend';
    return 'Подключение к backend';
  }

  const navItems = [
    { id: 'dashboard', label: 'Dashboard' },
    { id: 'servers',   label: 'Servers'   },
    { id: 'docker',    label: 'Docker'    },
    { id: 'terminal',  label: 'Terminal'  },
    { id: 'logs',      label: 'Logs'      },
    { id: 'alerts',    label: 'Alerts'    },
    { id: 'files',     label: 'Files'     }
  ];

  let activeTab = 'dashboard';
  let terminalSessionId = '';

  onMount(() => {
    connectWs();
  });

  onDestroy(() => {
    disconnectWs();
  });

  function labelOf(id) {
    const item = navItems.find((i) => i.id === id);
    return item ? item.label : id;
  }

  function handleSelect(id) {
    activeTab = id;
  }

  function handleSshConnected(session) {
    terminalSessionId = session.session_id;
    activeTab = 'terminal';
  }
</script>

<div class="shell">
  <Sidebar items={navItems} active={activeTab} onSelect={handleSelect} />

  <main class="workarea">
    <header class="topbar">
      <nav class="breadcrumbs" aria-label="Хлебные крошки">
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
          <button type="button" aria-label="Закрыть сообщение" on:click={dismissBackendError}>
            ×
          </button>
        </div>
      {/if}
      {#if activeTab === 'dashboard'}
        <DashboardPage />
      {:else if activeTab === 'servers'}
        <ServersPage onConnected={handleSshConnected} />
      {:else if activeTab === 'docker'}
        <PageStub label="Docker" hint="Заглушка: таблица контейнеров с действиями." />
      {:else if activeTab === 'terminal'}
        <TerminalPage initialSessionId={terminalSessionId} />
      {:else if activeTab === 'logs'}
        <PageStub label="Logs" hint="Заглушка: лента логов с фильтрами." />
      {:else if activeTab === 'alerts'}
        <PageStub label="Alerts" hint="Заглушка: список алертов." />
      {:else if activeTab === 'files'}
        <PageStub label="Files" hint="Заглушка: файловый менеджер." />
      {/if}
    </section>
  </main>
</div>

<style>
  .shell {
    display: flex;
    height: 100%;
  }

  .workarea {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    flex-direction: column;
    background: var(--synex-bg-app);
  }

  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex: 0 0 auto;
    height: 60px;
    padding-left: var(--synex-space-6);
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
    padding: var(--synex-space-6);
    display: flex;
    flex-direction: column;
  }
</style>