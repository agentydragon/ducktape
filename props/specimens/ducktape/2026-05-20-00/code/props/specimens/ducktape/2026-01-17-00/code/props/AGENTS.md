@README.md

# Agent Guide

## Component Documentation

- **Core library:** @core/AGENTS.md
- **Backend API:** @backend/AGENTS.md
- **Tests:** @core/testing/AGENTS.md

## Service Management Rules

**Never start services manually.** Manual service starts will:

1. Block process-compose from starting (port conflict: "Address already in use")
2. Watch wrong directories (code changes won't reload)
3. Break the devenv-managed workflow

Forbidden commands:

- `uvicorn` directly
- Starting the postgres container manually
- Killing service PIDs without checking if they're process-compose managed

## Database Safety

### CRITICAL - NEVER DROP THE DATABASE WITHOUT PERMISSION

The `props db recreate` command drops ALL data including expensively-collected agent rollouts.

**NEVER run this command without the user's explicit verbal agreement.**
