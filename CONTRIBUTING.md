# How we work with the repository

**English** | [Русский](CONTRIBUTING.ru.md)

## Branches and pull requests

- `main` is always a working version. Nobody pushes to it directly.
- Your own task, your own branch: `<area>/<task>`, for example `core/worker`, `frontend/ui`, `backend/api`.
- Done: open a pull request into `main`. If someone can review it, ask one reviewer; if there is no time, merge it yourself, but only with green checks.
- Before a PR, pull the fresh `main`: `git pull --rebase origin main`.

```bash
git switch -c <area>/<task>               # new branch
git add <files>
git commit -m "<short message>"
git push -u origin <area>/<task>          # then open a PR on GitHub
```

## Commits

- A short message in the imperative mood: "Add IPC client", "Fix worker startup".
- One commit, one logical change. The commit history is part of the grade.

## What must not be committed

- `.env`, keys and tokens: they live only in `.env`.
- Project data: everyone keeps it locally.
- Files over 10 MB, caches, logs, virtual environments: `.gitignore` filters them out, but check `git status` before committing.
