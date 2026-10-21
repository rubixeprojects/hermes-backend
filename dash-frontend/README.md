# Hermes Dash frontend

Standalone Dash client for the existing Hermes backend. Routes `/login` and `/chat` are distinct. Password sign-in uses backend HttpOnly cookies held in a server-side, expiring in-memory requests session; the browser stores only a random opaque session key in memory. No backend cookie, password, token, or ticket is stored in browser storage.

## Install and run

From this directory:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python hermes_dash.py
```

Open http://127.0.0.1:8050. Set `HERMES_DASH_HOST` to change the bind address (default `127.0.0.1`). Configure the trusted backend with `HERMES_BACKEND_URL` (default `http://127.0.0.1:9119`); it supports path prefixes and is never browser-editable. HTTPS is a prerequisite for sending passwords when the Dash app is exposed to a network; use HTTPS for both the app and backend.

Tool inputs and results can contain private data and are intentionally visible to the signed-in user. The app does not log payloads. Plain HTTP transmits session data unencrypted, including when used on a private network; use HTTPS wherever traffic crosses a network.

Run focused tests with `python -m pytest -q`.

This client does not modify the backend or claim real-model end-to-end validation.
