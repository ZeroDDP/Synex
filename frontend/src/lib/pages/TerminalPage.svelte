<script>
  import { onMount } from 'svelte';
  import { FitAddon } from '@xterm/addon-fit';
  import { Terminal } from '@xterm/xterm';
  import '@xterm/xterm/css/xterm.css';
  import { BACKEND_URL } from '../api/config.js';
  import { onEvent } from '../ws.js';

  export let initialSessionId = '';

  let terminalHost;
  let terminal;
  let fitAddon;
  let terminalSocket = null;
  let reconnectTimer = null;
  let connectionTimeoutTimer = null;
  let reconnectDelay = 1000;
  let mounted = false;
  let sessionClosed = false;
  let terminalHasStarted = false;
  let inputDisposable = null;
  let resizeObserver = null;
  let selectedSessionId = '';
  let sessions = [];
  let activeSession = null;
  let lastSeq = 0;
  let requestCounter = 0;
  let initialSessionApplied = false;
  let lineBuffer = '';
  let sessionsLoading = false;
  let sessionListError = '';
  let terminalError = '';
  let terminalState = 'disconnected';
  const pendingRequests = new Set();
  const CONNECTION_TIMEOUT_MS = 10000;

  function isRecord(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
  }

  function promptPrefix(cwd = activeSession?.cwd) {
    const user = safePromptText(activeSession?.username || 'user');
    const host = safePromptText(activeSession?.host || 'server');
    const path = safePromptText(cwd || '~');
    return `\x1b[38;2;255;123;0m${user}@${host}\x1b[0m:\x1b[38;2;92;109;137m${path}\x1b[0m$ `;
  }

  function safePromptText(value) {
    return String(value).replace(/[\x00-\x1f\x7f]/g, '?');
  }

  function writeTerminal(text) {
    const currentTerminal = terminal;
    if (!currentTerminal) return;
    currentTerminal.write(text, () => {
      requestAnimationFrame(() => {
        if (mounted && terminal === currentTerminal) currentTerminal.scrollToBottom();
      });
    });
  }

  function writeBlock(text, color = '') {
    if (!terminal || text === null || text === undefined || text === '') return;
    const normalized = String(text).replace(/\r\n/g, '\n').replace(/\r/g, '\n').replace(/\n/g, '\r\n');
    const prefix = color ? `\x1b[${color}m` : '';
    const suffix = color ? '\x1b[0m' : '';
    const lineEnding = normalized.endsWith('\r\n') ? '' : '\r\n';
    writeTerminal(`${prefix}${normalized}${suffix}${lineEnding}`);
  }

  function writePrompt() {
    if (!terminal) return;
    writeTerminal(promptPrefix());
  }

  function setTerminalError(message) {
    terminalError = message;
    writeBlock(message, '31');
  }

  function clearConnectionTimers() {
    clearTimeout(connectionTimeoutTimer);
    connectionTimeoutTimer = null;
  }

  function closeTerminalSocket() {
    clearConnectionTimers();
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
    const socket = terminalSocket;
    terminalSocket = null;
    if (socket) socket.close();
  }

  function scheduleReconnect(sessionId) {
    if (!mounted || sessionClosed || selectedSessionId !== sessionId || reconnectTimer) return;
    terminalState = 'reconnecting';
    const delay = reconnectDelay;
    reconnectDelay = Math.min(reconnectDelay * 2, 10000);
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connectTerminal(sessionId);
    }, delay);
  }

  function connectTerminal(sessionId) {
    if (!mounted || !sessionId || selectedSessionId !== sessionId || sessionClosed) return;
    closeTerminalSocket();
    terminalState = 'connecting';
    let timedOut = false;
    const wsBaseUrl = BACKEND_URL.replace(/^http/, 'ws');
    const url = `${wsBaseUrl}/api/ssh/sessions/${encodeURIComponent(sessionId)}/ws?since=${lastSeq}`;
    let socket;

    try {
      socket = new WebSocket(url);
    } catch (error) {
      setTerminalError(`Could not connect to terminal: ${error.message}`);
      scheduleReconnect(sessionId);
      return;
    }

    terminalSocket = socket;
    connectionTimeoutTimer = setTimeout(() => {
      if (terminalSocket !== socket || socket.readyState >= WebSocket.CLOSING) return;
      timedOut = true;
      socket.close();
    }, CONNECTION_TIMEOUT_MS);

    socket.onmessage = (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        setTerminalError('Terminal received invalid JSON; the message was ignored.');
        return;
      }
      handleTerminalMessage(message);
    };

    socket.onclose = (event) => {
      if (terminalSocket !== socket) return;
      clearConnectionTimers();
      terminalSocket = null;

      if (sessionClosed || event.code === 1000 || event.code === 4404) {
        terminalState = sessionClosed || event.code === 1000 ? 'closed' : 'error';
        if (event.code === 4404) {
          terminalError = 'This SSH session no longer exists.';
          void loadSessions();
        }
        return;
      }

      terminalState = 'reconnecting';
      setTerminalError(
        timedOut
          ? `Terminal did not respond within ${CONNECTION_TIMEOUT_MS / 1000} seconds. Reconnecting…`
          : `Terminal connection lost (code ${event.code}). Reconnecting…`
      );
      scheduleReconnect(sessionId);
    };

    socket.onerror = () => {
      socket.close();
    };
  }

  function handleTerminalMessage(message) {
    if (!isRecord(message) || typeof message.type !== 'string' || !Number.isFinite(message.ts)) {
      setTerminalError('Terminal received an invalid message; it was ignored.');
      return;
    }

    if (message.type === 'hello') {
      if (!isRecord(message.data) || !isRecord(message.data.session)) {
        setTerminalError('Terminal received invalid session details.');
        return;
      }
      activeSession = message.data.session;
      clearConnectionTimers();
      reconnectDelay = 1000;
      terminalState = 'connected';
      terminalError = '';
      if (message.data.replay?.gap) {
        writeBlock('Some earlier output is no longer available.', '33');
      }
      if (!terminalHasStarted) {
        writeBlock(`Connected to ${activeSession.host}:${activeSession.port}.`, '32');
        writePrompt();
        terminalHasStarted = true;
        terminal.focus();
      }
      return;
    }

    if (message.type === 'output') {
      handleOutput(message.data);
      return;
    }

    if (message.type === 'closed') {
      sessionClosed = true;
      terminalState = 'closed';
      writeBlock(`SSH session closed (${message.data?.reason || 'unknown reason'}).`, '31');
      void loadSessions();
      return;
    }

    if (message.type === 'error') {
      const detail = typeof message.data?.detail === 'string'
        ? message.data.detail
        : 'The terminal backend returned an error.';
      setTerminalError(detail);
    }
  }

  function handleOutput(entry) {
    if (
      !isRecord(entry) ||
      !Number.isInteger(entry.seq) ||
      entry.seq < 1 ||
      typeof entry.kind !== 'string'
    ) {
      setTerminalError('Terminal received invalid output data; it was ignored.');
      return;
    }
    if (entry.seq <= lastSeq) return;
    if (lastSeq > 0 && entry.seq > lastSeq + 1) {
      writeBlock('Some terminal output was missed.', '33');
    }
    lastSeq = entry.seq;

    if (entry.kind === 'command') {
      if (entry.request_id && pendingRequests.has(entry.request_id)) return;
      lineBuffer = '';
      writeTerminal(`\r\x1b[2K${promptPrefix(entry.cwd)}${entry.command || ''}\r\n`);
      return;
    }

    if (entry.kind === 'result') {
      if (typeof entry.stdout === 'string') writeBlock(entry.stdout);
      if (typeof entry.stderr === 'string') writeBlock(entry.stderr, '31');
      if (entry.timed_out) writeBlock('Command timed out.', '33');
      else if (Number.isInteger(entry.exit_code) && entry.exit_code !== 0) {
        writeBlock(`Exit code: ${entry.exit_code}`, '31');
      }
      if (typeof entry.cwd === 'string' && activeSession) {
        activeSession = { ...activeSession, cwd: entry.cwd };
      }
      if (entry.request_id) pendingRequests.delete(entry.request_id);
      writePrompt();
      return;
    }

    if (entry.kind === 'error') {
      writeBlock(entry.detail || entry.error || 'Command failed.', '31');
      if (entry.request_id) pendingRequests.delete(entry.request_id);
      writePrompt();
    }
  }

  function submitCommand() {
    const command = lineBuffer;
    lineBuffer = '';
    writeTerminal('\r\n');
    if (!command.trim()) {
      writePrompt();
      return;
    }
    if (!terminalSocket || terminalSocket.readyState !== WebSocket.OPEN) {
      setTerminalError('Terminal is not connected.');
      writePrompt();
      return;
    }
    if (command.length > 4096) {
      setTerminalError('Command exceeds the 4096-character limit.');
      writePrompt();
      return;
    }

    const requestId = `ui-${Date.now()}-${++requestCounter}`;
    pendingRequests.add(requestId);
    try {
      terminalSocket.send(JSON.stringify({
        type: 'exec',
        data: { command, id: requestId }
      }));
    } catch (error) {
      pendingRequests.delete(requestId);
      setTerminalError(`Could not send command: ${error.message}`);
      writePrompt();
    }
  }

  function handleInput(data) {
    for (const character of data) {
      if (character === '\r' || character === '\n') {
        submitCommand();
      } else if (character === '\x7f' || character === '\b') {
        if (lineBuffer.length > 0) {
          lineBuffer = lineBuffer.slice(0, -1);
          writeTerminal('\b \b');
        }
      } else if (character >= ' ' && character !== '\x7f') {
        lineBuffer += character;
        writeTerminal(character);
      }
    }
  }

  async function loadSessions() {
    sessionsLoading = true;
    sessionListError = '';
    try {
      const response = await fetch(`${BACKEND_URL}/api/ssh/sessions`);
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = typeof data?.detail === 'string' ? data.detail : `HTTP ${response.status}`;
        throw new Error(detail);
      }
      if (
        !Array.isArray(data) ||
        data.some((session) => !isRecord(session) || typeof session.session_id !== 'string')
      ) {
        throw new Error('Backend returned an invalid SSH session list.');
      }
      sessions = data;
      if (!initialSessionApplied) {
        initialSessionApplied = true;
        if (sessions.some((session) => session.session_id === initialSessionId)) {
          selectSession(initialSessionId);
          return;
        }
      }
      if (selectedSessionId && sessions.some((session) => session.session_id === selectedSessionId)) {
        return;
      }
      const nextSessionId = sessions[0]?.session_id || '';
      if (nextSessionId) {
        selectSession(nextSessionId);
      } else if (selectedSessionId) {
        selectedSessionId = '';
        activeSession = null;
        sessionClosed = true;
        closeTerminalSocket();
        terminal?.reset();
        terminalState = 'disconnected';
      }
    } catch (error) {
      sessionListError = error.message || 'Could not load SSH sessions.';
    } finally {
      sessionsLoading = false;
    }
  }

  function selectSession(sessionId) {
    selectedSessionId = sessionId;
    closeTerminalSocket();
    sessionClosed = false;
    terminalHasStarted = false;
    lastSeq = 0;
    lineBuffer = '';
    pendingRequests.clear();
    activeSession = sessions.find((session) => session.session_id === sessionId) || null;
    terminalError = '';
    terminalState = 'connecting';
    terminal?.reset();
    reconnectDelay = 1000;
    connectTerminal(sessionId);
  }

  function stateLabel(state) {
    if (state === 'connected') return 'Connected';
    if (state === 'connecting') return 'Connecting';
    if (state === 'reconnecting') return 'Reconnecting';
    if (state === 'closed') return 'Session closed';
    if (state === 'error') return 'Unavailable';
    return 'Disconnected';
  }

  onMount(() => {
    mounted = true;
    terminal = new Terminal({
      allowProposedApi: false,
      cursorBlink: true,
      convertEol: true,
      fontFamily: 'JetBrains Mono, Consolas, monospace',
      fontSize: 13,
      scrollback: 3000,
      theme: {
        background: '#12161b',
        foreground: '#d8dee9',
        cursor: '#ff7b00',
        selectionBackground: '#344055',
        black: '#151922',
        red: '#e5534b',
        green: '#12a93f',
        yellow: '#ffcc00',
        blue: '#4f8ff7',
        magenta: '#c678dd',
        cyan: '#56b6c2',
        white: '#d8dee9',
        brightBlack: '#697386',
        brightRed: '#ff6b64',
        brightGreen: '#2ecc71',
        brightYellow: '#ffd633',
        brightBlue: '#6aa6ff',
        brightMagenta: '#d99aff',
        brightCyan: '#67d5e0',
        brightWhite: '#ffffff'
      }
    });
    fitAddon = new FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.open(terminalHost);
    inputDisposable = terminal.onData(handleInput);

    const fitTerminal = () => {
      if (terminalHost.clientWidth > 0 && terminalHost.clientHeight > 0) fitAddon.fit();
    };
    resizeObserver = new ResizeObserver(fitTerminal);
    resizeObserver.observe(terminalHost);
    requestAnimationFrame(fitTerminal);

    const unsubscribe = onEvent('ssh.session', () => {
      void loadSessions();
    });
    void loadSessions();

    return () => {
      mounted = false;
      unsubscribe();
      resizeObserver?.disconnect();
      inputDisposable?.dispose();
      closeTerminalSocket();
      terminal?.dispose();
    };
  });
