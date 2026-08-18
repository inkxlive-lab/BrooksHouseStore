# BrooksHouse Store Development Instructions

## Project role

Codex is the hands-on developer for the BrooksHouse Store application. The user and their ChatGPT project-lead conversation make product and architecture decisions.

## Authoritative local project

- Local source: C:\BrooksHouseStore
- FastAPI target: app.main:app
- Local development port: 8001
- Protected local database: app\data\brookshouse_store.db
- Railway deployment uses a separate deployment source and must not be assumed identical to this local folder.

## Mandatory safety rules

1. Inspect existing code before editing it.
2. Preserve existing features and user changes.
3. Never delete, replace, reset, recreate, migrate, copy over, or modify the protected database without explicit user approval.
4. Never use the protected database for destructive tests.
5. Do not import or start app.main merely as a syntax check because application startup can perform schema and data modifications.
6. Use static checks that do not execute application startup whenever possible.
7. Back up every affected existing file before structural or high-risk changes.
8. Keep backups outside active import and template paths whenever possible.
9. Never read, display, copy, commit, or expose secrets, private keys, tokens, credentials, .env contents, PEM contents, or VAPID secrets.
10. Do not stop, restart, reconfigure, or replace the Windows scheduled task or the process using port 8001 without explicit approval.
11. Do not modify or deploy Railway production without explicit approval.
12. Do not run Railway deployment commands unless explicitly requested.
13. Do not assume the local SQLite database and Railway database contain identical data.
14. Do not initialize Git or create commits until .gitignore and secret/database exclusions have been reviewed and approved.
15. Do not delete or reorganize historical backups, patches, ZIPs, logs, spreadsheets, generated files, or duplicate-looking files without explicit approval.
16. Before modifying a route, search for duplicate routes, install functions, templates, JavaScript callers, and Railway-specific differences.
17. Test every permitted change before reporting success.
18. Report all files created, changed, moved, or deleted and every test performed.
19. If a requested action could affect inventory quantities, transactions, marketplace data, reward points, user access, or production data, stop and explain the risk before proceeding.
20. Do not automatically report to another ChatGPT conversation. The user will carry results and errors between conversations.

## Development workflow

For each change:

1. Inspect the relevant implementation and dependencies.
2. Explain the proposed change and risks.
3. Identify files that will be affected.
4. Create timestamped backups when required.
5. Make the smallest focused change.
6. Perform safe static checks and targeted tests.
7. Report the exact outcome, remaining risks, and deployment status.
8. Treat local completion and Railway deployment as separate steps.

## Database rules

- The primary local database is app\data\brookshouse_store.db.
- DATABASE_URL may override the default for configured SQLAlchemy components.
- Some legacy modules use hard-coded SQLite paths.
- Resolve the actual intended database target before any approved database operation.
- Default all repair or migration utilities to preview/dry-run mode.
- Require an explicit apply flag for data-changing utilities.
- Create a verified backup before an approved data-changing operation.
- Never silently fall back to another database.

## Current structural cautions

- app\main.py is large and contains most routes.
- Application startup can execute schema and data changes.
- Database access is split between central configuration and direct sqlite3 connections.
- Historical patch and backup files exist beside active code.
- Parallel image and storage directories may have different purposes.
- The local project is not currently a Git repository.
