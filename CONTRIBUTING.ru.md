# Как работаем с репозиторием

[English](CONTRIBUTING.md) | **Русский**

## Ветки и pull request

- `main` — всегда рабочая версия. Напрямую в неё не пушим.
- Своя задача — своя ветка: `<область>/<задача>`, например `core/worker`, `frontend/ui`, `backend/api`.
- Готово — открываем pull request в `main`. Если есть кому посмотреть, просим одного ревьюера; не успеваем — мержим сами, но только с зелёными проверками.
- Перед PR подтягиваем свежий `main`: `git pull --rebase origin main`.

Например, задача «поиск файлов» в backend:

**PowerShell** (Windows):

```powershell
git switch -c backend/search              # новая ветка: <область>/<задача>
git add backend/app/plugins/sftp/search.py
git commit -m "Добавить поиск файлов"
git push -u origin backend/search         # дальше — PR на GitHub
```

**bash** (Linux, macOS):

```bash
git switch -c backend/search              # новая ветка: <область>/<задача>
git add backend/app/plugins/sftp/search.py
git commit -m "Добавить поиск файлов"
git push -u origin backend/search         # дальше — PR на GitHub
```

## Коммиты

- Короткое сообщение в повелительном наклонении: «Добавить клиент IPC», «Исправить загрузку worker».
- Один коммит — одно логическое изменение. История коммитов входит в оценку.

## Пре-релиз

Сначала влейте PR с изменениями и проверьте матрицу Windows, Linux, macOS Intel и macOS ARM64. В коммите для тега должны совпадать `VERSION`, версия Python-пакета и раздел обеих версий CHANGELOG. Отправка тега `vX.Y.Z` запускает публикацию. В релиз входит MIT-лицензия. Ключи подписи и API Apple хранятся в GitHub Secrets окружения релиза.

## Что нельзя коммитить

- `.env`, ключи и токены — храним только в `.env`.
- Данные проекта — они у каждого локально.
- Файлы больше 10 МБ, кэш, журналы, виртуальные окружения — их отсекает `.gitignore`, но перед коммитом смотрим `git status`.
