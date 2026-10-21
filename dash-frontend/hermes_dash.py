"""Cookie-authenticated Dash client for the Hermes gateway."""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import uuid
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import requests
import websocket

DEFAULT_URL = "http://127.0.0.1:9119"
PROTOCOL = "hermes-gateway-v1"
SESSION_TTL = 3600
REGISTRY_LIMIT = 256
_REGISTRY: dict[str, tuple[requests.Session, float]] = {}
_REGISTRY_LOCK = threading.Lock()
_LOGGER = logging.getLogger(__name__)


def backend_url() -> str:
    value = os.environ.get("HERMES_BACKEND_URL", DEFAULT_URL)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Invalid configured backend URL")
    try:
        _ = parsed.port
    except ValueError:
        raise ValueError("Invalid configured backend URL") from None
    return value.rstrip("/")


def ws_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Invalid configured backend URL")
    netloc = parsed.netloc
    return urlunsplit(("wss" if parsed.scheme == "https" else "ws", netloc, parsed.path.rstrip("/") + "/api/ws", "", ""))


def _clean_registry() -> None:
    now = time.monotonic()
    with _REGISTRY_LOCK:
        for key, (client, expires) in list(_REGISTRY.items()):
            if expires <= now:
                del _REGISTRY[key]
                client.close()


def session_for(key: str | None) -> requests.Session | None:
    _clean_registry()
    with _REGISTRY_LOCK:
        found = _REGISTRY.get(key) if key else None
        if not found:
            return None
        _REGISTRY[key] = (found[0], time.monotonic() + SESSION_TTL)
        return found[0]


def _request(response: requests.Response) -> requests.Response:
    if 300 <= getattr(response, "status_code", 200) < 400:
        raise RuntimeError("Backend returned an unexpected redirect")
    response.raise_for_status()
    return response


def api_get(base_url: str, path: str, client: requests.Session) -> Any:
    response = _request(client.get(base_url.rstrip("/") + path, timeout=15, allow_redirects=False))
    return response.json()


def providers(base_url: str) -> list[dict[str, Any]]:
    response = _request(requests.get(base_url.rstrip("/") + "/api/auth/providers", timeout=15, allow_redirects=False))
    data = response.json()
    return [p for p in data.get("providers", []) if isinstance(p, dict) and p.get("supports_password")]


def password_login(base_url: str, provider: str, username: str, password: str) -> str:
    client = requests.Session()
    try:
        response = _request(client.post(base_url.rstrip("/") + "/auth/password-login", json={"provider": provider, "username": username, "password": password}, timeout=15, allow_redirects=False))
        api_get(base_url, "/api/auth/me", client)
        key = secrets.token_urlsafe(32)
        _clean_registry()
        with _REGISTRY_LOCK:
            while len(_REGISTRY) >= REGISTRY_LIMIT:
                old, (removed, _) = _REGISTRY.popitem()
                removed.close()
            _REGISTRY[key] = (client, time.monotonic() + SESSION_TTL)
        return key
    except Exception:
        client.close()
        raise


def logout(base_url: str, key: str | None) -> None:
    client = session_for(key)
    try:
        if client:
            try:
                client.post(base_url.rstrip("/") + "/auth/logout", timeout=15, allow_redirects=False)
            except requests.RequestException:
                pass
    finally:
        with _REGISTRY_LOCK:
            removed = _REGISTRY.pop(key, None) if key else None
        if removed:
            removed[0].cookies.clear()
            removed[0].close()


