"""Общая часть сборщиков Synex: UI, воркер, PyInstaller, архив, проверка.

Сборщики для каждой ОС (packaging/windows, linux, macos) находят или собирают воркер
своей ОС и вызывают этот скрипт:

    uv run --project backend python packaging/build.py --worker <путь к synex_worker>

Шаги:
1. UI: `npm ci` (если нет node_modules) и `npm run build` во frontend с
   адресами API относительно окна приложения, результат в packaging/work/stage/ui.
2. Воркер: проверка, что он для этой ОС (PE, ELF, Mach-O), копия в stage/worker.
3. PyInstaller (packaging/synex.spec): папка packaging/dist/Synex (на macOS Synex.app).
   PyInstaller и pywebview ставятся только на время сборки (`uv run --with`), в
   uv.lock их нет.
4. Подпись macOS и проверка запуска; в комплект копируется LICENSE.
5. Архив в packaging/dist: Synex-<версия>-<ос>-<архитектура>.zip или .tar.gz и
   SHA256SUMS.txt.

Версия берётся из файла VERSION в корне репозитория.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
BACKEND = ROOT / "backend"
PACKAGING = ROOT / "packaging"
WORK = PACKAGING / "work"
DIST = PACKAGING / "dist"
PYINSTALLER = "pyinstaller>=6.11,<7"
PYWEBVIEW = "pywebview>=5.3,<6"


def log(message: str) -> None:
    print(f"==> {message}", flush=True)


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("   $", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True, shell=False)


def target_os() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def target_arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        machine, machine
    )


def version() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------- UI


def build_ui(stage: Path, *, clean: bool) -> None:
    log("UI: frontend/dist")
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        raise SystemExit("npm not found: install Node.js 22")
    if clean or not (FRONTEND / "node_modules").is_dir():
        run([npm, "ci"], cwd=FRONTEND)
    # config.js strips the trailing slash: fetch and WebSocket resolve against
    # the document, even on port 5173 or when a developer has a local Vite .env.
    env = {**os.environ, "VITE_BACKEND_URL": "/"}
    run([npm, "run", "build"], cwd=FRONTEND, env=env)
    target = stage / "ui"
    shutil.copytree(FRONTEND / "dist", target)
    if not (target / "index.html").is_file():
        raise SystemExit("frontend build has no index.html")


# ----------------------------------------------------------------- worker

_MAGIC = {"windows": (b"MZ",), "linux": (b"\x7fELF",), "macos": (
    b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe"
)}  # fmt: skip


def stage_worker(stage: Path, worker: Path) -> None:
    log(f"Worker: {worker}")
    if not worker.is_file():
        raise SystemExit(f"worker not found: {worker}")
    head = worker.read_bytes()[:4]
    expected = _MAGIC[target_os()]
    if not head.startswith(expected):
        raise SystemExit(f"{worker} is not a {target_os()} executable (starts with {head!r})")
    name = "synex_worker.exe" if target_os() == "windows" else "synex_worker"
    target = stage / "worker" / name
    target.parent.mkdir(parents=True)
    shutil.copy2(worker, target)
    if target_os() != "windows":
        target.chmod(0o755)


# ------------------------------------------------------------- PyInstaller


def build_app(stage: Path, *, window: bool) -> Path:
    log("PyInstaller")
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv not found: https://docs.astral.sh/uv/")
    command = [uv, "run", "--project", str(BACKEND), "--with", PYINSTALLER]
    if window:
        command += ["--with", PYWEBVIEW]
    command += [
        "pyinstaller",
        str(PACKAGING / "synex.spec"),
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST),
        "--workpath",
        str(WORK / "pyinstaller"),
    ]
    run(command, cwd=ROOT, env={**os.environ, "SYNEX_STAGE": str(stage)})
    built = DIST / ("Synex.app" if target_os() == "macos" else "Synex")
    if not built.exists():
        raise SystemExit(f"PyInstaller did not create {built}")
    return built


def add_extras(built: Path) -> None:
    """Add the project license and Linux desktop integration files."""
    if target_os() != "macos":
        shutil.copy2(ROOT / "LICENSE", built / "LICENSE")
    if target_os() != "linux":
        return
    for name in ("synex.desktop", "install.sh"):
        shutil.copy2(PACKAGING / "linux" / name, built / name)
    (built / "install.sh").chmod(0o755)
    shutil.copy2(PACKAGING / "assets" / "synex.png", built / "synex.png")


def executable(built: Path) -> Path:
    if target_os() == "macos":
        return built / "Contents" / "MacOS" / "Synex"
    return built / ("Synex.exe" if target_os() == "windows" else "Synex")


def sign_macos(built: Path) -> None:
    log("Ad-hoc signing macOS bundle")
    run(["codesign", "--force", "--deep", "--sign", "-", str(built)])


def verify_macos(built: Path, arch: str) -> None:
    log(f"Verify macOS signature and architecture ({arch})")
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(built)])
    license_file = built / "Contents" / "Resources" / "LICENSE"
    if not license_file.is_file() or license_file.read_bytes() != (ROOT / "LICENSE").read_bytes():
        raise SystemExit("The macOS bundle is missing the project license")
    programs = (
        executable(built),
        built / "Contents" / "Frameworks" / "worker" / "synex_worker",
    )
    for program in programs:
        run(["lipo", str(program), "-verify_arch", arch])
    result = subprocess.run(
        [str(executable(built)), "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    if result.stdout.strip() != f"Synex {version()}":
        raise SystemExit(f"Unexpected macOS launcher version: {result.stdout!r}")


def archive_macos(built: Path, arch: str) -> None:
    verify_macos(built, arch)
    path = archive(built)
    with tempfile.TemporaryDirectory(prefix="synex-release-") as folder:
        run(["ditto", "-x", "-k", str(path), folder])
        verify_macos(Path(folder) / built.name, arch)


# ----------------------------------------------------------------- archive


def archive(built: Path) -> Path:
    name = f"Synex-{version()}-{target_os()}-{target_arch()}"
    log(f"Archive: {name}")
    if target_os() == "linux":
        path = DIST / f"{name}.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            tar.add(built, arcname=name)
    elif target_os() == "macos":
        path = DIST / f"{name}.zip"
        # ditto сохраняет ссылки и атрибуты внутри .app
        run(["ditto", "-c", "-k", "--keepParent", str(built), str(path)])
    else:
        path = DIST / f"{name}.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for file in sorted(built.rglob("*")):
                arcname = Path(name) / file.relative_to(built)
                zf.write(file, arcname.as_posix())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with (DIST / "SHA256SUMS.txt").open("a", encoding="utf-8") as sums:
        sums.write(f"{digest}  {path.name}\n")
    size = path.stat().st_size / 1024 / 1024
    log(f"{path.name}: {size:.1f} MB, sha256 {digest}")
    return path


# ------------------------------------------------------------------- smoke


def smoke_test(built: Path, port: int, *, browser: bool = False) -> None:
    """Запустить сборку без окна с пустой папкой данных и проверить, что всё на месте."""
    log("Smoke test")
    program = executable(built)
    with tempfile.TemporaryDirectory(prefix="synex-smoke-") as home:
        env = {
            **os.environ,
            "HOME": home,
            "USERPROFILE": home,
            "LOCALAPPDATA": home,
            "XDG_DATA_HOME": home,
            "XDG_RUNTIME_DIR": home,
            # Без системного хранилища паролей (CI): мастер-ключ в переменной
            "KEYSTORE_MASTER_KEY": "Zm9yLXNtb2tlLXRlc3Qtb25seS0wMTIzNDU2Nzg5YWI=",
            "PORT": str(port),
        }
        process = subprocess.Popen([str(program), "--headless"], env=env)
        try:
            health = _wait_health(port, process)
            worker = health.get("worker", {})
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                page = response.read().decode("utf-8", "replace")
            if '<div id="app">' not in page:
                raise SystemExit("the build does not serve the UI on /")
            if browser:
                run(
                    [
                        "node",
                        str(PACKAGING / "browser-smoke.cjs"),
                        f"http://127.0.0.1:{port}",
                    ]
                )
            log(f"OK: plugins {health.get('plugins')}, worker pid {worker.get('worker_pid')}")
        finally:
            if os.name == "nt" and process.poll() is None:
                subprocess.run(["taskkill", "/pid", str(process.pid), "/t", "/f"], check=False)
            else:
                process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _wait_health(port: int, process: subprocess.Popen[bytes]) -> dict:
    deadline = time.monotonic() + 60
    last: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"the build exited with code {process.returncode}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                data = json.loads(r.read().decode("utf-8"))
            worker = data.get("worker", {})
            if worker.get("connected") and worker.get("worker_pid"):
                return data  # воркер из сборки запущен и отвечает
            last = RuntimeError(f"worker: {data.get('worker')}")
        except OSError as e:
            last = e
        time.sleep(0.5)
    raise SystemExit(f"the build did not answer /api/health: {last!r}")


# -------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Synex for this OS")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--worker", type=Path, help="synex_worker for this OS")
    mode.add_argument(
        "--archive-macos",
        choices=("arm64", "x86_64"),
        help="validate and archive an existing Synex.app for this architecture",
    )
    parser.add_argument(
        "--defer-archive",
        action="store_true",
        help="leave Synex.app for the separate CI validation step",
    )
    parser.add_argument("--port", type=int, help="deprecated: set PORT when starting Synex")
    parser.add_argument("--no-window", action="store_true", help="no pywebview: browser UI")
    parser.add_argument("--clean", action="store_true", help="npm ci even with node_modules")
    parser.add_argument("--no-smoke", action="store_true", help="skip the smoke test")
    parser.add_argument("--smoke-port", type=int, default=18765)
    parser.add_argument(
        "--browser-smoke",
        action="store_true",
        help="check the same build in Chromium on two consecutive ports",
    )
    args = parser.parse_args(argv)
    macos = target_os() == "macos"
    if (args.archive_macos or args.defer_archive) and not macos:
        parser.error("macOS packaging options require macOS")
    if args.archive_macos:
        expected_arch = "arm64" if target_arch() == "arm64" else "x86_64"
        if args.archive_macos != expected_arch:
            parser.error(f"expected a native macOS runner ({args.archive_macos})")
        archive_macos(DIST / "Synex.app", args.archive_macos)
        return 0
    if args.browser_smoke and (args.no_smoke or not 1 <= args.smoke_port <= 65534):
        parser.error("--browser-smoke requires smoke tests and two valid consecutive ports")
    if args.port is not None:
        log("--port is deprecated; set PORT in synex.env at runtime")

    log(f"Synex {version()} for {target_os()}-{target_arch()}")
    shutil.rmtree(WORK, ignore_errors=True)
    for old in DIST.glob("Synex*"):
        shutil.rmtree(old) if old.is_dir() else old.unlink()
    (DIST / "SHA256SUMS.txt").unlink(missing_ok=True)
    stage = WORK / "stage"
    stage.mkdir(parents=True)
    shutil.copy2(ROOT / "VERSION", stage / "VERSION")

    build_ui(stage, clean=args.clean)
    stage_worker(stage, args.worker.resolve())
    window = not args.no_window and target_os() in ("windows", "macos")
    built = build_app(stage, window=window)
    add_extras(built)
    if macos:
        sign_macos(built)
    if not args.no_smoke:
        smoke_test(built, args.smoke_port, browser=args.browser_smoke)
        if args.browser_smoke:
            smoke_test(built, args.smoke_port + 1, browser=True)
    if not args.defer_archive:
        if macos:
            archive_macos(built, "arm64" if target_arch() == "arm64" else "x86_64")
        else:
            archive(built)
    log(f"Done: {DIST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
