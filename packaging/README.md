# Building Synex

[Русская версия](README.ru.md)

Synex ships as a desktop app: one folder (or `Synex.app`) with the backend, the UI and the
`synex_worker` of that OS. There is a builder per OS; each one gets its own worker and then
runs the shared steps in [`build.py`](build.py).

| OS | Builder | Worker | Result in `packaging/dist` |
|---|---|---|---|
| Windows x64 | [`windows/build.ps1`](windows/build.ps1) | `synex_worker_windows.exe` from the repo (`-FromCore` builds it from `core\`) | `Synex-<v>-windows-x64.zip`, `Synex-<v>-windows-x64-setup.exe` (with Inno Setup 6) |
| Linux x86_64 | [`linux/build.sh`](linux/build.sh) | `synex_worker_linux` from the repo (`--from-core` builds it; arm64 always does) | `Synex-<v>-linux-x64.tar.gz` |
| macOS | [`macos/build.sh`](macos/build.sh) | bundled worker for this Mac (Apple Silicon or Intel) | `Synex-<v>-macos-<arch>.zip` with `Synex.app` |

The version comes from [`VERSION`](../VERSION). Next to the archives: `SHA256SUMS.txt`.

The worker in the repo is built on Ubuntu 24.04 and needs glibc 2.38 or newer. The release
workflow builds the Linux worker from `core/` on Ubuntu 22.04, so the published Linux build
also runs on Ubuntu 22.04 and Debian 12.

## What you need

- Python 3.12 and [uv](https://docs.astral.sh/uv/), Node.js 22 (as for development).
- `-FromCore` / `--from-core`: Rust ([rustup](https://rustup.rs)); macOS also the
  Xcode Command Line Tools.
- Windows installer (optional): [Inno Setup 6](https://jrsoftware.org/isinfo.php).

PyInstaller (and pywebview for the window on Windows and macOS) is installed only for the
build (`uv run --with`), it is not in `uv.lock`.

## How to build

From the repository root; the result is in `packaging/dist`.

**PowerShell** (Windows):

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1             # worker from the repo
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -FromCore   # worker built from core\
```

**bash** (Linux, macOS):

```bash
bash packaging/linux/build.sh               # Linux: worker from the repo
bash packaging/linux/build.sh --from-core   # Linux: worker built from core/
bash packaging/macos/build.sh               # macOS: bundled worker for the host CPU
```

With PowerShell 7 `pwsh packaging\windows\build.ps1` works too. `-NoInstaller` skips the
Windows installer.

## What a build does

1. Builds the UI with same-origin API and WebSocket URLs (`npm ci` if needed, `npm run build`).
2. Checks that the worker is an executable of this OS and copies it into the build.
3. Runs PyInstaller with [`synex.spec`](synex.spec): `Synex/` (onedir, starts fast) or
   `Synex.app`. Icons: [`assets/`](assets) (the logo from the Figma design).
4. **Smoke test**: starts the build without a window and with an empty data folder, waits
   for `/api/health` with the bundled worker running and checks that `/` serves the UI.
   Release builds also run Chromium against the same bundle on two ports. The check waits for
   the UI's alerts response and WebSocket greeting, then reads `/api/health` in the page.
5. Packs the archive and its SHA-256.

Options of `build.py` go after the builder's own ones: `--clean` (fresh `npm ci`),
`--no-window` (no pywebview: the UI opens in the browser), `--no-smoke`. Set `PORT` in `synex.env` at runtime.

## How the app runs

`Synex` (`Synex.exe`, `Synex.app`) is [`backend/app/desktop.py`](../backend/app/desktop.py):

- reads `synex.env` from the user data folder (`%LOCALAPPDATA%\Synex`,
  `~/Library/Application Support/Synex`, `~/.local/share/synex`); the first start creates
  it with examples. Every setting from `backend/.env.example` works there; environment
  variables win. Keys, connections, terminal history and alert rules are in the same
  folder, so they survive updates;
- if Synex is already running, opens it again instead of starting a second copy;
- starts the backend on `127.0.0.1:8000`; the backend starts the bundled worker and serves
  the UI (`UI_DIR`);
- opens a window (WebView2 on Windows 10/11, WebKit on macOS) or the browser (Linux, or
  `--browser`); closing the window stops the backend and the worker;
- the Windows and macOS builds have no console: the log is `logs/synex.log` in the data
  folder (the previous start: `synex.log.1`).

`Synex --headless` runs the backend without a window (tests, servers), `Synex --version`
prints the version. From the sources: `uv run python -m app.desktop` in `backend/` after
`npm run build` in `frontend/` (see
[RUNNING.md](../RUNNING.md#as-an-app-one-window-no-dev-server)).

On Linux, `sh install.sh` inside the unpacked folder adds Synex to the applications menu
(`--remove` takes it away).

## Memory

[`measure_memory.py`](measure_memory.py) starts a build (or the backend from sources) with
an empty data folder and measures RSS and USS of the backend, the worker and, with
`--window`, the WebView processes:

**PowerShell** (Windows):

```powershell
uv run --project backend --with psutil python packaging/measure_memory.py `
    --app packaging\dist\Synex\Synex.exe
# with SSH scenarios (1 session + terminal + live log, then 5 sessions):
#   --ssh user@host:22 --key $HOME\.ssh\id_ed25519
```

**bash** (Linux, macOS):

```bash
uv run --project backend --with psutil python packaging/measure_memory.py \
    --app packaging/dist/Synex/Synex      # macOS: packaging/dist/Synex.app/Contents/MacOS/Synex
# with SSH scenarios (1 session + terminal + live log, then 5 sessions):
#   --ssh user@host:22 --key ~/.ssh/id_ed25519
```

The report goes to the console and to `packaging/dist/memory-<os>-<arch>.md`. The release
workflow runs it on every OS and puts the tables into the release notes. Target (SYN-72):
under 200 MB idle without the window.

Linux build 0.1.0 (Ubuntu, x86_64, settle 15 s):

| Scenario | Backend USS | Worker USS | Total USS | Total RSS |
|---|---:|---:|---:|---:|
| idle | 90.1 MB | 2.7 MB | 92.8 MB | 97.7 MB |
| UI connected (/ws, metrics) | 91.2 MB | 2.7 MB | 93.9 MB | 98.9 MB |
| 1 SSH session, terminal, live log | 91.9 MB | 5.2 MB | 97.1 MB | 102.0 MB |
| 5 SSH sessions | 92.0 MB | 5.6 MB | 97.6 MB | 102.5 MB |

The window adds its WebView: measure it on Windows and macOS with `--window`.

## Releasing

1. Put the new version into `VERSION` and describe it in `CHANGELOG.md` and
   `CHANGELOG.ru.md` (a `## [X.Y.Z] - date` section).
2. Merge into `main`, then tag the fresh `main`:

   **PowerShell** (Windows):

   ```powershell
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

   **bash** (Linux, macOS):

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

3. The [release workflow](../.github/workflows/release.yml) builds Windows, Linux and macOS
   (Apple Silicon and Intel), runs the smoke test and the memory check, and creates the
   GitHub Release with the archives, `SHA256SUMS.txt` and the notes from the changelog.
   `0.x` versions are marked as pre-release; `v1.0.0` will be the first full release.

A manual run (Actions → release → Run workflow) only builds and keeps the files as
artifacts.

## macOS signing and license

After PyInstaller, the builder ad-hoc signs the entire `Synex.app`. Before creating the archive, CI checks the signature with `codesign --verify --deep --strict`, the launcher and worker architecture with `lipo`, and the launcher with `Synex --version`. Those checks repeat after extracting the ZIP. To clear quarantine on a verified download, follow [RUNNING.md](../RUNNING.md#run-a-downloaded-build).

Trusted Gatekeeper distribution requires a Developer ID Application certificate and its private key, Hardened Runtime, a `notarytool` submission and stapling the ticket. Store the certificate and its password, and the Apple API credentials in a protected GitHub release environment. The ZIP and ad-hoc builds do not need signing credentials. [Apple documentation](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution/)

The MIT license is next to the executable on Windows and Linux, in `Synex.app/Contents/Resources/LICENSE` on macOS, and a separate GitHub Pre-release asset. No activation is required. Memory results are in the workflow and build reports. `N/A` marks unavailable metrics. CI requires RSS measurements and publishes USS when available.

## Not yet

- Windows Authenticode and automated Apple notarization.
- AppImage or deb/rpm for Linux, dmg for macOS.
- Auto-updates.
