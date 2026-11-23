<script>
  import { onMount } from 'svelte';
  import { BACKEND_URL } from '../api/config.js';
  import { selectedServerId } from '../stores.js';
  import { onEvent } from '../ws.js';

  export let onAddSession = () => {};

  let sessions = [];
  let sessionId = '';
  let sessionMenuOpen = false;
  let sessionLoading = true;
  let info = null;
  let listing = null;
  let treeListing = null;
  let currentPath = '';
  let query = '';
  let searchOpen = false;
  let searchResults = null;
  let searchLoading = false;
  let searchNotice = '';
  let selectedPath = '';
  let loading = false;
  let error = '';
  let treeError = '';
  let sessionError = '';
  let sessionRequest = 0;
  let directoryRequest = 0;

  $: visibleEntries = searchResults
    ? searchResults.entries
    : (listing?.entries || []).filter((entry) =>
        !query.trim() || entry.name.toLowerCase().includes(query.trim().toLowerCase())
      );
  $: treeFolders = (treeListing?.entries || []).filter((entry) => entry.is_dir);
  $: pathParts = breadcrumbParts(currentPath);
  $: sessionLabel = selectedSession()
    ? `${selectedSession().username}@${selectedSession().host}:${selectedSession().port}`
    : 'SSH session';

  function isRecord(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
  }

  function errorMessage(requestError) {
    return requestError instanceof Error ? requestError.message : 'Unexpected backend error.';
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

  function isEntry(entry) {
    return (
      isRecord(entry) &&
      typeof entry.name === 'string' &&
      typeof entry.path === 'string' &&
      typeof entry.is_dir === 'boolean' &&
      (entry.size === null || entry.size === undefined || Number.isFinite(entry.size))
    );
  }

  function isListing(value) {
    return (
      isRecord(value) &&
      typeof value.path === 'string' &&
      (value.parent === null || typeof value.parent === 'string') &&
      typeof value.home === 'string' &&
      Array.isArray(value.entries) &&
      value.entries.every(isEntry)
    );
  }

  function selectedSession() {
    return sessions.find((session) => session.session_id === sessionId) || null;
  }

  function listUrl(id, path) {
    const queryParams = new URLSearchParams({ path });
    return `/api/sftp/sessions/${encodeURIComponent(id)}/list?${queryParams}`;
  }

  async function loadSessions() {
    sessionLoading = true;
    sessionError = '';
    try {
      const data = await request('/api/ssh/sessions');
      if (
        !Array.isArray(data) ||
        data.some((session) => !isRecord(session) || typeof session.session_id !== 'string')
      ) {
        throw new Error('Backend returned an invalid SSH session list.');
      }

      sessions = data;
      const currentExists = sessions.some((session) => session.session_id === sessionId);
      const preferred = sessions.find(
        (session) => `${session.host}:${session.port}` === $selectedServerId
      );
      const next = currentExists ? sessionId : preferred?.session_id || sessions[0]?.session_id || '';
      if (next !== sessionId) await selectSession(next);
      else if (!next) {
        info = null;
        listing = null;
        treeListing = null;
        currentPath = '';
      }
    } catch (requestError) {
      sessionError = errorMessage(requestError);
    } finally {
      sessionLoading = false;
    }
  }

  async function selectSession(nextSessionId) {
    sessionId = nextSessionId;
    sessionMenuOpen = false;
    info = null;
    listing = null;
    treeListing = null;
    currentPath = '';
    selectedPath = '';
    searchResults = null;
    searchNotice = '';
    error = '';
    treeError = '';
    const version = ++sessionRequest;
    if (!nextSessionId) return;

    loading = true;
    try {
      const data = await request(`/api/sftp/sessions/${encodeURIComponent(nextSessionId)}/info`);
      if (!isRecord(data) || typeof data.home !== 'string') {
        throw new Error('Backend returned invalid server file information.');
      }
      if (version !== sessionRequest) return;
      info = data;
      const activeSession = sessions.find((session) => session.session_id === nextSessionId);
      await loadDirectory(nextSessionId, activeSession?.cwd || data.home);
    } catch (requestError) {
      if (version === sessionRequest) error = errorMessage(requestError);
    } finally {
      if (version === sessionRequest) loading = false;
    }
  }

  async function loadDirectory(id, path, highlight = '') {
    if (!id || !path) return;
    const version = ++directoryRequest;
    const currentSessionRequest = sessionRequest;
    loading = true;
    error = '';
    treeError = '';
    searchResults = null;
    searchNotice = '';
    selectedPath = highlight;

    try {
      const data = await request(listUrl(id, path));
      if (!isListing(data)) throw new Error('Backend returned an invalid directory listing.');

      let parentData = data;
      if (data.parent) {
        try {
          const parent = await request(listUrl(id, data.parent));
          if (isListing(parent)) parentData = parent;
          else treeError = 'Backend returned an invalid parent directory listing.';
        } catch (parentError) {
          treeError = `Could not load the folder tree: ${errorMessage(parentError)}`;
        }
      }
      if (version !== directoryRequest || currentSessionRequest !== sessionRequest) return;
      listing = data;
      treeListing = parentData;
      currentPath = data.path;
    } catch (requestError) {
      if (version === directoryRequest && currentSessionRequest === sessionRequest) {
        error = errorMessage(requestError);
      }
    } finally {
      if (version === directoryRequest && currentSessionRequest === sessionRequest) loading = false;
    }
  }

  async function runSearch() {
    const name = query.trim();
    if (!sessionId || !currentPath || !name || searchLoading) return;

    searchLoading = true;
    searchNotice = '';
    error = '';
    try {
      const params = new URLSearchParams({ path: currentPath, name });
      const data = await request(
        `/api/sftp/sessions/${encodeURIComponent(sessionId)}/search?${params}`
      );
      if (
        !isRecord(data) ||
        !Array.isArray(data.entries) ||
        data.entries.some(
          (entry) => !isRecord(entry) || typeof entry.name !== 'string' || typeof entry.path !== 'string'
        ) ||
        typeof data.truncated !== 'boolean' ||
        typeof data.timed_out !== 'boolean'
      ) {
        throw new Error('Backend returned invalid file search results.');
      }
      searchResults = data;
      searchNotice = data.truncated || data.timed_out
        ? 'Not all results are shown. Narrow the folder or name and search again.'
        : data.denied > 0
          ? `${data.denied} folders could not be searched because access was denied.`
          : '';
    } catch (requestError) {
      error = `Could not search files: ${errorMessage(requestError)}`;
    } finally {
      searchLoading = false;
    }
  }

  async function openSearchResult(entry) {
    const parent = entry.parent || searchResults?.path;
    if (!parent) {
      error = 'This search result has no parent directory.';
      return;
    }
    query = '';
    await loadDirectory(sessionId, parent, entry.path);
  }

  function handleQueryInput() {
    searchResults = null;
    searchNotice = '';
    selectedPath = '';
  }

  function toggleSearch() {
    searchOpen = !searchOpen;
    if (!searchOpen) {
      query = '';
      searchResults = null;
      searchNotice = '';
      selectedPath = '';
    }
  }

  function breadcrumbParts(path) {
    if (!path) return [];
    const normalized = path.replace(/\\/g, '/');
    const parts = normalized.split('/').filter(Boolean);
    const result = [{ name: '/', path: '/' }];
    let current = '';
    for (const part of parts) {
      current += `/${part}`;
      result.push({ name: part, path: current });
    }
    return result;
  }

  function folderName(path) {
    if (!path || path === '/') return '/';
    return path.replace(/\\/g, '/').replace(/\/+$/, '').split('/').pop() || path;
  }

  function formatSize(size) {
    if (!Number.isFinite(size) || size < 0) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = size;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${Number(value.toFixed(value >= 10 ? 0 : 1))}${units[unit]}`;
  }

  function openEntry(entry) {
    if (entry.is_dir) {
      void loadDirectory(sessionId, entry.path);
    } else {
      selectedPath = entry.path;
    }
  }

  onMount(() => {
    const unsubscribe = onEvent('ssh.session', () => void loadSessions());
    void loadSessions();
    return unsubscribe;
  });
</script>

<section class="files-page" aria-label="Files">
  {#if sessionLoading && sessions.length === 0}
    <div class="files-message">Loading SSH sessions…</div>
  {:else if sessions.length === 0}
    <div class="files-empty">
      {#if sessionError}
        <p role="alert">{sessionError}</p>
      {:else}
        <p>No active SSH sessions. Connect to a server to browse files.</p>
      {/if}
      <button type="button" class="files-add-server" on:click={onAddSession}>Add new server</button>
    </div>
  {:else}
    <div class="files-panel">
      <header class="files-head">
        <nav class="breadcrumbs" aria-label="Current folder">
          {#each pathParts as part, index (part.path)}
            {#if index > 0}<span class="breadcrumb-separator">&gt;</span>{/if}
            <button
              type="button"
              class:breadcrumb-current={index === pathParts.length - 1}
              on:click={() => void loadDirectory(sessionId, part.path)}
            >{part.name}</button>
          {/each}
        </nav>

        <div class="files-controls">
          <button type="button" class="files-plus" aria-label="Unavailable" disabled>+</button>
          <div class="session-picker">
            <button
              type="button"
              class="files-session-toggle"
              aria-label={`Select SSH session. Current session: ${sessionLabel}`}
              aria-expanded={sessionMenuOpen}
              on:click={() => (sessionMenuOpen = !sessionMenuOpen)}
            >V</button>
            {#if sessionMenuOpen}
              <div class="session-menu" role="listbox" aria-label="SSH sessions">
                {#each sessions as session (session.session_id)}
                  <button
                    type="button"
                    role="option"
                    aria-selected={session.session_id === sessionId}
                    on:click={() => void selectSession(session.session_id)}
                  >{session.username}@{session.host}:{session.port}</button>
                {/each}
              </div>
            {/if}
          </div>
          <button
            type="button"
            class="files-search-toggle"
            aria-label={searchOpen ? 'Close file search' : 'Search files'}
            aria-expanded={searchOpen}
            on:click={toggleSearch}
          ><img src="/icons/search.svg" alt="" /></button>
          {#if searchOpen}
            <form class="files-search-form" on:submit|preventDefault={runSearch}>
              <input
                type="search"
                aria-label="Filter this folder or search subfolders"
                placeholder="Search"
                bind:value={query}
                on:input={handleQueryInput}
              />
              <button type="submit" disabled={!query.trim() || searchLoading}>
                {searchLoading ? 'Searching…' : 'Search in subfolders'}
              </button>
            </form>
          {/if}
        </div>
      </header>

      {#if error}
        <div class="files-error" role="alert">{error}</div>
      {/if}
      {#if treeError}
        <div class="files-error files-error--subtle" role="status">{treeError}</div>
      {/if}
      {#if searchNotice}
        <div class="files-search-notice" role="status">{searchNotice}</div>
      {/if}

      <div class="files-browser">
        <aside class="files-tree" aria-label="Folders">
          {#if treeListing}
            <button
              type="button"
              class="folder-root"
              aria-current={treeListing.path === currentPath ? 'location' : undefined}
              on:click={() => void loadDirectory(sessionId, treeListing.path)}
            >
              <img src="/icons/_Dashboard%20icons/files.svg" alt="" />
              <span>{folderName(treeListing.path)}</span>
            </button>
            <div class="folder-children">
              {#each treeFolders as folder (folder.path)}
                <button
                  type="button"
                  class="folder-row"
                  class:folder-row--active={folder.path === currentPath}
                  aria-current={folder.path === currentPath ? 'location' : undefined}
                  on:click={() => void loadDirectory(sessionId, folder.path)}
                >
                  <img src="/icons/_Dashboard%20icons/files.svg" alt="" />
                  <span>{folder.name}</span>
                </button>
              {/each}
            </div>
          {/if}
        </aside>

        <div class="files-table" aria-busy={loading || searchLoading}>
          <div class="files-table-head">
            <span>NAME</span>
            <span>SIZE</span>
          </div>
          {#if loading && !listing}
            <p class="files-message">Loading folder…</p>
          {:else if visibleEntries.length === 0}
            <p class="files-message">
              {searchLoading ? 'Searching…' : query.trim() ? 'No matching files.' : 'This folder is empty.'}
            </p>
          {:else}
            {#each visibleEntries as entry (entry.path)}
              <button
                type="button"
                class="file-row"
                class:file-row--selected={selectedPath === entry.path}
                on:click={() => searchResults ? void openSearchResult(entry) : openEntry(entry)}
              >
                <span class="file-name">{entry.name}</span>
                <span class="file-size">{formatSize(entry.size)}</span>
              </button>
            {/each}
          {/if}
        </div>
      </div>
    </div>
  {/if}
</section>

<style>
  .files-page {
    display: flex;
    flex: 1 1 auto;
    min-height: 0;
    font-family: var(--font-host);
  }

  .files-panel {
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

  .files-head {
    position: relative;
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    justify-content: space-between;
    gap: var(--synex-space-4);
    min-height: 89px;
    padding: 18px 26px 18px 34px;
    border-bottom: 1px solid var(--synex-border-default);
  }

  .breadcrumbs {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 12px;
    color: var(--synex-text-secondary);
    font-size: 22px;
    white-space: nowrap;
  }

  .breadcrumbs button {
    overflow: hidden;
    color: inherit;
    text-overflow: ellipsis;
  }

  .breadcrumbs button:hover {
    color: var(--synex-accent);
  }

  .breadcrumbs .breadcrumb-current {
    color: var(--synex-text-secondary);
  }

  .breadcrumb-separator {
    color: var(--synex-text-muted);
  }

  .files-controls {
    position: relative;
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    gap: var(--synex-space-2);
  }

  .files-plus,
  .files-session-toggle,
  .files-search-toggle {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 42px;
    height: 40px;
    border: 1px solid var(--synex-border-default);
    border-radius: 8px;
    color: var(--synex-text-muted);
    font-size: 23px;
    line-height: 1;
  }

  .files-plus {
    border-color: var(--synex-accent);
    color: var(--synex-accent);
  }

  .files-plus:disabled {
    cursor: default;
    opacity: 1;
  }

  .files-session-toggle:hover {
    border-color: var(--synex-accent);
    color: var(--synex-accent);
  }

  .files-search-toggle {
    border-color: var(--synex-accent);
  }

  .files-search-toggle img {
    display: block;
    width: 25px;
    height: 25px;
  }

  .session-picker {
    position: relative;
  }

  .session-menu {
    position: absolute;
    z-index: 4;
    top: calc(100% + 6px);
    right: 0;
    display: flex;
    min-width: 230px;
    max-height: 260px;
    flex-direction: column;
    overflow: auto;
    border: 1px solid var(--synex-border-default);
    border-radius: 8px;
    background: var(--synex-bg-surface);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
  }

  .session-menu button {
    padding: 10px 12px;
    color: var(--synex-text-secondary);
    text-align: left;
    white-space: nowrap;
  }

  .session-menu button:hover,
  .session-menu button[aria-selected='true'] {
    background: var(--synex-bg-active);
    color: var(--synex-accent);
  }

  .files-search-form {
    position: absolute;
    z-index: 3;
    top: calc(100% + 8px);
    right: 0;
    display: flex;
    gap: var(--synex-space-2);
    padding: 8px;
    border: 1px solid var(--synex-border-default);
    border-radius: 8px;
    background: var(--synex-bg-surface);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
  }

  .files-search-form input {
    width: 200px;
    min-width: 0;
    padding: 8px 10px;
    border: 1px solid var(--synex-border-default);
    border-radius: 6px;
    outline: none;
    background: var(--synex-bg-app);
    color: var(--synex-text-primary);
  }

  .files-search-form input:focus {
    border-color: var(--synex-accent);
  }

  .files-search-form button {
    padding: 8px 10px;
    border: 1px solid var(--synex-accent);
    border-radius: 6px;
    color: var(--synex-accent);
    font-size: 12px;
    white-space: nowrap;
  }

  .files-search-form button:disabled {
    cursor: default;
    opacity: 0.5;
  }

  .files-error,
  .files-search-notice {
    flex: 0 0 auto;
    margin: var(--synex-space-3) var(--synex-space-4) 0;
    padding: var(--synex-space-2) var(--synex-space-3);
    border: 1px solid var(--synex-danger);
    border-radius: var(--synex-radius-sm);
    background: var(--synex-danger-bg);
    color: var(--synex-danger-fg);
    font-size: 12px;
  }

  .files-error--subtle,
  .files-search-notice {
    border-color: var(--synex-warning);
    background: var(--synex-warning-bg);
    color: var(--synex-text-secondary);
  }

  .files-browser {
    display: grid;
    flex: 1 1 auto;
    grid-template-columns: minmax(240px, 330px) minmax(0, 1fr);
    min-height: 0;
  }

  .files-tree {
    min-width: 0;
    overflow: auto;
    padding: 21px 14px;
    border-right: 1px solid var(--synex-border-default);
  }

  .folder-root,
  .folder-row {
    display: flex;
    align-items: center;
    gap: 24px;
    width: 100%;
    min-height: 45px;
    padding: 6px 12px;
    border-radius: 7px;
    color: var(--synex-text-primary);
    text-align: left;
  }

  .folder-root {
    padding-left: 25px;
  }

  .folder-row {
    width: min(220px, 100%);
    margin: 2px 0 2px 31px;
    padding-left: 22px;
  }

  .folder-row--active,
  .folder-row:hover,
  .folder-root:hover {
    background: rgba(115, 68, 31, 0.65);
  }

  .folder-root img,
  .folder-row img {
    flex: 0 0 auto;
    width: 32px;
    height: 24px;
    object-fit: contain;
  }

  .folder-root span,
  .folder-row span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .files-table {
    min-width: 0;
    overflow: auto;
  }

  .files-table-head,
  .file-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 170px;
    align-items: center;
    gap: var(--synex-space-4);
    padding: 0 44px;
  }

  .files-table-head {
    min-height: 55px;
    border-bottom: 1px solid var(--synex-border-default);
    color: var(--synex-text-muted);
    font-size: 16px;
  }

  .file-row {
    width: 100%;
    min-height: 39px;
    color: var(--synex-text-primary);
    text-align: left;
    font-size: 16px;
  }

  .file-row:hover,
  .file-row--selected {
    background: var(--synex-bg-hover);
  }

  .file-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .file-size {
    text-align: left;
    white-space: nowrap;
  }

  .files-message {
    margin: var(--synex-space-5);
    color: var(--synex-text-muted);
    text-align: center;
  }

  .files-empty {
    display: flex;
    flex: 1 1 auto;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--synex-space-3);
    color: var(--synex-text-muted);
    text-align: center;
  }

  .files-empty p {
    margin: 0;
  }

  .files-add-server {
    padding: 9px 18px;
    border: 1px solid var(--synex-accent);
    border-radius: var(--synex-radius-sm);
    background: var(--synex-accent);
    color: var(--synex-accent-on);
    font-weight: 600;
  }

  @media (max-width: 800px) {
    .files-head {
      align-items: flex-start;
      flex-direction: column;
    }

    .files-controls {
      max-width: 100%;
    }

    .files-search-form {
      right: auto;
      left: 0;
    }

    .files-browser {
      grid-template-columns: minmax(120px, 35%) minmax(0, 1fr);
    }

    .folder-root,
    .folder-row {
      gap: var(--synex-space-2);
      padding-left: 8px;
    }

    .folder-row {
      margin-left: 8px;
    }

    .files-table-head,
    .file-row {
      grid-template-columns: minmax(0, 1fr) 75px;
      padding: 0 var(--synex-space-3);
    }
  }
</style>
