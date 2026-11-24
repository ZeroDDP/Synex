#!/usr/bin/env bash
# Берёт готовый воркер Core для процессора этого Mac.
#
#   bash packaging/macos/build.sh [параметры build.py]
#
# Результат: packaging/dist/Synex-<версия>-macos-<арх>.zip с Synex.app. Приложение не
# подписано: при первом запуске macOS спросит разрешение (правый клик → Открыть).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

case "$(uname -m)" in
    arm64) worker_name=synex_worker_aarch64-apple-darwin ;;
    x86_64) worker_name=synex_worker_x84_64-apple-darwin ;;
    *) echo "Unsupported macOS architecture" >&2; exit 1 ;;
esac
WORKER="$ROOT/backend/app/core/target/release/$worker_name"

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
