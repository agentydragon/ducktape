# E2E Tests (Playwright)

## Current Status

These E2E tests currently **cannot run** in the development environment due to a fundamental GLIBC incompatibility.

### Error

```
/lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.36' not found
```

### Root Cause

Playwright's pip package bundles a precompiled Node.js binary with a hardcoded interpreter (`/lib64/ld-linux-x86-64.so.2`) that links to the system's GLIBC. When the Nix development environment adds newer libraries (libstdc++.so.6 from gcc-14.3.0) to LD_LIBRARY_PATH, those libraries require GLIBC 2.38 symbols that the system's older GLIBC doesn't provide.

This creates an irreconcilable conflict: we can't use Nix's newer GLIBC globally (breaks system binaries like Docker), and we can't prevent Playwright's Node.js from loading Nix's libstdc++.

## Recommended Solution: Docker

Run E2E tests in a Docker container with Ubuntu 24.04 or similar (GLIBC 2.36+):

```dockerfile
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y python3.12 python3-pip
COPY . /workspace
WORKDIR /workspace
RUN pip install -e '.[dev]'
RUN python -m playwright install --with-deps chromium
CMD ["pytest", "tests/e2e/", "-v"]
```

## Alternative: Skip E2E Tests

The non-E2E agent tests work fine (48 passing):

```bash
pytest tests/agent/ -m "not e2e"
```

## Test Infrastructure

Despite the Playwright issue, the E2E test infrastructure has been updated:

- **E2EPageHelper**: Comprehensive fixture class with UI interaction methods
- **Agent-driven operations**: Tests use mocked OpenAI responses to simulate agent behavior
- **Proper typing**: Uses `AgentID` type throughout
- **Clean fixtures**: `ServerHandle` dataclass for server management

### Updated Tests

- `test_proposals_reject.py` - Policy proposal rejection via agent
- `test_notifications_handler.py` - System notifications via agent tool calls
- `test_ui.py` - UI creation, chat, and persistence
- `test_abort.py` - Abort functionality
- `test_approvals.py` - Approval workflow

All tests pass pre-commit checks and are ready to run once Playwright is fixed.

## Running E2E Tests (when environment is fixed)

```bash
# Single test
pytest tests/e2e/test_proposals_reject.py -v

# All E2E tests
pytest tests/e2e/ -v

# With headful browser (for debugging)
ADGN_E2E_HEADLESS=0 pytest tests/e2e/ -v
```

## Browser Selection

Set `ADGN_E2E_BROWSER` to choose browser (default: chromium):

```bash
ADGN_E2E_BROWSER=firefox pytest tests/e2e/ -v
```
