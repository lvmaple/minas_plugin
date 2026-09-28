# Repository Guidelines

## Project Structure & Module Organization

Each `xiaomi-nas-plugin-*` directory is a deployable plugin. `INFO` holds metadata, `scripts/control` handles lifecycle actions, and `deploy/` contains packaging and installation files. Python backends live in `src/files/` (or `files/` for `sysmon`); web UIs and CGI entry points live in `src/ui/` (or `ui/`). The root `deploy-plugins.py` provides a shared deployment menu; `tests/` covers it. Read the affected plugin's `README.md` before changing its NAS behavior.

## Build, Test, and Development Commands

- `python -X utf8 -m unittest discover -s tests -v` runs the root deployment tests (UTF-8 avoids Windows console encoding failures).
- `python deploy-plugins.py --list` lists plugins with valid `INFO` and NAS installation entry points without connecting to a NAS.
- `python deploy-plugins.py --help` shows configuration and selection options.
- `python xiaomi-nas-plugin-dockerctl/deploy/pack.py` builds its ZIP locally. Other plugins have `deploy/package-and-upload.ps1` scripts; inspect parameters first because they can install to a NAS.

There is no repository-wide build system or configured formatter/linter.

## Coding Style & Naming Conventions

Use four spaces in Python and follow surrounding JavaScript, shell, and PowerShell style. Match the `plugin` field in `INFO` to directory name `xiaomi-nas-plugin-<plugin_id>`. Name Python tests `test_*.py` and methods `test_*`. Keep ZIP paths POSIX-style (`/`) on Windows. Avoid hand-editing bundled Motrix files under `src/ui/assets/`.

## Testing Guidelines

The automated suite uses Python's `unittest`; no coverage target is configured. Add focused tests for shared deployment changes. For plugin changes, verify the affected local service or package and document any NAS validation performed. A package build alone does not verify installation.

## Commit & Pull Request Guidelines

History contains one commit (`Publish sanitized Xiaomi NAS plugins`), so no convention is established. Use a short, imperative subject. Pull requests should summarize affected plugins and NAS behavior, list validation results, link relevant issues, and include UI screenshots when applicable. After changing PR metadata, read it back through the GitHub API to verify title, a body with summary and validation, links to the intended branch or document, and non-ASCII text without U+FFFD. Set PowerShell stdin, stdout, and `$OutputEncoding` to UTF-8 when sending non-ASCII PR content.

## Security & Agent Scope

Copy `deploy-config.example.json` to ignored `deploy-config.json` for local settings. Do not commit NAS addresses, private keys, tokens, or device-specific records. The complete Superpowers workflow requires explicit opt-in; ordinary edits authorize task-specific work and proportionate verification. Avoid unrequested process documents and review loops. Report adjacent findings before expanding scope.
