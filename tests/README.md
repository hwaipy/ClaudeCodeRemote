# ClaudeCodeRemote E2E tests

Playwright (Python) + pytest. Tests boot a real uvicorn against a temp sqlite +
fake `claude` binary, then drive the UI through a real Chromium.

## One-time setup

```bash
cd ClaudeCodeRemote
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r tests/requirements-test.txt
playwright install chromium
chmod +x tests/fakes/fake_claude.py
```

## Run

```bash
cd ClaudeCodeRemote
source .venv/bin/activate
pytest tests/                     # headless
pytest tests/ --headed            # visible browser
pytest tests/e2e/test_login.py    # one file
pytest tests/ -k auto_logs_in     # one test by substring
pytest tests/ --tracing on        # record trace.zip on failure
```

Server logs from each session go to `/tmp/ccr-test-server-*.log` — check there
when fixtures fail to come up.

## Layout

```
tests/
├── conftest.py            # uvicorn fixture (session-scoped) + page helpers
├── fakes/fake_claude.py   # stand-in for `claude` CLI
├── pages/                 # Page Objects (selectors + actions per view)
├── e2e/                   # actual test files
│   ├── test_smoke.py      # L0: server boots, static reachable
│   └── test_login.py      # §1: login flow
└── requirements-test.txt
```

## Markers

- `spec_only`: encodes spec behavior not yet implemented in code (expected
  red until the feature lands). Skip with `pytest -m 'not spec_only'`.
