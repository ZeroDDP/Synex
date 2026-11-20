#!/usr/bin/env bash
# Сборщик Synex для Linux. Воркер: готовый из репозитория (backend/app/core/target/release,
# x86_64) или собранный из core/ (--from-core, нужен Rust; на arm64 всегда так).
#
#   bash packaging/linux/build.sh [--from-core] [параметры build.py]
#
# Результат: packaging/dist/Synex-<версия>-linux-<арх>.tar.gz (папка Synex с программой,
# synex.desktop и install.sh для меню приложений).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FROM_CORE=0
if [ "${1:-}" = "--from-core" ]; then FROM_CORE=1; shift; fi
if [ "$(uname -m)" != "x86_64" ]; then FROM_CORE=1; fi

if [ "$FROM_CORE" = 1 ]; then
  echo "==> Worker: cargo build --release in core/"
  (cd "$ROOT/core" && cargo build --release -p synex_worker)
  WORKER="$ROOT/core/target/release/synex_worker"
else
  WORKER="$ROOT/backend/app/core/target/release/synex_worker"
fi

cd "$ROOT"
uv run --project backend python packaging/build.py --worker "$WORKER" "$@"
