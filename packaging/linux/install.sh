#!/bin/sh
# Добавить Synex в меню приложений (для текущего пользователя). Запускать из папки
# Synex, распакованной из архива: sh install.sh. Удалить: sh install.sh --remove.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/512x512/apps"
if [ "${1:-}" = "--remove" ]; then
  rm -f "$APPS/synex.desktop" "$ICONS/synex.png"
  echo "Synex removed from the applications menu (the folder $HERE stays)"
  exit 0
fi
mkdir -p "$APPS" "$ICONS"
cp "$HERE/synex.png" "$ICONS/synex.png"
sed -e "s|@EXEC@|\"$HERE/Synex\"|" -e "s|@ICON@|synex|" "$HERE/synex.desktop" > "$APPS/synex.desktop"
chmod +x "$APPS/synex.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" || true
echo "Synex added to the applications menu. Or run: $HERE/Synex"
