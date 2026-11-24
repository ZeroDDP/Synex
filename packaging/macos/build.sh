#!/usr/bin/env bash
# Сборщик Synex для macOS. Готового воркера для macOS в репозитории нет: он всегда
# собирается из core/ под архитектуру этого Mac (Apple Silicon или Intel). Нужен Rust
# (https://rustup.rs) и Xcode Command Line Tools.
#
#   bash packaging/macos/build.sh [параметры build.py]
#
# Результат: packaging/dist/Synex-<версия>-macos-<арх>.zip с Synex.app. Приложение не
# подписано: при первом запуске macOS спросит разрешение (правый клик → Открыть).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

echo "==> Worker: cargo build --release in core/ ($(uname -m))"
# Воркер для macOS 11 и новее на любом Mac сборки: цель отсюда берут и Rust, и C-части (cc)
(cd "$ROOT/core" && MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-11.0}" \
    cargo build --release -p synex_worker)
WORKER="$ROOT/core/target/release/synex_worker"

cd "$ROOT"
if [[ "$(uname -m)" == "x86_64" ]]; then
    # cryptography ships no Intel Mac wheel. Link its extension statically so
    # the app cannot load Python's different libssl from the bundle at startup.
    openssl_prefix="$(brew --prefix openssl@3)"
    export OPENSSL_DIR="$openssl_prefix" OPENSSL_STATIC=1
    # Keep wheels and environments from earlier dynamic builds out of this bundle.
    cache_dir="$(uv cache dir)"
    export UV_CACHE_DIR="${cache_dir}-synex-intel-static"
    export UV_PROJECT_ENVIRONMENT="$ROOT/packaging/.venv/macos-intel-static"
fi
uv run --project backend python packaging/build.py --worker "$WORKER" "$@"
