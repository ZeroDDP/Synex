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
(cd "$ROOT/core" && cargo build --release -p synex_worker)
WORKER="$ROOT/core/target/release/synex_worker"

cd "$ROOT"
uv run --project backend python packaging/build.py --worker "$WORKER" "$@"