def event_of(frame: Any) -> tuple[str, dict[str, Any]] | None:
    if isinstance(frame, str):
        try: frame = json.loads(frame)
        except json.JSONDecodeError: return None
    if not isinstance(frame, dict): return None
    if frame.get("method") == "event":
        params = frame.get("params", {})
        if isinstance(params, dict) and isinstance(params.get("type"), str):
            payload = params.get("payload", {})
            payload = payload.copy() if isinstance(payload, dict) else {"text": payload}
            if isinstance(params.get("session_id"), str): payload["_session_id"] = params["session_id"]
            return params["type"], payload
    if frame.get("type") == "event" and isinstance(frame.get("event"), str):
        payload = frame.get("payload", {})
        payload = payload.copy() if isinstance(payload, dict) else {"text": payload}
        if isinstance(frame.get("admission_id"), str): payload["_admission_id"] = frame["admission_id"]
        return frame["event"], payload
    if frame.get("type") in {"message.delta", "message.complete", "error"}:
        payload = frame.get("payload", frame)
        payload = payload.copy() if isinstance(payload, dict) else {"text": payload}
        if isinstance(frame.get("admission_id"), str): payload["_admission_id"] = frame["admission_id"]
        return frame["type"], payload
    return None


def rpc(sock: Any, method: str, params: dict[str, Any], callback=None) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    sock.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}))
    while True:
        frame = json.loads(sock.recv())
        if not isinstance(frame, dict): continue
        if frame.get("id") == request_id:
            if "error" in frame: raise RuntimeError("Gateway RPC failed")
            return frame.get("result", {})
        event = event_of(frame)
        if event and callback:
            callback(*event)
            if event[0] == "error" or event[1].get("status") == "error": raise RuntimeError("Model request failed")


def chat(base_url: str, client: requests.Session, prompt: str, session_id: str | None, callback, profile: str | None = None) -> tuple[str, dict[str, Any] | None, str | None]:
    response = _request(client.post(base_url.rstrip("/") + "/api/auth/ws-ticket", timeout=15, allow_redirects=False))
    ticket = response.json()["ticket"]
    sock = websocket.create_connection(ws_url(base_url), subprotocols=[PROTOCOL, f"hermes-gateway-ticket.{ticket}"], timeout=60)
    try:
        if session_id:
            result = rpc(sock, "session.resume", {"session_id": session_id, **({"profile": profile} if profile else {})})
            stored, runtime_id = session_id, result.get("session_id")
            if not runtime_id: raise RuntimeError("Gateway session.resume returned no session_id")
        else:
            result = rpc(sock, "session.create", {"source": "gui", **({"profile": profile} if profile else {})})
            stored, runtime_id = result.get("stored_session_id"), result.get("session_id")
            if not stored or not runtime_id: raise RuntimeError("Gateway session.create returned no session identifiers")
        done, usage = False, None
        receipt_missing = False
        user_row_id = None
        pending_events = []
        def receive(kind, payload):
            nonlocal done, usage, receipt_missing, pending_events
            if payload.get("_session_id") != runtime_id:
                return
            if kind != "message.complete":
                pending_events.append((kind, payload))
                return
            persisted = payload.get("persisted_turn")
            row_id = persisted.get("user_row_id") if isinstance(persisted, dict) else None
            if row_id is None:
                receipt_missing = True
                return
            if user_row_id is None:
                pending_events.append((kind, payload))
                return
            if row_id != user_row_id:
                pending_events.clear()
                return
            for buffered_kind, buffered_payload in pending_events:
                callback(buffered_kind, buffered_payload)
            pending_events.clear()
            done, usage = True, payload.get("usage")
            callback(kind, payload)
        admission = rpc(sock, "prompt.submit", {"session_id": runtime_id, "text": prompt}, receive)
        status = admission.get("status")
        if status not in {"streaming", "queued", "steered", "redirected"}:
            raise RuntimeError("Prompt submission returned an unsupported status")
        if status != "streaming":
            return stored, None, "accepted-pending"
        user_row_id = admission.get("user_row_id")
        if user_row_id is None:
            return stored, None, "accepted-pending"
        buffered_events = pending_events
        pending_events = []
        for buffered_event in buffered_events:
            receive(*buffered_event)
            if done or receipt_missing:
                break
        if receipt_missing:
            return stored, None, "accepted-pending"
        while not done:
            try:
                event = event_of(sock.recv())
            except (websocket.WebSocketTimeoutException, TimeoutError):
                return stored, None, "accepted-pending"
            if event:
                receive(*event)
                if receipt_missing:
                    return stored, None, "accepted-pending"
                if event[0] == "error" or event[1].get("status") == "error": raise RuntimeError("Model request failed")
        return stored, usage, None
    finally:
        sock.close()


