<script>
  import Sidebar from './lib/Sidebar.svelte';
  import PageStub from './lib/PageStub.svelte';
  import DashboardPage from './lib/pages/DashboardPage.svelte';
  import ServersPage from './lib/pages/ServersPage.svelte';


  // import dashboardIcon from './lib/icons/dashboard.svg?raw';
  // import serversIcon from './lib/icons/servers.svg?raw';
  // import dockerIcon from './lib/icons/docker.svg?raw';
  // import terminalIcon from './lib/icons/terminal.svg?raw';
  // import logsIcon from './lib/icons/logs.svg?raw';
  // import alertsIcon from './lib/icons/alerts.svg?raw';
  // import filesIcon from './lib/icons/files.svg?raw';


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

  function labelOf(id) {
    const item = navItems.find((i) => i.id === id);
    return item ? item.label : id;
  }

  function handleSelect(id) {
    activeTab = id;
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
    </header>

    <section class="content">
      {#if activeTab === 'dashboard'}
        <DashboardPage />
      {:else if activeTab === 'servers'}
        <ServersPage />
      {:else if activeTab === 'docker'}
        <PageStub label="Docker" hint="Заглушка: таблица контейнеров с действиями." />
      {:else if activeTab === 'terminal'}
        <PageStub label="Terminal" hint="Заглушка: терминал с вкладками сессий." />
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
    flex: 0 0 auto;
    height: 60px;
    padding-left: var(--synex-space-6);
    border-bottom: 1px solid var(--synex-border-subtle);
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
  }
</style>