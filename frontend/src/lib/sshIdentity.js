export function sshServerKey(target) {
  const host = typeof target?.host === 'string' ? target.host.trim().toLowerCase() : '';
  const port = Number(target?.port || 22);
  if (!host || !Number.isInteger(port) || port < 1 || port > 65535) return '';
  return JSON.stringify([host, port]);
}

export function sshTargetKey(target) {
  const serverKey = sshServerKey(target);
  const username = typeof target?.username === 'string' ? target.username.trim() : '';
  return serverKey ? JSON.stringify([serverKey, username]) : '';
}

export function sshConnectionName(target, existingConnections) {
  const host = String(target.host).trim();
  const username = String(target.username).trim();
  const port = Number(target.port || 22);
  const base = `${username}@${host}${port === 22 ? '' : `:${port}`}`;
  let name = base.slice(0, 100);
  let suffix = 2;
  while (existingConnections.some((connection) => connection.name?.toLowerCase() === name.toLowerCase())) {
    const ending = ` (${suffix++})`;
    name = `${base.slice(0, 100 - ending.length)}${ending}`;
  }
  return name;
}

export function uniqueSshServers(items) {
  const seen = new Set();
  return items.filter((item) => {
    const key = sshServerKey(item);
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