def _json_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _workflow_turn(prompt: str, events: list[tuple[str, dict[str, Any]]], answer: str, usage: Any = None, failed: bool = False):
    from dash import html
    tools: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    thinking_count = 0
    public_summaries: list[str] = []
    errors: list[str] = []
    for kind, payload in events:
        if kind in {"reasoning.delta", "thinking.delta"}: thinking_count += 1
        elif kind == "reasoning.available":
            thinking_count += 1
            text = payload.get("public_reasoning_summary") or payload.get("public_summary")
            if isinstance(text, str) and text.strip(): public_summaries.append(text)
        elif kind in {"tool.start", "tool.generating", "tool.complete"}:
            identity = str(payload.get("tool_call_id") or payload.get("tool_id") or payload.get("id") or f"anonymous-{uuid.uuid4().hex}")
            tool = tools.get(identity)
            if tool is None and not any(payload.get(k) for k in ("tool_call_id", "tool_id", "id")) and kind != "tool.start":
                # ID-less lifecycle events correlate to the earliest unfinished same-name start;
                # concurrent same-name calls remain inherently ambiguous.
                name_hint = payload.get("name") or payload.get("tool_name")
                tool = next((candidate for candidate in ordered if candidate["name"] == name_hint and not candidate["completed"]), None)
            if tool is None:
                tool = {"identity": identity, "name": payload.get("name") or payload.get("tool_name") or "Tool", "args": None, "result": None, "completed": False, "error": False}
                tools[identity] = tool; ordered.append(tool)
            tool["name"] = payload.get("name") or payload.get("tool_name") or tool["name"]
            if kind == "tool.start": tool["args"] = payload.get("args", payload.get("params"))
            if kind == "tool.complete": tool["result"] = payload.get("result_text", payload.get("result")); tool["completed"] = True; tool["error"] = bool(payload.get("is_error"))
        elif kind == "error" or payload.get("status") == "error" or "fail" in kind.lower():
            code = payload.get("code")
            safe_code = code if isinstance(code, str) and code.isascii() and code.isalnum() and len(code) <= 48 else None
            errors.append("Request error" + (f" · {safe_code}" if safe_code else ""))
    children = [html.Div([html.Div("You", className="block-label"), html.Div(prompt, className="block-body")], className="flow-block user-block")]
    if thinking_count or public_summaries:
        children.append(html.Details([html.Summary(f"Reasoning activity · {thinking_count} updates (private reasoning text omitted)"), *[html.Pre(text, className="code-block") for text in public_summaries]], className="flow-block reasoning-block"))
    for item in ordered:
        panes = [html.Div([html.Span("Arguments", className="minor-label"), html.Pre(_json_text(item["args"] if item["args"] is not None else {}), className="code-block")], className="tool-pane")]
        if item["completed"]: panes.append(html.Div([html.Span("Error" if item["error"] else "Result", className="minor-label"), html.Pre(_json_text(item["result"]), className="code-block")], className="tool-pane " + ("error-pane" if item["error"] else "success-pane")))
        children.append(html.Div([html.Div([html.Span("Tool call", className="block-label"), html.Strong(item["name"])], className="tool-heading"), *panes], className="flow-block tool-block"))
    children.extend(html.Div([html.Div("Error / failure", className="block-label"), html.Pre(error, className="code-block")], className="flow-block error-pane") for error in errors)
    children.append(html.Div([html.Div("Hermes", className="block-label"), html.Div(answer or ("Turn interrupted before a response was received." if failed else "No response text received."), className="block-body")], className="flow-block assistant-block"))
    if usage is not None: children.append(html.Div("Usage · " + _json_text(usage), className="usage-block"))
    return html.Section([html.Div("Workflow turn" + (" · interrupted" if failed else ""), className="turn-heading"), *children], className="turn-card" + (" turn-failed" if failed else ""))