</script>

<div class="terminal-page">
  {#if sessionListError || terminalError}
    <div class="terminal-error" role="alert">
      <span>{sessionListError || terminalError}</span>
      <button
        type="button"
        aria-label="Dismiss terminal error"
        on:click={() => {
          sessionListError = '';
          terminalError = '';
        }}
      >×</button>
    </div>
  {/if}

  {#if !sessionsLoading && sessions.length === 0}
    <div class="empty-state">
      No active SSH sessions. Open a session first, then select it here.
    </div>
  {/if}

  <section class="terminal-frame" aria-label="SSH terminal">
    <header class="terminal-titlebar">
      <div class="terminal-session-group">
        <label class="sr-only" for="terminal-session">SSH session</label>
        <select
          id="terminal-session"
          class="terminal-session-select"
          value={selectedSessionId}
          on:change={(event) => selectSession(event.currentTarget.value)}
          disabled={sessionsLoading || sessions.length === 0}
        >
          {#if sessions.length === 0}
            <option value="">No active sessions</option>
          {:else}
            {#each sessions as session (session.session_id)}
              <option value={session.session_id}>{session.host}</option>
            {/each}
          {/if}
        </select>
        <span class="terminal-cwd" title={activeSession?.cwd || '/'}>
          {activeSession?.cwd || '/'}
        </span>
      </div>
      <div class="terminal-controls">
        <span class="terminal-state" data-state={terminalState}>{stateLabel(terminalState)}</span>
        <button
          type="button"
          class="refresh-button"
          aria-label="Refresh SSH sessions"
          title="Refresh SSH sessions"
          on:click={loadSessions}
          disabled={sessionsLoading}
        >↻</button>
      </div>
    </header>
    <div class="terminal-host" bind:this={terminalHost}></div>
  </section>
</div>

<style>
  .terminal-page {
    display: flex;
    flex-direction: column;
    gap: var(--synex-space-4);
    flex: 1 1 auto;
    min-height: 0;
    color: var(--synex-text-primary);
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  .terminal-titlebar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--synex-space-3);
    flex: 0 0 auto;
    min-height: 46px;
    padding: 0 var(--synex-space-4);
    border-bottom: 1px solid var(--synex-border-subtle);
    background: var(--synex-bg-surface);
  }

  .terminal-session-group,
  .terminal-controls {
    display: flex;
    align-items: center;
    min-width: 0;
  }

  .terminal-session-group {
    gap: var(--synex-space-4);
  }

  .terminal-session-select {
    min-width: 100px;
    max-width: min(220px, 45vw);
    padding: var(--synex-space-2) var(--synex-space-3);
    border: 1px solid var(--synex-border-strong);
    border-radius: var(--synex-radius-sm);
    background: #12161b;
    color: var(--synex-text-primary);
    font: inherit;
    font-size: var(--font-size-12);
  }

  .terminal-cwd {
    overflow: hidden;
    color: var(--synex-text-muted);
    font-family: var(--font-mono);
    font-size: var(--font-size-11);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .terminal-controls {
    gap: var(--synex-space-3);
    flex: 0 0 auto;
  }

  .refresh-button {
    width: 28px;
    height: 28px;
    border: 1px solid var(--synex-border-default);
    border-radius: var(--synex-radius-sm);
    color: var(--synex-text-secondary);
    font-size: 18px;
    line-height: 1;
  }

  .refresh-button:hover:not(:disabled) {
    background: var(--synex-bg-hover);
  }

  .refresh-button:disabled {
    cursor: wait;
    opacity: 0.6;
  }

  .terminal-state {
    margin-left: auto;
    color: var(--synex-text-muted);
    font-size: var(--font-size-12);
  }

  .terminal-state[data-state='connected'] {
    color: var(--synex-ok);
  }

  .terminal-state[data-state='reconnecting'] {
    color: var(--synex-warning);
  }

  .terminal-state[data-state='error'],
  .terminal-state[data-state='closed'] {
    color: var(--synex-danger-fg);
  }

  .terminal-error {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--synex-space-3);
    padding: var(--synex-space-3) var(--synex-space-4);
    border: 1px solid var(--synex-danger);
    border-radius: var(--synex-radius-sm);
    background: var(--synex-danger-bg);
    color: var(--synex-danger-fg);
  }

  .terminal-error > span {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .terminal-error button {
    flex: 0 0 auto;
    color: inherit;
    font-size: 18px;
    line-height: 1;
  }

  .empty-state {
    padding: var(--synex-space-4);
    border: 1px solid var(--synex-border-subtle);
    border-radius: var(--synex-radius-sm);
    color: var(--synex-text-muted);
  }

  .terminal-frame {
    display: flex;
    flex: 1 1 auto;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
    border: 1px solid var(--synex-border-subtle);
    border-radius: var(--synex-radius-md);
    background: #12161b;
  }

  .terminal-host {
    flex: 1 1 auto;
    min-height: 0;
    padding: var(--synex-space-4);
  }

  @media (max-width: 640px) {
    .terminal-state {
      display: none;
    }

    .terminal-titlebar {
      padding: 0 var(--synex-space-3);
    }
  }
</style>
