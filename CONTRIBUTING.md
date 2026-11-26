# How we work with the repository

**English** | [Русский](CONTRIBUTING.ru.md)

## Branches and pull requests

- `main` is always a working version. Nobody pushes to it directly.
- Your own task, your own branch: `<area>/<task>`, for example `core/worker`, `frontend/ui`, `backend/api`.
- Done: open a pull request into `main`. If someone can review it, ask one reviewer; if there is no time, merge it yourself, but only with green checks.
- Before a PR, pull the fresh `main`: `git pull --rebase origin main`.

For example, the task "file search" in the backend:

**PowerShell** (Windows):

```powershell
git switch -c backend/search              # new branch: <area>/<task>
git add backend/app/plugins/sftp/search.py
git commit -m "Add file search"
git push -u origin backend/search         # then open a PR on GitHub
```

**bash** (Linux, macOS):

```bash
git switch -c backend/search              # new branch: <area>/<task>
git add backend/app/plugins/sftp/search.py
git commit -m "Add file search"
git push -u origin backend/search         # then open a PR on GitHub
```

## Commits

- A short message in the imperative mood: "Add IPC client", "Fix worker startup".
- One commit, one logical change. The commit history is part of the grade.

## Pre-release

Merge the changes and check the Windows, Linux, macOS Intel and macOS ARM64 matrix. Align `VERSION`, the Python package version and both changelog entries at the commit you tag. Pushing `vX.Y.Z` starts publishing. The release includes the MIT license. Store Apple signing and API keys in GitHub Secrets for the release environment.

## What must not be committed

- `.env`, keys and tokens: they live only in `.env`.
- Project data: everyone keeps it locally.
- Files over 10 MB, caches, logs, virtual environments: `.gitignore` filters them out, but check `git status` before committing.