def _historical_turns(rows: list[Any]):
    turns = []; current = None
    def finish():
        if current is None: return
        events = []
        for c in current["calls"]:
            if not isinstance(c, dict): continue
            fn = c.get("function")
            if isinstance(fn, str):
                try: fn = json.loads(fn)
                except (json.JSONDecodeError, TypeError): fn = {}
            if not isinstance(fn, dict): fn = {}
            events.append(("tool.start", {"tool_call_id": c.get("id") or c.get("tool_call_id") or c.get("tool_id"), "name": c.get("name") or fn.get("name"), "args": c.get("args", c.get("arguments", fn.get("arguments")))}))
        events.extend(("tool.complete", {"tool_call_id": r.get("tool_call_id") or r.get("tool_id") or r.get("id"), "name": r.get("name"), "result": r.get("content"), "is_error": r.get("is_error", False)}) for r in current["results"] if isinstance(r, dict))
        if current["reasoning_summary"]: events.append(("reasoning.available", {"public_reasoning_summary": current["reasoning_summary"]}))
        turns.append(_workflow_turn(current["prompt"], events, current["answer"], current["usage"]))
    for msg in rows:
        if not isinstance(msg, dict): continue
        role, content = msg.get("role", ""), msg.get("content", "")
        if isinstance(content, list): content = " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
        if role == "user":
            finish(); current = {"prompt": str(content), "answer": "", "calls": [], "results": [], "reasoning_summary": None, "usage": None}
        elif current is not None and role == "assistant":
            if content and str(content).strip(): current["answer"] = (current["answer"] + "\n" if current["answer"] else "") + str(content)
            calls = msg.get("tool_calls") or []
            if isinstance(calls, str):
                try: calls = json.loads(calls)
                except (json.JSONDecodeError, TypeError): calls = []
            if isinstance(calls, list): current["calls"].extend(c for c in calls if isinstance(c, dict))
            current["reasoning_summary"] = msg.get("public_reasoning_summary") or msg.get("public_summary") or current["reasoning_summary"]
            if msg.get("usage") is not None: current["usage"] = msg["usage"]
        elif current is not None and role == "tool": current["results"].append(msg)
    finish()
    return turns


