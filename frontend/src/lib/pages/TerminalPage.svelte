<script>
  import { onMount } from 'svelte';
  import { FitAddon } from '@xterm/addon-fit';
  import { SearchAddon } from '@xterm/addon-search';
  import { Terminal } from '@xterm/xterm';
  import { WebLinksAddon } from '@xterm/addon-web-links';
  import { Unicode11Addon } from '@xterm/addon-unicode11';
  import stringWidth from 'string-width';
  import '@xterm/xterm/css/xterm.css';
  import { BACKEND_URL } from '../api/config.js';
  import { setServerUsernames, sshSessionReplacement } from '../stores.js';
  import { onEvent } from '../ws.js';
  import {
    sshConnectionName,
    sshServerKey,
    sshTargetKey,
    uniqueSshServers
  } from '../sshIdentity.js';

  export let initialSessionId = '';
  export let hidden = false;
  export let onNewSession = () => {};

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
  let lastInitialSessionId = '';
  let lineBuffer = '';
  let cursor = 0;
  let history = [];
  let historyIndex = -1;
  let draft = '';
  let interactivePrompt = null;
  let searchAddon;
  let completionPending = false;
  let lastCompletion = null;
  let connectionNames = [];
  let connectionsLoading = true;
  let connectingConnectionId = '';
  let creatingSessionProfileId = '';
  let sessionsLoading = false;
  let sessionListRequest = 0;
  let terminalTabs = [];
  let activeTerminalTabId = '';
  let terminalTabCounter = 0;
  let knownSessionIds = new Set();
  let sessionsMenuOpen = false;
  let sessionsMenuElement;
  let menuNotice = '';
  let sessionListError = '';
  let terminalError = '';
  let terminalState = 'disconnected';
  const pendingRequests = new Map();
  let pendingRequestCount = 0;
  const CONNECTION_TIMEOUT_MS = 10000;
  $: visibleTerminalTabs = terminalTabs
    .map((tab) => ({ ...tab, session: sessions.find((session) => session.session_id === tab.sessionId) }))
    .filter((tab) => tab.session);
  $: visibleConnections = uniqueSshServers(connectionNames);
  $: if (
    mounted &&
    initialSessionId &&
    initialSessionId !== lastInitialSessionId &&
    sessions.some((session) => session.session_id === initialSessionId)
  ) {
    lastInitialSessionId = initialSessionId;
    initialSessionApplied = true;
    activateSessionTab(initialSessionId);
    void loadConnectionNames();
  }

  function syncPendingRequestCount() {
    pendingRequestCount = pendingRequests.size;
  }

  $: if (mounted && !hidden) {
    requestAnimationFrame(() => {
      if (mounted && !hidden && terminalHost && terminalHost.clientWidth > 0) fitAddon?.fit();
    });
  }

  function isRecord(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
  }

  function promptPrefix(cwd = activeSession?.cwd) {
    const user = safePromptText(activeSession?.username || 'user');
    const host = safePromptText(activeSession?.host || 'server');
    const path = safePromptText(cwd || '~');
    return `\x1b[38;2;255;123;0m${user}@${host}\x1b[0m:\x1b[38;2;92;109;137m${path}\x1b[0m$ `;
  }

  function connectionName(session) {
    if (!session) return '';
    const saved = session.connection_id
      ? connectionNames.find((connection) => connection.id === session.connection_id)
      : connectionNames.find((connection) =>
          sshTargetKey(connection) === sshTargetKey(session)
        );
    return sessionNames[session.session_id] || saved?.name || session.name || '';
  }

  function tabLabel(session) {
    const name = connectionName(session);
    const generatedName = `${session.username}@${session.host}${
      Number(session.port || 22) === 22 ? '' : `:${session.port}`
    }`;
    if (name && name.toLowerCase() === generatedName.toLowerCase()) return name;
    return `${name || session.username || 'server'}@${session.host}`;
  }

  function safePromptText(value) {
    return String(value).replace(/[\x00-\x1f\x7f]/g, '?');
  }

  function writeTerminal(text) {
    const currentTerminal = terminal;
    if (!currentTerminal) return;
    const shouldScroll = currentTerminal.buffer.active.viewportY === currentTerminal.buffer.active.baseY;
    currentTerminal.write(text, () => {
      requestAnimationFrame(() => {
        if (shouldScroll && mounted && terminal === currentTerminal) currentTerminal.scrollToBottom();
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
    redrawLine();
  }

  function setTerminalError(message) {
    terminalError = message;
  }

  function lineCharacters(value = lineBuffer) {
    return Array.from(value);
  }

  function redrawLine() {
    if (!terminal) return;
    writeTerminal(`\r\x1b[2K${promptPrefix()}${lineBuffer}`);
    const back = stringWidth(lineCharacters().slice(cursor).join(''));
    if (back > 0) writeTerminal(`\x1b[${back}D`);
  }

  function setLine(value, nextCursor = lineCharacters(value).length) {
    lineBuffer = value;
    cursor = Math.max(0, Math.min(nextCursor, lineCharacters(value).length));
    redrawLine();
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
      const requestId = typeof message.data?.request_id === 'string' ? message.data.request_id : '';
      if (message.data?.error === 'ssh_interactive_command' && requestId) {
        interactivePrompt = {
          command: pendingRequests.get(requestId) || '',
          program: typeof message.data.program === 'string' ? message.data.program : '',
          hint: typeof message.data.hint === 'string' ? message.data.hint : detail,
          requestId
        };
        pendingRequests.delete(requestId);
        syncPendingRequestCount();
        writePrompt();
      } else if (requestId && pendingRequests.has(requestId)) {
        pendingRequests.delete(requestId);
        syncPendingRequestCount();
        writeBlock(detail, '31');
        writePrompt();
      } else {
        setTerminalError(detail);
      }
      return;
    }

    if (message.type === 'interrupt') {
      if (message.data?.status === 'idle') {
        writeBlock('No command is running.', '33');
      }
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
      cursor = 0;
      writeTerminal(`\r\x1b[2K${promptPrefix(entry.cwd)}${entry.command || ''}\r\n`);
      return;
    }

    if (entry.kind === 'result') {
      if (entry.interrupted) writeBlock('^C', '33');
      if (typeof entry.stdout === 'string') writeBlock(entry.stdout);
      if (typeof entry.stderr === 'string') writeBlock(entry.stderr, '31');
      if (entry.timed_out) writeBlock('Command timed out.', '33');
      else if (Number.isInteger(entry.exit_code) && entry.exit_code !== 0) {
        writeBlock(`Exit code: ${entry.exit_code}`, '31');
      }
      if (Number.isFinite(entry.duration_ms) && entry.duration_ms >= 2000) {
        writeBlock(`Duration: ${(entry.duration_ms / 1000).toFixed(1)} s`, '90');
      }
      if (typeof entry.cwd === 'string' && activeSession) {
        activeSession = { ...activeSession, cwd: entry.cwd };
      }
      if (entry.request_id) pendingRequests.delete(entry.request_id);
      syncPendingRequestCount();
      writePrompt();
      return;
    }

    if (entry.kind === 'error') {
      writeBlock(entry.detail || entry.error || 'Command failed.', '31');
      if (entry.request_id) pendingRequests.delete(entry.request_id);
      syncPendingRequestCount();
      writePrompt();
    }
  }

  function submitCommand(command = lineBuffer, allowInteractive = false) {
    redrawLine();
    const characters = lineCharacters();
    const trailingWidth = stringWidth(characters.slice(cursor).join(''));
    if (trailingWidth > 0) writeTerminal(`\x1b[${trailingWidth}C`);
    writeTerminal('\r\n');
    lineBuffer = '';
    cursor = 0;
    historyIndex = -1;
    draft = '';
    interactivePrompt = null;
    if (!command.trim()) {
      writePrompt();
      return;
    }
    if (command.trim() === 'clear') {
      if (!command.startsWith(' ') && history[history.length - 1] !== command) {
        history = [...history, command].slice(-500);
      }
      terminal?.clear();
      writePrompt();
      return;
    }
    if (!terminalSocket || terminalSocket.readyState !== WebSocket.OPEN) {
      setTerminalError('Terminal is not connected.');
      writePrompt();
      return;
    }
    if (lineCharacters(command).length > 4096) {
      setTerminalError('Command exceeds the 4096-character limit.');
      writePrompt();
      return;
    }

    const requestId = `ui-${Date.now()}-${++requestCounter}`;
    pendingRequests.set(requestId, command);
    syncPendingRequestCount();
    if (!command.startsWith(' ') && history[history.length - 1] !== command) {
      history = [...history, command].slice(-500);
    }
    try {
      terminalSocket.send(JSON.stringify({
        type: 'exec',
        data: { command, id: requestId, ...(allowInteractive ? { allow_interactive: true } : {}) }
      }));
    } catch (error) {
      pendingRequests.delete(requestId);
      syncPendingRequestCount();
      setTerminalError(`Could not send command: ${error.message}`);
      writePrompt();
    }
  }

  function handleInterrupt() {
    if (!pendingRequests.size) {
      lineBuffer = '';
      cursor = 0;
      historyIndex = -1;
      draft = '';
      writeTerminal('^C\r\n');
      writePrompt();
      return;
    }
    if (!terminalSocket || terminalSocket.readyState !== WebSocket.OPEN) {
      setTerminalError('Cannot interrupt the command because the terminal is disconnected.');
      return;
    }
    try {
      terminalSocket.send(JSON.stringify({ type: 'interrupt' }));
    } catch (error) {
      setTerminalError(`Could not interrupt command: ${error.message}`);
    }
  }

  function moveCursor(delta) {
    cursor = Math.max(0, Math.min(lineCharacters().length, cursor + delta));
    redrawLine();
  }

  function browseHistory(direction) {
    if (history.length === 0) return;
    if (historyIndex === -1) {
      if (direction > 0) return;
      draft = lineBuffer;
      historyIndex = history.length;
    }
    const nextIndex = Math.max(-1, Math.min(history.length - 1, historyIndex + direction));
    if (nextIndex === -1) {
      historyIndex = -1;
      setLine(draft);
      return;
    }
    historyIndex = nextIndex;
    setLine(history[historyIndex]);
  }

  async function completeLine() {
    if (!selectedSessionId || pendingRequests.size || !terminalSocket || completionPending) return;
    const currentLine = lineBuffer;
    const currentCursor = cursor;
    if (
      lastCompletion &&
      lastCompletion.line === currentLine &&
      lastCompletion.cursor === currentCursor
    ) {
      writeTerminal('\r\n');
      writeBlock(lastCompletion.candidates.join('  '));
      lastCompletion = null;
      writePrompt();
      return;
    }

    completionPending = true;
    try {
      const query = new URLSearchParams({ line: currentLine, cursor: String(currentCursor) });
      const response = await fetch(
        `${BACKEND_URL}/api/ssh/sessions/${encodeURIComponent(selectedSessionId)}/complete?${query}`
      );
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(typeof data?.detail === 'string' ? data.detail : `HTTP ${response.status}`);
      }
      if (
        !Number.isInteger(data?.start) ||
        data.start < 0 ||
        data.start > currentCursor ||
        !Array.isArray(data.candidates) ||
        !Array.isArray(data.insert) ||
        typeof data.common !== 'string' ||
        data.insert.length !== data.candidates.length ||
        data.insert.some((value) => typeof value !== 'string') ||
        data.candidates.some((value) => typeof value !== 'string')
      ) {
        throw new Error('Backend returned invalid completion data.');
      }
      if (lineBuffer !== currentLine || cursor !== currentCursor) return;
      if (data.candidates.length === 0) return;

      const characters = lineCharacters();
      const replacement = data.candidates.length === 1 ? data.insert[0] : data.common;
      const next = characters.slice(0, data.start).join('') +
        replacement +
        characters.slice(currentCursor).join('');
      setLine(next, data.start + lineCharacters(replacement).length);
      lastCompletion = data.candidates.length > 1
        ? { line: lineBuffer, cursor, candidates: data.candidates }
        : null;
    } catch (error) {
      setTerminalError(`Could not complete command: ${errorMessage(error)}`);
    } finally {
      completionPending = false;
    }
  }

  function insertCharacter(character) {
    const characters = lineCharacters();
    characters.splice(cursor, 0, character);
    setLine(characters.join(''), cursor + 1);
    historyIndex = -1;
    lastCompletion = null;
  }

  function handleInput(data) {
    if (data.includes('\x03')) {
      handleInterrupt();
      return;
    }
    if (pendingRequests.size) return;
    if (data.startsWith('\x1b')) {
      const keyActions = {
        '\x1b[D': () => moveCursor(-1),
        '\x1b[C': () => moveCursor(1),
        '\x1b[A': () => browseHistory(-1),
        '\x1b[B': () => browseHistory(1),
        '\x1b[H': () => moveCursor(-cursor),
        '\x1bOH': () => moveCursor(-cursor),
        '\x1b[F': () => moveCursor(lineCharacters().length - cursor),
        '\x1bOF': () => moveCursor(lineCharacters().length - cursor),
        '\x1b[3~': () => {
          const characters = lineCharacters();
          characters.splice(cursor, 1);
          setLine(characters.join(''), cursor);
        }
      };
      keyActions[data]?.();
      return;
    }

    if (data.includes('\r') || data.includes('\n')) {
      const pasted = data.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
      const lineCount = pasted.split('\n').length;
      if (lineCount > 1) {
        const characters = lineCharacters();
        const combined = characters.slice(0, cursor).join('') +
          pasted +
          characters.slice(cursor).join('');
        lineBuffer = combined;
        cursor = lineCharacters(combined).length;
        submitCommand(combined);
        return;
      }
    }

    for (const character of data) {
      if (character === '\r' || character === '\n') {
        submitCommand();
      } else if (character === '\x7f' || character === '\b') {
        if (cursor > 0) {
          const characters = lineCharacters();
          characters.splice(cursor - 1, 1);
          setLine(characters.join(''), cursor - 1);
        }
      } else if (character === '\t') {
        void completeLine();
      } else if (character === '\x01') {
        moveCursor(-cursor);
      } else if (character === '\x05') {
        moveCursor(lineCharacters().length - cursor);
      } else if (character === '\x15') {
        const remaining = lineCharacters().slice(cursor).join('');
        setLine(remaining, 0);
      } else if (character === '\x17') {
        const characters = lineCharacters();
        let start = cursor;
        while (start > 0 && /\s/u.test(characters[start - 1])) start -= 1;
        while (start > 0 && !/\s/u.test(characters[start - 1])) start -= 1;
        characters.splice(start, cursor - start);
        setLine(characters.join(''), start);
      } else if (character === '\x0c') {
        terminal.clear();
        redrawLine();
      } else if (character >= ' ' && character !== '\x7f') {
        insertCharacter(character);
      }
    }
  }

  async function loadHistory(sessionId) {
    try {
      const response = await fetch(
        `${BACKEND_URL}/api/ssh/sessions/${encodeURIComponent(sessionId)}/history?limit=500`
      );
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(typeof data?.detail === 'string' ? data.detail : `HTTP ${response.status}`);
      }
      if (!Array.isArray(data?.commands) || data.commands.some((command) => typeof command !== 'string')) {
        throw new Error('Backend returned invalid command history.');
      }
      if (selectedSessionId === sessionId) history = data.commands;
    } catch (error) {
      if (selectedSessionId === sessionId) {
        setTerminalError(`Could not load command history: ${errorMessage(error)}`);
      }
    }
  }

  async function loadConnectionNames() {
    connectionsLoading = true;
    try {
      const response = await fetch(`${BACKEND_URL}/api/connections`);
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(typeof data?.detail === 'string' ? data.detail : `HTTP ${response.status}`);
      }
      if (
        !Array.isArray(data) ||
        data.some((connection) =>
          typeof connection?.id !== 'string' ||
          typeof connection?.name !== 'string' ||
          typeof connection?.host !== 'string' ||
          typeof connection?.username !== 'string'
        )
      ) {
        throw new Error('Backend returned invalid saved connections.');
      }
      connectionNames = data;
      if (activeSession && terminalHasStarted && !pendingRequests.size) redrawLine();
    } catch (error) {
      setTerminalError(`Could not load saved connection names: ${errorMessage(error)}`);
    } finally {
      connectionsLoading = false;
    }
  }

  function sessionForConnection(connection) {
    return sessions.find((session) =>
      session.connection_id
        ? session.connection_id === connection.id
        : sshTargetKey(connection) === sshTargetKey(session)
    );
  }

  $: unlistedSessions = uniqueSshServers(sessions.filter(
    (session) =>
      !connectionNames.some((connection) =>
        (session.connection_id
          ? connection.id === session.connection_id
          : sshTargetKey(connection) === sshTargetKey(session)) ||
        sshServerKey(connection) === sshServerKey(session)
      )
  ));

  async function requestSavedConnection(connection, credentials, retried = false) {
    const response = await fetch(
      `${BACKEND_URL}/api/connections/${encodeURIComponent(connection.id)}/connect`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(credentials)
      }
    );
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const errorType = data?.error;
      if (
        !retried &&
        response.status === 409 &&
        (errorType === 'ssh_host_key_unknown' || errorType === 'ssh_host_key_mismatch')
      ) {
        await acceptSavedHostKey(data, connection, errorType === 'ssh_host_key_mismatch');
        return requestSavedConnection(connection, credentials, true);
      }
      throw new Error(typeof data?.detail === 'string' ? data.detail : `Backend returned HTTP ${response.status}.`);
    }
    if (
      typeof data?.session_id !== 'string' ||
      typeof data.host !== 'string' ||
      typeof data.username !== 'string' ||
      !Number.isInteger(data.port)
    ) {
      throw new Error('Backend returned invalid SSH session details.');
    }
    return data;
  }

  async function acceptSavedHostKey(data, connection, replace) {
    const key = data?.host_key;
    const expectedHost = connection.host;
    const expectedPort = Number(connection.port || 22);
    const fingerprintPattern = /^SHA256:[A-Za-z0-9+/]{43}$/;
    if (
      !key ||
      typeof key.host !== 'string' ||
      key.host.toLowerCase() !== expectedHost.toLowerCase() ||
      key.port !== expectedPort ||
      typeof key.fingerprint !== 'string' ||
      !fingerprintPattern.test(key.fingerprint) ||
      (replace &&
        (typeof key.expected_fingerprint !== 'string' ||
          !fingerprintPattern.test(key.expected_fingerprint)))
    ) {
      throw new Error('Backend returned invalid SSH host key details.');
    }
    const warning = replace
      ? `Host key changed for ${connection.name}.\nExpected: ${key.expected_fingerprint}\nReceived: ${key.fingerprint}\nReplace the saved host key? Verify the new fingerprint first.`
      : `Trust the SSH host key for ${connection.name}?\n${key.fingerprint}`;
    if (!window.confirm(warning)) throw new Error('SSH host key was not accepted; no connection was made.');

    const response = await fetch(`${BACKEND_URL}/api/ssh/known-hosts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        host: key.host,
        port: key.port,
        fingerprint: key.fingerprint,
        key_type: typeof key.key_type === 'string' ? key.key_type : null,
        ...(replace ? { replace: true } : {})
      })
    });
    const result = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(
        typeof result?.detail === 'string'
          ? result.detail
          : `Could not save SSH host key (HTTP ${response.status}).`
      );
    }
  }

  async function connectSavedConnection(connection) {
    if (connectingConnectionId) return;
    if (connection.auth === 'password' && !connection.has_password) {
      setTerminalError(
        `No password is saved for ${connection.name}. Save it in the server profile to open terminal tabs without a prompt.`
      );
      return;
    }

    connectingConnectionId = connection.id;
    sessionsMenuOpen = false;
    terminalError = '';
    try {
      const session = await requestSavedConnection(connection, {});
      sessionNames = { ...sessionNames, [session.session_id]: connection.name };
      sessions = [...sessions.filter((item) => item.session_id !== session.session_id), session];
      setServerUsernames(sessions);
      knownSessionIds = new Set([...knownSessionIds, session.session_id]);
      activateSessionTab(session.session_id);
      void loadSessions();
    } catch (error) {
      setTerminalError(`Could not connect to ${connection.name}: ${errorMessage(error)}`);
    } finally {
      connectingConnectionId = '';
    }
  }

  function openConnectionTab(connection) {
    void connectSavedConnection(connection);
  }

  async function savedConnectionForSession(session) {
    const response = await fetch(`${BACKEND_URL}/api/connections`);
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(typeof data?.detail === 'string' ? data.detail : `HTTP ${response.status}`);
    }
    if (
      !Array.isArray(data) ||
      data.some(
        (connection) =>
          typeof connection?.id !== 'string' ||
          typeof connection.name !== 'string' ||
          typeof connection.host !== 'string' ||
          !Number.isInteger(connection.port) ||
          typeof connection.username !== 'string'
      )
    ) {
      throw new Error('Backend returned invalid saved connections.');
    }
    const existing = data.find((connection) => sshServerKey(connection) === sshServerKey(session));
    if (existing) {
      connectionNames = data;
      return existing;
    }

    if (session.auth !== 'key_id' || typeof session.key_id !== 'string') {
      throw new Error(
        'This session has no reusable saved credentials. Add it through SSH with its credentials saved before opening independent tabs.'
      );
    }

    const profile = {
      name: sshConnectionName(session, data),
      host: session.host,
      port: session.port,
      username: session.username,
      auth: 'key_id',
      key_id: session.key_id
    };
    const saveResponse = await fetch(`${BACKEND_URL}/api/connections`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(profile)
    });
    const saved = await saveResponse.json().catch(() => null);
    if (!saveResponse.ok) {
      throw new Error(
        typeof saved?.detail === 'string'
          ? saved.detail
          : `Could not save server profile (HTTP ${saveResponse.status}).`
      );
    }
    if (
      typeof saved?.id !== 'string' ||
      typeof saved.name !== 'string' ||
      typeof saved.host !== 'string' ||
      !Number.isInteger(saved.port) ||
      typeof saved.username !== 'string'
    ) {
      throw new Error('Backend saved the server but returned invalid connection details.');
    }
    connectionNames = [...data, saved];
    return saved;
  }

  async function openUnlistedSessionTab(session) {
    if (connectingConnectionId || creatingSessionProfileId) return;
    creatingSessionProfileId = session.session_id;
    terminalError = '';
    try {
      const saved = await savedConnectionForSession(session);
      if (saved) await connectSavedConnection(saved);
    } catch (error) {
      setTerminalError(`Could not open an independent tab: ${errorMessage(error)}`);
    } finally {
      creatingSessionProfileId = '';
    }
  }

  let sessionNames = {};

  async function loadSessions() {
    const requestVersion = ++sessionListRequest;
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
      if (requestVersion !== sessionListRequest) return;
      sessions = data;
      setServerUsernames(data);
      const currentSessionIds = new Set(sessions.map((session) => session.session_id));
      terminalTabs = terminalTabs.filter((tab) => currentSessionIds.has(tab.sessionId));
      const newSessionTabs = [];
      for (const session of sessions) {
        if (!knownSessionIds.has(session.session_id)) {
          newSessionTabs.push(createTerminalTab(session.session_id));
        }
      }
      knownSessionIds = currentSessionIds;
      if (!terminalTabs.some((tab) => tab.id === activeTerminalTabId)) {
        activeTerminalTabId = '';
      }
      if (
        initialSessionId &&
        initialSessionId !== lastInitialSessionId &&
        sessions.some((session) => session.session_id === initialSessionId)
      ) {
        lastInitialSessionId = initialSessionId;
        initialSessionApplied = true;
        activateSessionTab(initialSessionId);
        return;
      }
      if (!initialSessionApplied) {
        initialSessionApplied = true;
        if (sessions.some((session) => session.session_id === initialSessionId)) {
          activateSessionTab(initialSessionId);
          return;
        }
        if (!activeTerminalTabId && terminalTabs.length > 0) {
          selectTerminalTab(terminalTabs[0].id);
          return;
        }
      }
      if (selectedSessionId && currentSessionIds.has(selectedSessionId)) {
        if (!activeTerminalTabId && newSessionTabs.length > 0) {
          selectTerminalTab(newSessionTabs[0].id);
        }
        return;
      }
      if (terminalTabs.length > 0) {
        const nextTab = terminalTabs.find((tab) => tab.sessionId === sessions[0]?.session_id) || terminalTabs[0];
        selectTerminalTab(nextTab.id);
      } else if (selectedSessionId && !currentSessionIds.has(selectedSessionId)) {
        selectedSessionId = '';
        activeSession = null;
        sessionClosed = true;
        closeTerminalSocket();
        terminal?.reset();
        terminalState = 'disconnected';
        history = [];
        pendingRequests.clear();
        syncPendingRequestCount();
        interactivePrompt = null;
      }
    } catch (error) {
      if (requestVersion === sessionListRequest) {
        sessionListError = error.message || 'Could not load SSH sessions.';
      }
    } finally {
      if (requestVersion === sessionListRequest) sessionsLoading = false;
    }
  }

  function createTerminalTab(sessionId) {
    const tab = { id: `terminal-tab-${++terminalTabCounter}`, sessionId };
    terminalTabs = [...terminalTabs, tab];
    return tab;
  }

  function activateSessionTab(sessionId) {
    const tab = terminalTabs.find((candidate) => candidate.sessionId === sessionId) ||
      createTerminalTab(sessionId);
    selectTerminalTab(tab.id);
  }

  function selectTerminalTab(tabId) {
    const tab = terminalTabs.find((candidate) => candidate.id === tabId);
    if (!tab) return;
    activeTerminalTabId = tabId;
    sessionsMenuOpen = false;
    if (selectedSessionId !== tab.sessionId) selectSession(tab.sessionId);
    else {
      terminal?.focus();
      requestAnimationFrame(() => fitAddon?.fit());
    }
  }

  function closeTerminalTab(tabId) {
    const closingIndex = terminalTabs.findIndex((tab) => tab.id === tabId);
    if (closingIndex < 0) return;
    terminalTabs = terminalTabs.filter((tab) => tab.id !== tabId);
    if (activeTerminalTabId !== tabId) return;

    const nextTab = terminalTabs[Math.min(closingIndex, terminalTabs.length - 1)];
    if (nextTab) selectTerminalTab(nextTab.id);
    else activeTerminalTabId = '';
  }

  function selectSession(sessionId) {
    sessionsMenuOpen = false;
    if (selectedSessionId === sessionId) return;
    selectedSessionId = sessionId;
    closeTerminalSocket();
    sessionClosed = false;
    terminalHasStarted = false;
    lastSeq = 0;
    lineBuffer = '';
    cursor = 0;
    history = [];
    pendingRequests.clear();
    syncPendingRequestCount();
    interactivePrompt = null;
    lastCompletion = null;
    activeSession = sessions.find((session) => session.session_id === sessionId) || null;
    terminalError = '';
    terminalState = 'connecting';
    terminal?.reset();
    reconnectDelay = 1000;
    connectTerminal(sessionId);
    void loadHistory(sessionId);
  }

  function applySessionReplacement(replacement) {
    if (
      !mounted ||
      !isRecord(replacement) ||
      !Array.isArray(replacement.oldSessionIds) ||
      replacement.oldSessionIds.some((sessionId) => typeof sessionId !== 'string') ||
      !isRecord(replacement.newSession) ||
      typeof replacement.newSession.session_id !== 'string'
    ) {
      return;
    }

    const oldIds = new Set(replacement.oldSessionIds);
    if (oldIds.size === 0) return;
    const newSession = replacement.newSession;
    const wasSelected = oldIds.has(selectedSessionId);
    const activeOldTab = terminalTabs.find(
      (tab) => tab.id === activeTerminalTabId && oldIds.has(tab.sessionId)
    );

    sessions = [
      ...sessions.filter((session) => !oldIds.has(session.session_id)),
      newSession
    ];
    setServerUsernames(sessions);
    terminalTabs = terminalTabs.filter((tab) => !oldIds.has(tab.sessionId));
    knownSessionIds = new Set([
      ...[...knownSessionIds].filter((sessionId) => !oldIds.has(sessionId)),
      newSession.session_id
    ]);

    if (wasSelected) {
      const tab = activeOldTab
        ? { ...activeOldTab, sessionId: newSession.session_id }
        : { id: `terminal-tab-${++terminalTabCounter}`, sessionId: newSession.session_id };
      terminalTabs = [...terminalTabs, tab];
      activeTerminalTabId = tab.id;
      selectSession(newSession.session_id);
    }
  }

  function handleWindowClick(event) {
    if (sessionsMenuOpen && !sessionsMenuElement?.contains(event.target)) {
      sessionsMenuOpen = false;
    }
  }

  function handleWindowKeydown(event) {
    if (event.key === 'Escape') {
      sessionsMenuOpen = false;
      return;
    }
    if (event.ctrlKey && event.shiftKey && /^[1-9]$/.test(event.key)) {
      const connection = visibleConnections[Number(event.key) - 1];
      if (connection) {
        event.preventDefault();
        openConnectionTab(connection);
      }
    }
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
      allowProposedApi: true,
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
    searchAddon = new SearchAddon();
    terminal.loadAddon(fitAddon);
    terminal.loadAddon(new WebLinksAddon());
    terminal.loadAddon(searchAddon);
    terminal.loadAddon(new Unicode11Addon());
    terminal.unicode.activeVersion = '11';
    terminal.open(terminalHost);
    inputDisposable = terminal.onData(handleInput);
    terminal.attachCustomKeyEventHandler((event) => {
      if (event.type !== 'keydown') return true;
      const key = event.key.toLowerCase();
      if ((event.ctrlKey || event.metaKey) && key === 'c' && terminal.hasSelection()) {
        if (navigator.clipboard?.writeText) {
          navigator.clipboard.writeText(terminal.getSelection()).catch((error) => {
            setTerminalError(`Could not copy terminal selection: ${errorMessage(error)}`);
          });
        } else {
          setTerminalError('Clipboard access is unavailable in this browser.');
        }
        return false;
      }
      if ((event.ctrlKey || event.metaKey) && key === 'v') return false;
      if (event.ctrlKey && key === 'f') {
        const query = window.prompt('Search terminal output');
        if (query) searchAddon.findNext(query);
        else searchAddon.clearDecorations();
        return false;
      }
      return true;
    });

    const fitTerminal = () => {
      if (terminalHost.clientWidth > 0 && terminalHost.clientHeight > 0) fitAddon.fit();
    };
    resizeObserver = new ResizeObserver(fitTerminal);
    resizeObserver.observe(terminalHost);
    requestAnimationFrame(fitTerminal);

    const unsubscribe = onEvent('ssh.session', () => {
      void loadSessions();
    });
    const unsubscribeReplacement = sshSessionReplacement.subscribe(applySessionReplacement);
    void loadConnectionNames();
    void loadSessions();

    return () => {
      mounted = false;
      unsubscribe();
      unsubscribeReplacement();
      resizeObserver?.disconnect();
      inputDisposable?.dispose();
      closeTerminalSocket();
      terminal?.dispose();
    };
  });
</script>

<svelte:window on:click={handleWindowClick} on:keydown={handleWindowKeydown} />

<div class="terminal-page" class:terminal-page--hidden={hidden}>
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
  {:else if !activeTerminalTabId}
    <div class="empty-state">
      No terminal tab selected. Open an active session from the + menu; its SSH connection stays active when a tab closes.
    </div>
  {/if}

  {#if interactivePrompt}
    <div class="interactive-command" role="alert">
      <div>
        <strong>{interactivePrompt.program || 'Interactive command'}</strong>
        <p>{interactivePrompt.hint}</p>
      </div>
      <div class="interactive-actions">
        <button
          type="button"
          class="refresh-button"
          on:click={() => submitCommand(interactivePrompt.command, true)}
        >Run anyway</button>
        <button
          type="button"
          class="refresh-button"
          on:click={() => (interactivePrompt = null)}
        >Cancel</button>
      </div>
    </div>
  {/if}

  <section class="terminal-frame" aria-label="SSH terminal">
    <header class="terminal-titlebar">
      <div class="terminal-tabs" aria-label="SSH terminal sessions">
        <div class="terminal-tab-list" role="tablist" aria-label="Open terminal sessions">
          {#each visibleTerminalTabs as tab (tab.id)}
            <div class="terminal-tab" class:terminal-tab--active={tab.id === activeTerminalTabId}>
              <button
                type="button"
                class="terminal-tab-select"
                role="tab"
                aria-selected={tab.id === activeTerminalTabId}
                title={tabLabel(tab.session)}
                on:click={() => selectTerminalTab(tab.id)}
              >
                <span class="session-live-dot" aria-hidden="true"></span>
                <span class="terminal-tab-name">{tabLabel(tab.session)}</span>
              </button>
              <button
                type="button"
                class="terminal-tab-close"
                aria-label={`Close ${tabLabel(tab.session)} terminal tab`}
                title="Close terminal tab"
                on:click={() => closeTerminalTab(tab.id)}
              >×</button>
            </div>
          {/each}
        </div>
        <div class="terminal-session-menu-anchor" bind:this={sessionsMenuElement}>
          <div class="terminal-session-menu-actions">
            <button
              type="button"
              class="terminal-menu-trigger"
              aria-label="Open a server in a new terminal tab"
              aria-expanded={sessionsMenuOpen}
              on:click={() => {
                sessionsMenuOpen = !sessionsMenuOpen;
                menuNotice = '';
              }}
            >+</button>
          </div>
          {#if sessionsMenuOpen}
            <div class="terminal-session-menu" role="menu" aria-label="Servers">
              <span class="terminal-menu-heading">SERVERS</span>
              {#if connectionsLoading}
                <span class="terminal-menu-empty">Loading servers…</span>
              {:else if connectionNames.length === 0 && unlistedSessions.length === 0}
                <span class="terminal-menu-empty">No active or saved servers</span>
              {:else}
                {#each visibleConnections as connection, index (connection.id)}
                  <button
                    type="button"
                    class="terminal-menu-server"
                    role="menuitem"
                    disabled={Boolean(connectingConnectionId || creatingSessionProfileId)}
                    on:click={() => openConnectionTab(connection)}
                  >
                    <span
                      class="session-live-dot"
                      class:session-live-dot--offline={!sessionForConnection(connection)}
                      aria-hidden="true"
                    ></span>
                    <span class="terminal-menu-server-copy">
                      <span class="terminal-menu-server-name">
                        {connectingConnectionId === connection.id ? 'Connecting…' : connection.name}
                      </span>
                      <span class="terminal-menu-server-sub">
                        {connection.username}@{connection.host}:{connection.port || 22}
                      </span>
                    </span>
                    {#if index < 9}
                      <span class="terminal-menu-shortcut">Ctrl+Shift+{index + 1}</span>
                    {/if}
                  </button>
                {/each}
                {#each unlistedSessions as session (session.session_id)}
                  <button
                    type="button"
                    class="terminal-menu-server"
                    role="menuitem"
                    disabled={Boolean(connectingConnectionId || creatingSessionProfileId)}
                    on:click={() => void openUnlistedSessionTab(session)}
                  >
                    <span class="session-live-dot" aria-hidden="true"></span>
                    <span class="terminal-menu-server-copy">
                      <span class="terminal-menu-server-name">
                        {creatingSessionProfileId === session.session_id ? 'Saving profile…' : tabLabel(session)}
                      </span>
                      <span class="terminal-menu-server-sub">{session.username}@{session.host}:{session.port}</span>
                    </span>
                  </button>
                {/each}
              {/if}
              <div class="terminal-menu-divider"></div>
              <button type="button" class="terminal-menu-action terminal-menu-action--primary" role="menuitem" on:click={() => {
                sessionsMenuOpen = false;
                onNewSession();
              }}>＋ Add new server...</button>
              <button
                type="button"
                class="terminal-menu-action"
                role="menuitem"
                title="Server management is not available yet"
                on:click={() => (menuNotice = 'Server management is not available yet.')}
              >⚙ Manage servers</button>
              {#if menuNotice}
                <span class="terminal-menu-notice" role="status">{menuNotice}</span>
              {/if}
            </div>
          {/if}
        </div>
      </div>
      <div class="terminal-controls">
        <span class="terminal-state" data-state={terminalState}>
          {pendingRequestCount ? 'Command running — Ctrl+C to interrupt' : stateLabel(terminalState)}
        </span>
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
    <div class="terminal-host" class:terminal-host--hidden={!activeTerminalTabId} bind:this={terminalHost}></div>
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

  .terminal-page--hidden {
    display: none;
  }

  .terminal-titlebar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--synex-space-3);
    flex: 0 0 auto;
    min-height: 46px;
    padding: 0 var(--synex-space-3);
    border-bottom: 1px solid var(--synex-border-subtle);
    background: var(--synex-bg-surface);
  }

  .terminal-tabs,
  .terminal-tab-list,
  .terminal-tab,
  .terminal-session-menu-actions,
  .terminal-controls {
    display: flex;
    align-items: center;
    min-width: 0;
  }

  .terminal-tabs {
    position: relative;
    flex: 1 1 auto;
    gap: var(--synex-space-2);
  }

  .terminal-tab-list {
    gap: var(--synex-space-1);
    min-width: 0;
    overflow-x: auto;
  }

  .terminal-tab {
    flex: 0 0 auto;
    gap: var(--synex-space-2);
    min-width: 0;
    padding: 0 var(--synex-space-2);
    border: 1px solid var(--synex-border-subtle);
    border-radius: var(--synex-radius-md);
    background: var(--synex-bg-app);
  }

  .terminal-tab--active {
    border-color: var(--synex-accent);
  }

  .terminal-tab-select {
    display: inline-flex;
    align-items: center;
    gap: var(--synex-space-2);
    min-width: 0;
    height: 36px;
    color: var(--synex-text-primary);
  }

  .terminal-tab-name {
    max-width: 180px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--font-size-13);
  }

  .terminal-tab-close {
    flex: 0 0 auto;
    color: var(--synex-text-muted);
    font-size: 16px;
  }

  .terminal-tab-close:hover:not(:disabled) {
    color: var(--synex-text-primary);
  }

  .terminal-session-menu-anchor {
    position: relative;
    flex: 0 0 auto;
  }

  .terminal-session-menu-actions {
    height: 36px;
    overflow: hidden;
    border: 1px solid var(--synex-border-subtle);
    border-radius: var(--synex-radius-md);
    background: var(--synex-bg-hover);
  }

  .terminal-menu-trigger {
    height: 100%;
    color: var(--synex-text-primary);
  }

  .terminal-menu-trigger {
    width: 40px;
    font-size: 20px;
  }

  .terminal-menu-trigger:hover {
    color: var(--synex-accent);
  }

  .terminal-session-menu {
    position: absolute;
    z-index: 5;
    top: calc(100% + 8px);
    left: 0;
    width: min(420px, calc(100vw - 48px));
    padding: var(--synex-space-2);
    border: 1px solid var(--synex-border-strong);
    border-radius: var(--synex-radius-md);
    background: #1a2029;
    box-shadow: 0 12px 30px rgba(0, 0, 0, 0.4);
  }

  .terminal-menu-heading {
    display: block;
    padding: var(--synex-space-2) var(--synex-space-3);
    color: var(--synex-text-muted);
    font-size: var(--font-size-12);
    letter-spacing: 0.06em;
  }

  .terminal-menu-server,
  .terminal-menu-action {
    display: flex;
    align-items: center;
    width: 100%;
    min-height: 54px;
    padding: var(--synex-space-2) var(--synex-space-3);
    border-radius: var(--synex-radius-sm);
    text-align: left;
  }

  .terminal-menu-server {
    gap: var(--synex-space-3);
    color: var(--synex-text-primary);
  }

  .terminal-menu-server,
  .terminal-menu-action {
    transition: background-color 120ms ease, color 120ms ease;
  }

  .terminal-menu-server:hover:not(:disabled),
  .terminal-menu-action:hover {
    background: var(--synex-bg-hover);
    color: var(--synex-accent);
  }

  .terminal-menu-server:disabled {
    cursor: wait;
    opacity: 0.65;
  }

  .terminal-menu-server-copy {
    display: flex;
    flex: 1 1 auto;
    flex-direction: column;
    min-width: 0;
  }

  .terminal-menu-server-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .terminal-menu-server-sub,
  .terminal-menu-shortcut,
  .terminal-menu-empty {
    color: var(--synex-text-muted);
    font-size: var(--font-size-12);
  }

  .terminal-menu-shortcut {
    flex: 0 0 auto;
  }

  .terminal-menu-empty {
    display: block;
    padding: var(--synex-space-3);
  }

  .terminal-menu-notice {
    display: block;
    padding: var(--synex-space-2) var(--synex-space-3);
    color: var(--synex-text-muted);
    font-size: var(--font-size-12);
  }

  .terminal-menu-divider {
    margin: var(--synex-space-2) 0;
    border-top: 1px solid var(--synex-border-subtle);
  }

  .terminal-menu-action {
    min-height: 40px;
    color: var(--synex-text-primary);
  }

  .terminal-menu-action--primary {
    color: var(--synex-accent);
  }

  .session-live-dot {
    flex: 0 0 auto;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--synex-ok);
  }

  .session-live-dot--offline {
    background: var(--synex-offline);
  }

  .terminal-controls {
    gap: var(--synex-space-2);
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
    cursor: not-allowed;
    opacity: 0.5;
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

  .interactive-command {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--synex-space-4);
    padding: var(--synex-space-3) var(--synex-space-4);
    border: 1px solid var(--synex-warning);
    border-radius: var(--synex-radius-sm);
    background: var(--synex-warning-bg);
    color: var(--synex-text-primary);
  }

  .interactive-command p {
    margin: var(--synex-space-1) 0 0;
    color: var(--synex-text-secondary);
  }

  .interactive-actions {
    display: flex;
    gap: var(--synex-space-2);
    flex: 0 0 auto;
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

  .terminal-host--hidden {
    display: none;
  }

  @media (max-width: 640px) {
    .terminal-state {
      display: none;
    }

    .terminal-titlebar {
      padding: 0 var(--synex-space-3);
    }

    .terminal-tab-name {
      max-width: 110px;
    }

    .terminal-state {
      max-width: 90px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  }
</style>
