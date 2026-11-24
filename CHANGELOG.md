# Changelog

All notable changes to Synex. Versions follow [Semantic Versioning](https://semver.org/):
`0.x` releases are previews on the way to `1.0.0`. The version lives in the `VERSION` file;
the [release workflow](.github/workflows/release.yml) publishes a tag `vX.Y.Z` with builds
for Windows, Linux and macOS.

[Русская версия](CHANGELOG.ru.md)

## [0.3.0] — unreleased

- The Rust worker accesses Docker Engine over SSH. The existing API serves containers, images, stats and logs.
- The backend selects a worker for Windows x64, Linux x64, Intel Macs and Apple Silicon. Workers support SSH command and SFTP transfer cancellation.
- Updated Dashboard, Servers, Logs and Terminal views, including SSH usernames for selected servers.

## [0.2.0] - 2026-11-24

- Added IPC for external plugin hosts, local package installation and updates, permissions and REST/WebSocket actions.
- Updated navigation, pages and SSH terminal. Packaged UI uses the application's port. Release builds check HTTP and WebSocket on two ports.

## [0.1.0] - 2026-11-20

The first build of Synex as a desktop app: one download, no Python, Node.js or Rust
needed.

### App

- Builds for Windows 10/11 x64 (zip and installer), Linux x86_64 (tar.gz, Ubuntu 22.04,
  Debian 12 or newer) and macOS 11+ (Apple Silicon and Intel, zip with `Synex.app`). Each
  build carries its own `synex_worker`.
- The app starts the backend and the worker and opens the UI in its own window (WebView2
  on Windows, WebKit on macOS) or in the browser (Linux). A second start opens the running
  copy. Closing the window stops everything.
- User data stays in one place across versions: `%LOCALAPPDATA%\Synex`,
  `~/Library/Application Support/Synex`, `~/.local/share/synex` (keys, saved connections,
  terminal history, alert rules, `synex.env` settings, `logs/`).
- Memory without the window: about 100 MB (backend and worker), see the release notes.

### Features

- SSH: several sessions, encrypted key vault, saved connections, server key verification
  (TOFU, OpenSSH `known_hosts`).
- Terminal: command history per server, Tab completion, Ctrl+C interrupts the running
  command, commands up to 10 minutes, a hint instead of a hang for programs that need a
  real terminal (vim, top, tmux).
- Monitoring: CPU, memory, disks, network and GPU of every server, twice a second.
- Alerts: thresholds for CPU, memory, disks, GPU temperature and "server does not
  respond" on all servers, with history and acknowledgement.
- Logs of the selected server: systemd journal with filters by unit, level, time and
  text, log files from `/var/log`, both live.
- Files: two-pane SFTP manager, folders with verification, search on the server and on
  this computer.
- Docker: containers, actions, logs and live logs. Git: status, history, pull, deploy.
- Plugins: every module can be switched on and off while the app runs.

### Known limitations

- The terminal has no real TTY yet: full-screen programs do not work (needs the core's
  PTY support).
- Builds are not signed: Windows SmartScreen and macOS Gatekeeper ask for confirmation
  on the first start.
- The UI is built for port 8000: if it is taken, set another `PORT` in `synex.env` and
  rebuild, or free the port.