def make_app():
    from dash import Dash, Input, Output, State, dcc, html, no_update
    app = Dash(__name__, suppress_callback_exceptions=True)
    app.title = "Hermes Chat"
    app.layout = html.Div([dcc.Location(id="url"), dcc.Store(id="credential", storage_type="memory"), html.Div(id="page")])

    def login_layout():
        try:
            items = providers(backend_url())
            options = [{"label": p.get("display_name", p["name"]), "value": p["name"]} for p in items]
            status = "" if items else "No password sign-in provider is available. Contact your administrator."
        except Exception:
            options = []
            status = "Unable to load sign-in providers. Check the backend connection or contact your administrator."
        return html.Main([html.H1("Hermes Chat"), dcc.Dropdown(id="provider", options=options, placeholder="Password provider"), dcc.Input(id="username", placeholder="Username"), dcc.Input(id="password", type="password", placeholder="Password"), html.Button("Sign in", id="login"), html.Div(status, id="login-status")], className="login-card")

    def chat_layout():
        return html.Div([html.Aside([html.Div("H", className="brand-mark"), html.H2("Hermes"), html.P("AI workbench", className="sidebar-caption"), html.Label("WORKSPACE", className="sidebar-label"), dcc.Dropdown(id="profile", placeholder="Choose profile"), html.Label("CONVERSATIONS", className="sidebar-label"), dcc.Dropdown(id="sessions", placeholder="New chat or resume session"), html.Div([html.Button("↻ Refresh", id="lists"), html.Button("＋ New chat", id="new-chat")], className="sidebar-actions"), html.Div(className="sidebar-spacer"), html.Button("Log out", id="logout", className="logout-button")], className="sidebar"), html.Main([html.Header([html.Div([html.Div("HERMES · WORKSPACE", className="eyebrow"), html.H1("Chat", className="page-title")]), html.Div("Ready", className="status-pill")], className="topbar"), html.P("Tool inputs and results may contain private data and are visible to you while signed in. Private reasoning text is omitted. Plain HTTP transmits session data unencrypted; use HTTPS outside trusted localhost.", className="privacy-notice"), html.Div(id="chat-status", className="status-message"), html.Div(id="usage", className="usage-block"), html.Div(id="transcript", className="history-list"), html.Div(id="workflow", className="workflow-list"), html.Div([html.Label("Message Hermes", htmlFor="prompt", className="prompt-label"), dcc.Textarea(id="prompt", placeholder="Message Hermes…", className="composer-input"), html.Div([html.Span("Enter a prompt to start a workflow", className="composer-hint"), html.Button("Send ↑", id="send", className="send-button")], className="composer-footer")], className="composer")], className="chat-main")], className="workbench")

    @app.callback(Output("page", "children"), Input("url", "pathname"), State("credential", "data"))
    def route(path, credential):
        return chat_layout() if path == "/chat" and session_for((credential or {}).get("id")) else login_layout()

    @app.callback(Output("credential", "data"), Output("url", "pathname"), Output("login-status", "children", allow_duplicate=True), Output("password", "value"), Input("login", "n_clicks"), State("provider", "value"), State("username", "value"), State("password", "value"), prevent_initial_call=True)
    def login(_n, provider, username, password):
        if not _n:
            return no_update, no_update, no_update, no_update
        try:
            if not all((provider, username, password)): raise ValueError
            return {"id": password_login(backend_url(), provider, username, password)}, "/chat", "", ""
        except Exception:
            return no_update, no_update, "Sign-in failed. Check your credentials and try again.", ""

    @app.callback(Output("credential", "data", allow_duplicate=True), Output("url", "pathname", allow_duplicate=True), Input("logout", "n_clicks"), State("credential", "data"), prevent_initial_call=True)
    def signout(n_clicks, credential):
        if not n_clicks:
            return no_update, no_update
        try: logout(backend_url(), (credential or {}).get("id"))
        except Exception: pass
        return None, "/login"

    @app.callback(Output("profile", "options"), Output("profile", "value"), Output("sessions", "options"), Output("sessions", "value"), Output("chat-status", "children"), Input("url", "pathname"), Input("lists", "n_clicks"), Input("profile", "value"), State("credential", "data"), State("sessions", "value"))
    def load_lists(path, _n, profile, credential, selected):
        if path != "/chat": return [], None, [], None, ""
        client = session_for((credential or {}).get("id"))
        if not client:
            return [], None, [], None, "Your sign-in session is unavailable. Please sign in again."
        stage = "profile list"
        try:
            base = backend_url()
            stage = "session validation"
            api_get(base, "/api/auth/me", client)
            stage = "profile list"
            data = api_get(base, "/api/profiles", client)
            profiles = _list_envelope(data, "profiles", "profile list")
            opts = [{"label": str(p.get("name", p.get("id", p))), "value": str(p.get("id", p.get("name", p)))} for p in profiles]
            chosen = profile if profile in [o["value"] for o in opts] else (opts[0]["value"] if opts else None)
            stage = "session list"
            data = api_get(base, "/api/sessions" + ("?" + urlencode({"profile": chosen}) if chosen else ""), client)
            sessions = _list_envelope(data, "sessions", "session list")
            so = [{"label": str(s.get("title") or s.get("id") or s.get("session_id")), "value": str(s.get("id", s.get("session_id")))} for s in sessions if isinstance(s, dict) and s.get("id", s.get("session_id")) is not None]
            from dash import ctx
            if ctx.triggered_id == "profile":
                _LOGGER.info("Loaded chat lists: profiles=%d sessions=%d", len(opts), len(so))
                return opts, chosen, so, None, ""
            _LOGGER.info("Loaded chat lists: profiles=%d sessions=%d", len(opts), len(so))
            return opts, chosen, so, selected if selected in [o["value"] for o in so] else None, ""
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            _LOGGER.warning("Unable to load chat lists: stage=%s category=http status=%s", stage, status)
            if status == 401:
                message = "Your sign-in session expired. Please sign in again."
            elif status == 403:
                message = "Not authorized to load chat lists."
            else:
                message = f"Unable to load {stage} (HTTP {status})."
        except (requests.ConnectionError, requests.Timeout):
            _LOGGER.warning("Unable to load chat lists: stage=%s category=connection", stage)
            message = "Unable to connect to the backend while loading chat lists. Check the backend connection."
        except RuntimeError:
            _LOGGER.warning("Unable to load chat lists: stage=%s category=redirect", stage)
            message = "The backend returned an unexpected redirect while loading chat lists."
        except Exception as exc:
            _LOGGER.warning("Unable to load chat lists: stage=%s exception=%s", stage, type(exc).__name__)
            message = f"Unable to load {stage} ({type(exc).__name__})."
        return [], None, [], None, message

    @app.callback(Output("transcript", "children"), Output("workflow", "children"), Output("usage", "children"), Input("sessions", "value"), State("credential", "data"), State("profile", "value"))
    def select_session(selected, credential, profile):
        if not selected: return [], [], ""
        client = session_for((credential or {}).get("id"))
        if not client: return [], [], "Please sign in again."
        try:
            path = f"/api/sessions/{quote(str(selected), safe='')}/messages"
            if profile:
                path += "?" + urlencode({"profile": profile})
            data = api_get(backend_url(), path, client)
            rows = data.get("messages", data.get("data", []))
            return [], _historical_turns(rows), ""
        except Exception: return [], [], "Unable to load this session's history."

    @app.callback(Output("transcript", "children", allow_duplicate=True), Output("workflow", "children", allow_duplicate=True), Output("usage", "children", allow_duplicate=True), Output("chat-status", "children", allow_duplicate=True), Output("sessions", "value", allow_duplicate=True), Input("send", "n_clicks"), State("credential", "data"), State("sessions", "value"), State("prompt", "value"), State("transcript", "children"), State("workflow", "children"), State("profile", "value"), prevent_initial_call=True)
    def send_message(_n, credential, selected, prompt, transcript, workflows, profile):
        client = session_for((credential or {}).get("id"))
        if not client or not prompt or not prompt.strip(): return no_update, no_update, no_update, "Enter a message after signing in.", no_update
        turns, chunks, events = list(workflows or []), [], []
        def receive(kind, payload):
            if kind == "message.delta" and isinstance(payload.get("text"), str): chunks.append(payload["text"])
            elif kind == "message.complete":
                content = payload.get("text", payload.get("content"))
                if isinstance(content, str): chunks[:] = [content]
                elif isinstance(content, list): chunks[:] = [block["text"] for block in content if isinstance(block, dict) and isinstance(block.get("text"), str)]
            elif kind in {"message.delta", "message.complete"}:
                return
            elif kind in {"reasoning.delta", "reasoning.available", "thinking.delta", "tool.start", "tool.generating", "tool.complete"} or kind == "error" or payload.get("status") == "error" or "fail" in kind.lower():
                events.append((kind, payload))
        try:
            stored, usage, submission_state = chat(backend_url(), client, prompt, selected, receive, profile)
            if submission_state == "accepted-pending":
                turns.append(_workflow_turn(prompt, events, "Prompt accepted; completion is pending.", failed=False))
                return [], turns, "", "Prompt accepted and pending; completion is not yet correlated to this request.", stored
            turns.append(_workflow_turn(prompt, events, "".join(chunks), usage))
            return [], turns, json.dumps(usage, ensure_ascii=False) if usage is not None else "", "", stored
        except Exception:
            turns.append(_workflow_turn(prompt, events, "", failed=True))
            return [], turns, "", "Chat failed. Please retry.", no_update

    @app.callback(Output("sessions", "value", allow_duplicate=True), Output("transcript", "children", allow_duplicate=True), Output("workflow", "children", allow_duplicate=True), Output("usage", "children", allow_duplicate=True), Input("new-chat", "n_clicks"), prevent_initial_call=True)
    def new_chat(_n): return None, [], [], ""

    app.index_string = """<!DOCTYPE html><html><head>{%metas%}<title>{%title%}</title>{%favicon%}{%css%}<style>
:root{color-scheme:dark;--bg:#0b1118;--panel:#121c27;--panel2:#172431;--line:#263746;--text:#edf4fa;--muted:#9aabba;--accent:#67d5bb;--red:#ff8b8b;--green:#78d8a5}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 65% -20%,#193440 0,transparent 42%),var(--bg);color:var(--text);font:15px/1.55 Inter,ui-sans-serif,system-ui,sans-serif}.login-card{max-width:440px;margin:10vh auto;padding:32px;border:1px solid var(--line);border-radius:18px;background:var(--panel);box-shadow:0 24px 80px #0006}.prompt-label{display:block;margin-bottom:8px;color:var(--muted);font-weight:600} :focus-visible{outline:3px solid var(--accent);outline-offset:3px}button:focus-visible,.Select-control:focus-visible{box-shadow:0 0 0 3px var(--accent)}.workbench{min-height:100vh;display:grid;grid-template-columns:270px minmax(0,1fr)}.sidebar{background:#0d151e;border-right:1px solid var(--line);padding:30px 20px;display:flex;flex-direction:column;gap:10px}.brand-mark{width:42px;height:42px;display:grid;place-items:center;border-radius:13px;background:#173a3a;color:var(--accent);font-weight:800;font-size:20px}.sidebar h2{margin:2px 0 0;font-size:20px}.sidebar-caption,.eyebrow,.sidebar-label,.minor-label,.block-label{color:var(--muted)}.sidebar-caption{margin:0 0 26px}.sidebar-label{font-size:10px;letter-spacing:.14em;font-weight:700;margin-top:14px}.sidebar-actions{display:grid;gap:8px;margin-top:12px}.sidebar-spacer{flex:1;min-height:30px}.chat-main{min-width:0;max-width:1120px;width:100%;margin:0 auto;padding:30px clamp(18px,5vw,70px);display:flex;flex-direction:column}.topbar{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:22px;margin-bottom:22px}.eyebrow{font-size:10px;letter-spacing:.16em}.page-title{font-size:25px;margin:3px 0 0}.status-pill{color:var(--accent);background:#15302e;padding:5px 12px;border-radius:20px;font-size:12px}.history-list,.workflow-list{display:grid;gap:15px}.workflow-list{margin-top:14px}.turn-card{border:1px solid var(--line);background:linear-gradient(135deg,#121d28,#101923);border-radius:16px;padding:18px;box-shadow:0 12px 36px #0002}.turn-heading{font-size:12px;font-weight:700;color:var(--muted);margin-bottom:14px}.flow-block{border:1px solid var(--line);border-radius:11px;padding:13px 15px;margin-top:9px;min-width:0}.user-block{background:#152533}.assistant-block{background:#14251f;border-color:#24483e}.reasoning-block{background:#211f32;border-color:#3a3552}.tool-block{background:#121c27}.block-label{font-size:11px;text-transform:uppercase;letter-spacing:.08em;font-weight:700;margin-bottom:5px}.block-body{white-space:pre-wrap;overflow-wrap:anywhere}.tool-heading{display:flex;align-items:center;gap:10px;margin-bottom:10px}.tool-heading strong{overflow-wrap:anywhere}.tool-pane{min-width:0;margin-top:8px}.code-block{background:#0a1118;color:#d9e6ee;border:1px solid #233341;border-radius:8px;padding:12px;margin:5px 0 0;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;max-height:360px;overflow:auto;font:12px/1.55 ui-monospace,SFMono-Regular,monospace}.success-pane .minor-label{color:var(--green)}.error-pane{border-color:#6b353b;background:#2a191e;color:#ffd6d6}.error-pane .minor-label,.error-pane .block-label{color:var(--red)}.turn-failed{border-color:#6b353b}.usage-block{margin:12px 3px 2px;color:var(--muted);font:11px ui-monospace,monospace;overflow-wrap:anywhere}.composer{position:sticky;bottom:12px;margin-top:auto;padding:12px;background:#121c27;border:1px solid #314352;border-radius:15px;box-shadow:0 15px 40px #0006}.composer-input{display:block;width:100%;height:86px;resize:vertical;border:0!important;background:transparent!important;padding:8px!important}.composer-footer{display:flex;align-items:center;justify-content:space-between;padding:5px 2px 0}.composer-hint{font-size:11px;color:var(--muted)}button,input,textarea{font:inherit;color:var(--text);background:#172431;border:1px solid #314352;border-radius:9px;padding:10px 12px}button{cursor:pointer;transition:.15s}button:hover{border-color:var(--accent);background:#1c3038}.send-button{background:#176454;color:#effffb;border:0;font-weight:700;padding:9px 18px}.logout-button{color:#ffb3b3}.status-message{color:#ffb3b3}.Select-control,.Select-menu-outer,.Select-menu,.Select-option,.Select-value-label,.Select-placeholder,.Select-input>input,.Select--single>.Select-control .Select-value{background:#14212d!important;color:var(--text)!important}.Select-control{border:1px solid #314352!important;border-radius:9px!important;min-height:42px}.Select-placeholder{color:#a8b7c4!important}.Select-option{background:#14212d!important;color:var(--text)!important}.Select-option.is-focused,.Select-option:hover{background:#20413f!important;color:#fff!important}.Select-menu-outer{background:#14212d!important;border:1px solid #314352!important;z-index:10}.sidebar .Select.is-focused:not(.is-open)>.Select-control{border-color:var(--accent)!important;box-shadow:0 0 0 1px var(--accent)!important}.Select-arrow{border-color:#a8b7c4 transparent transparent!important}.Select-clear{color:#a8b7c4!important}.status-message:empty{display:none}details summary{cursor:pointer;color:#d7caff;font-weight:600}#page{min-height:100vh}body:has(#login){padding:0}.login-card{max-width:480px;margin:10vh auto;padding:32px;background:var(--panel);border:1px solid var(--line);border-radius:16px}
@media(max-width:760px){.workbench{grid-template-columns:1fr}.sidebar{padding:16px;display:grid;grid-template-columns:auto 1fr;align-items:center;gap:8px}.sidebar .sidebar-caption,.sidebar .sidebar-label,.sidebar .sidebar-spacer{display:none}.logout-button{display:block;grid-column:1/-1}.sidebar-actions{grid-column:1/-1;display:flex}.sidebar-actions button{flex:1}.sidebar h2{margin:0}.sidebar .brand-mark{grid-row:span 1}.chat-main{padding:18px 14px}.topbar{margin-bottom:14px}.turn-card{padding:12px}.composer{bottom:5px}}
</style></head><body>{%app_entry%}<footer>{%config%}{%scripts%}{%renderer%}</footer></body></html>"""
    return app


def _list_envelope(data: Any, key: str, stage: str) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        raise TypeError(f"Invalid {stage} response")
    for candidate in (key, "data", "items"):
        if candidate in data:
            value = data[candidate]
            if not isinstance(value, list):
                raise TypeError(f"Invalid {stage} response")
            return value
    raise TypeError(f"Invalid {stage} response")


if __name__ == "__main__":
    make_app().run(host=os.environ.get("HERMES_DASH_HOST", "127.0.0.1"), port=8050, debug=False)
