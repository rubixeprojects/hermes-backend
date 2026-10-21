import json

import hermes_dash as app
import pytest


def test_url_event_and_fixed_backend(monkeypatch):
    assert app.ws_url("https://example.test:9443/base/") == "wss://example.test:9443/base/api/ws"
    assert app.event_of(json.dumps({"method": "event", "params": {"type": "thinking.delta", "payload": {"text": "x"}}})) == ("thinking.delta", {"text": "x"})
    monkeypatch.setenv("HERMES_BACKEND_URL", "https://trusted.test/base")
    assert app.backend_url() == "https://trusted.test/base"
    for value in ("http://user:pass@host", "http://host/path?q=1", "ftp://host"):
        monkeypatch.setenv("HERMES_BACKEND_URL", value)
        try:
            app.backend_url()
        except ValueError:
            pass
        else:
            raise AssertionError("invalid configured URL accepted")


def test_reject_redirects_and_do_not_follow(monkeypatch):
    seen = {}
    class Response:
        status_code = 302
        def raise_for_status(self): pass
    def get(url, **kwargs):
        seen.update(kwargs)
        return Response()
    monkeypatch.setattr(app.requests, "get", get)
    try:
        app.providers("http://backend")
    except RuntimeError as exc:
        assert "redirect" in str(exc)
    else:
        raise AssertionError("redirect accepted")
    assert seen["allow_redirects"] is False


def test_provider_bootstrap_contract_and_login_callback(monkeypatch):
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"providers": [
                {"name": "password", "display_name": "Password", "supports_password": True},
                {"name": "oauth", "supports_password": False},
            ]}
    seen = {}
    def get(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()
    monkeypatch.setattr(app.requests, "get", get)
    assert app.providers("http://backend") == [{"name": "password", "display_name": "Password", "supports_password": True}]
    assert seen["url"] == "http://backend/api/auth/providers" and seen["allow_redirects"] is False

    dash = app.make_app()
    monkeypatch.setattr(app, "password_login", lambda *args: "opaque-id")
    key = next(key for key in dash.callback_map if "login-status.children" in key and "credential.data" in key)
    login = dash.callback_map[key]["callback"].__wrapped__
    from dash import no_update
    calls = []
    monkeypatch.setattr(app, "password_login", lambda *args: calls.append(args) or "opaque-id")
    for clicks in (0, None):
        assert login(clicks, None, None, None) == (no_update, no_update, no_update, no_update)
    assert calls == []
    result = login(1, "password", "user", "secret")
    assert result == ({"id": "opaque-id"}, "/chat", "", "")
    assert calls == [(app.backend_url(), "password", "user", "secret")]


def test_login_failure_closes_temporary_client(monkeypatch):
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ok": True}
    class Client:
        def __init__(self): self.closed = False; self.posts = []
        def post(self, url, **kw): self.posts.append((url, kw)); return Response()
        def get(self, url, **kw): raise RuntimeError("invalid auth")
        def close(self): self.closed = True
    client = Client()
    monkeypatch.setattr(app.requests, "Session", lambda: client)
    try:
        app.password_login("http://backend", "local", "u", "secret")
    except RuntimeError:
        pass
    assert client.closed


def test_logout_callback_requires_genuine_click(monkeypatch):
    from dash import no_update

    dash = app.make_app()
    calls = []
    monkeypatch.setattr(app, "logout", lambda *args: calls.append(args))
    key = next(key for key, value in dash.callback_map.items() if any(item["id"] == "logout" for item in value["inputs"]))
    signout = dash.callback_map[key]["callback"].__wrapped__

    assert signout(0, {"id": "opaque-id"}) == (no_update, no_update)
    assert calls == []
    assert signout(1, {"id": "opaque-id"}) == (None, "/login")
    assert calls == [(app.backend_url(), "opaque-id")]


def test_registry_bounds_expiry_and_logout_cleanup(monkeypatch):
    class Client:
        def __init__(self): self.closed = False; self.cookies = type("Cookies", (), {"clear": lambda self: None})()
        def close(self): self.closed = True
        def post(self, *args, **kwargs): raise RuntimeError("backend unavailable")
    app._REGISTRY.clear()
    monkeypatch.setattr(app, "REGISTRY_LIMIT", 1)
    first, second = Client(), Client()
    app._REGISTRY.update({"a": (first, 0)})
    app._REGISTRY["b"] = (second, 9999999999.0)
    app._clean_registry()
    assert first.closed and "b" in app._REGISTRY
    app._REGISTRY["expired"] = (second, 0)
    app._clean_registry()
    assert second.closed


def test_layout_routes_and_unique_ids():
    dash = app.make_app()
    client = dash.server.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/_dash-layout").status_code == 200
    assert client.get("/_dash-dependencies").status_code == 200
    assert ".workbench" in dash.index_string and ".Select-option" in dash.index_string and "color-scheme:dark" in dash.index_string
    layout = client.get("/_dash-layout")
    assert layout.status_code == 200
    assert dash.config.suppress_callback_exceptions
    ids = []
    def walk(node):
        if isinstance(node, dict):
            props = node.get("props", {})
            if "id" in props: ids.append(props["id"])
            for value in node.values(): walk(value)
        elif isinstance(node, list):
            for value in node: walk(value)
    walk(layout.get_json())
    assert len(ids) == len(set(ids))
    assert "page.children" in dash.callback_map
    app._REGISTRY["layout-test"] = (app.requests.Session(), 9999999999.0)
    route = dash.callback_map["page.children"]["callback"].__wrapped__
    chat_layout = route("/chat", {"id": "layout-test"})
    chat_ids = []
    def walk_chat(node):
        if hasattr(node, "to_plotly_json"):
            node = node.to_plotly_json()
        if isinstance(node, dict):
            props = node.get("props", {})
            if "id" in props: chat_ids.append(props["id"])
            for value in node.values(): walk_chat(value)
        elif isinstance(node, (list, tuple)):
            for value in node: walk_chat(value)
    walk_chat(chat_layout)
    assert all(identifier in chat_ids for identifier in ("usage", "profile", "sessions", "workflow", "prompt", "send"))
    assert len(chat_ids) == len(set(chat_ids))
    app._REGISTRY.pop("layout-test", None)


def test_websocket_ticket_protocol_without_cookie_header(monkeypatch):
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ticket": "one-use"}
    class Client:
        def post(self, url, **kw): return Response()
    class Socket:
        def __init__(self): self.sent = []; self.frames = []; self.closed = False
        def send(self, data):
            req = json.loads(data); self.sent.append(req)
            result = {"session_id": "runtime"} if req["method"] == "session.resume" else {"status": "streaming", "user_row_id": 41}
            if req["method"] == "prompt.submit":
                self.frames.append(json.dumps({"method": "event", "params": {"type": "message.complete", "session_id": "runtime", "payload": {"persisted_turn": {"user_row_id": 41}, "text": "answer", "usage": {"input_tokens": 4}}}}))
            self.frames.append(json.dumps({"id": req["id"], "result": result}))
        def recv(self): return self.frames.pop(0)
        def close(self): self.closed = True
    sock = Socket(); captured = {}
    def connect(url, **kwargs): captured.update(kwargs); return sock
    monkeypatch.setattr(app.websocket, "create_connection", connect)
    result = app.chat("http://backend/base", Client(), "hello", "stored", lambda *a: None, "profile-a")
    assert result == ("stored", {"input_tokens": 4}, None)
    assert "cookie" not in captured
    assert captured["subprotocols"] == [app.PROTOCOL, "hermes-gateway-ticket.one-use"]
    assert sock.closed
    submit = next(item for item in sock.sent if item["method"] == "prompt.submit")
    assert submit["params"] == {"session_id": "runtime", "text": "hello"}


def test_fresh_session_create_uses_strict_params_and_returns_ids(monkeypatch):
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ticket": "one-use"}
    class Client:
        def post(self, *_args, **_kwargs): return Response()
    class Socket:
        def __init__(self): self.frames = []; self.sent = []
        def send(self, data):
            request = json.loads(data); self.sent.append(request)
            if request["method"] == "session.create":
                assert request["params"] == {"source": "gui", "profile": "profile-a"}
                self.frames.append(json.dumps({"id": request["id"], "result": {"stored_session_id": "stored", "session_id": "runtime"}}))
            elif request["method"] == "prompt.submit":
                assert request["params"] == {"session_id": "runtime", "text": "hello"}
                self.frames.append(json.dumps({"id": request["id"], "result": {"status": "queued"}}))
        def recv(self): return self.frames.pop(0)
        def close(self): pass
    sock = Socket()
    monkeypatch.setattr(app.websocket, "create_connection", lambda *_args, **_kwargs: sock)
    assert app.chat("http://backend", Client(), "hello", None, lambda *_args: None, "profile-a") == ("stored", None, "accepted-pending")
    create = next(item for item in sock.sent if item["method"] == "session.create")
    assert "request_id" not in create["params"]


def test_streaming_correlates_row_and_ignores_wrong_session_and_row(monkeypatch):
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ticket": "one-use"}
    class Client:
        def post(self, *_args, **_kwargs): return Response()
    class Socket:
        def __init__(self): self.frames = []; self.sent = []
        def send(self, data):
            request = json.loads(data); self.sent.append(request)
            if request["method"] == "session.resume":
                self.frames.append(json.dumps({"id": request["id"], "result": {"session_id": "runtime"}}))
            elif request["method"] == "prompt.submit":
                self.frames.extend([
                    json.dumps({"method": "event", "params": {"type": "message.complete", "session_id": "other-session", "payload": {"persisted_turn": {"user_row_id": 41}, "text": "wrong-session"}}}),
                    json.dumps({"method": "event", "params": {"type": "message.complete", "session_id": "runtime", "payload": {"persisted_turn": {"user_row_id": 99}, "text": "wrong-row"}}}),
                    json.dumps({"method": "event", "params": {"type": "message.complete", "payload": {"persisted_turn": {"user_row_id": 41}, "text": "unscoped"}}}),
                    json.dumps({"method": "event", "params": {"type": "message.complete", "session_id": "runtime", "payload": {"persisted_turn": {"user_row_id": 41}, "text": "right", "usage": {"output_tokens": 3}}}}),
                    json.dumps({"id": request["id"], "result": {"status": "streaming", "user_row_id": 41}}),
                ])
        def recv(self): return self.frames.pop(0)
        def close(self): pass
    sock = Socket()
    monkeypatch.setattr(app.websocket, "create_connection", lambda *_args, **_kwargs: sock)
    received = []
    assert app.chat("http://backend", Client(), "prompt", "stored", lambda *event: received.append(event)) == ("stored", {"output_tokens": 3}, None)
    sent = next(item for item in sock.sent if item["method"] == "prompt.submit")
    assert sent["params"] == {"session_id": "runtime", "text": "prompt"}
    assert [event[1].get("text") for event in received if event[0] == "message.complete"] == ["right"]


def test_streaming_without_row_id_is_accepted_pending_not_transport_failure(monkeypatch):
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ticket": "one-use"}
    class Client:
        def post(self, *_args, **_kwargs): return Response()
    class Socket:
        def __init__(self): self.frames = []
        def send(self, data):
            request = json.loads(data)
            result = {"session_id": "runtime"} if request["method"] == "session.resume" else {"status": "streaming"}
            self.frames.append(json.dumps({"id": request["id"], "result": result}))
        def recv(self): return self.frames.pop(0)
        def close(self): pass
    monkeypatch.setattr(app.websocket, "create_connection", lambda *_args, **_kwargs: Socket())
    assert app.chat("http://backend", Client(), "prompt", "stored", lambda *_args: None) == ("stored", None, "accepted-pending")


@pytest.mark.parametrize("receipt", [None, {"persisted_turn": {}}])
def test_same_session_completion_without_row_receipt_is_pending(monkeypatch, receipt):
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ticket": "one-use"}
    class Client:
        def post(self, *_args, **_kwargs): return Response()
    class Socket:
        def __init__(self): self.frames = []
        def send(self, data):
            request = json.loads(data)
            if request["method"] == "session.resume":
                self.frames.append(json.dumps({"id": request["id"], "result": {"session_id": "runtime"}}))
            elif request["method"] == "prompt.submit":
                payload = {"text": "not safely correlated", **(receipt or {})}
                self.frames.extend([
                    json.dumps({"method": "event", "params": {"type": "message.complete", "session_id": "runtime", "payload": payload}}),
                    json.dumps({"id": request["id"], "result": {"status": "streaming", "user_row_id": 41}}),
                ])
        def recv(self): return self.frames.pop(0)
        def close(self): pass
    monkeypatch.setattr(app.websocket, "create_connection", lambda *_args, **_kwargs: Socket())
    received = []
    assert app.chat("http://backend", Client(), "prompt", "stored", lambda *event: received.append(event)) == ("stored", None, "accepted-pending")
    assert received == []


def test_streaming_timeout_after_admission_is_pending(monkeypatch):
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ticket": "one-use"}
    class Client:
        def post(self, *_args, **_kwargs): return Response()
    class Socket:
        def __init__(self): self.frames = []; self.submitted = False
        def send(self, data):
            request = json.loads(data)
            result = {"session_id": "runtime"} if request["method"] == "session.resume" else {"status": "streaming", "user_row_id": 41}
            self.frames.append(json.dumps({"id": request["id"], "result": result}))
            if request["method"] == "prompt.submit": self.submitted = True
        def recv(self):
            if self.frames: return self.frames.pop(0)
            assert self.submitted
            raise app.websocket.WebSocketTimeoutException("timeout")
        def close(self): pass
    monkeypatch.setattr(app.websocket, "create_connection", lambda *_args, **_kwargs: Socket())
    assert app.chat("http://backend", Client(), "prompt", "stored", lambda *_args: None) == ("stored", None, "accepted-pending")


def test_wrong_row_then_matching_row_completion_uses_matching_only(monkeypatch):
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ticket": "one-use"}
    class Client:
        def post(self, *_args, **_kwargs): return Response()
    class Socket:
        def __init__(self): self.frames = []
        def send(self, data):
            request = json.loads(data)
            if request["method"] == "session.resume":
                self.frames.append(json.dumps({"id": request["id"], "result": {"session_id": "runtime"}}))
            elif request["method"] == "prompt.submit":
                for row, text in ((99, "wrong-row"), (41, "matching")):
                    self.frames.append(json.dumps({"method": "event", "params": {"type": "message.complete", "session_id": "runtime", "payload": {"persisted_turn": {"user_row_id": row}, "text": text}}}))
                self.frames.append(json.dumps({"id": request["id"], "result": {"status": "streaming", "user_row_id": 41}}))
        def recv(self): return self.frames.pop(0)
        def close(self): pass
    monkeypatch.setattr(app.websocket, "create_connection", lambda *_args, **_kwargs: Socket())
    received = []
    assert app.chat("http://backend", Client(), "prompt", "stored", lambda *event: received.append(event)) == ("stored", None, None)
    assert [event[1]["text"] for event in received if event[0] == "message.complete"] == ["matching"]


def test_same_session_events_are_buffered_until_matching_receipt(monkeypatch):
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ticket": "one-use"}
    class Client:
        def post(self, *_args, **_kwargs): return Response()
    class Socket:
        def __init__(self): self.frames = []
        def send(self, data):
            request = json.loads(data)
            if request["method"] == "session.resume":
                self.frames.append(json.dumps({"id": request["id"], "result": {"session_id": "runtime"}}))
            elif request["method"] == "prompt.submit":
                def event(kind, payload):
                    return json.dumps({"method": "event", "params": {"type": kind, "session_id": "runtime", "payload": payload}})
                self.frames.extend([
                    event("message.delta", {"text": "old delta"}),
                    event("message.complete", {"persisted_turn": {"user_row_id": 99}, "text": "old completion"}),
                    event("message.delta", {"text": "current delta"}),
                    event("tool.start", {"name": "current tool"}),
                    event("message.complete", {"persisted_turn": {"user_row_id": 41}, "text": "answer"}),
                    json.dumps({"id": request["id"], "result": {"status": "streaming", "user_row_id": 41}}),
                ])
        def recv(self): return self.frames.pop(0)
        def close(self): pass
    monkeypatch.setattr(app.websocket, "create_connection", lambda *_args, **_kwargs: Socket())
    received = []
    assert app.chat("http://backend", Client(), "prompt", "stored", lambda *event: received.append(event)) == ("stored", None, None)
    assert [(kind, payload.get("text", payload.get("name"))) for kind, payload in received] == [
        ("message.delta", "current delta"),
        ("tool.start", "current tool"),
        ("message.complete", "answer"),
    ]


@pytest.mark.parametrize("status", ["queued", "steered", "redirected"])
def test_busy_prompt_is_accepted_pending_without_waiting_for_completion(monkeypatch, status):
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ticket": "one-use"}
    class Client:
        def post(self, *_args, **_kwargs): return Response()
    class Socket:
        def __init__(self): self.frames = []
        def send(self, data):
            request = json.loads(data)
            if request["method"] == "session.resume":
                self.frames.append(json.dumps({"id": request["id"], "result": {"session_id": "runtime"}}))
            elif request["method"] == "prompt.submit":
                self.frames.append(json.dumps({"id": request["id"], "result": {"status": status}}))
        def recv(self): return self.frames.pop(0)
        def close(self): pass
    sock = Socket()
    monkeypatch.setattr(app.websocket, "create_connection", lambda *_args, **_kwargs: sock)
    received = []
    assert app.chat("http://backend", Client(), "prompt", "stored", lambda *event: received.append(event)) == ("stored", None, "accepted-pending")


def test_prompt_rejects_unsupported_status(monkeypatch):
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ticket": "one-use"}
    class Client:
        def post(self, *_args, **_kwargs): return Response()
    class Socket:
        def __init__(self): self.frames = []
        def send(self, data):
            request = json.loads(data)
            result = {"session_id": "runtime"} if request["method"] == "session.resume" else {"status": "unknown"}
            self.frames.append(json.dumps({"id": request["id"], "result": result}))
        def recv(self): return self.frames.pop(0)
        def close(self): pass
    monkeypatch.setattr(app.websocket, "create_connection", lambda *_args, **_kwargs: Socket())
    with pytest.raises(RuntimeError, match="unsupported status"):
        app.chat("http://backend", Client(), "prompt", "stored", lambda *_args: None)


def test_rpc_errors_are_generic():
    class Socket:
        def send(self, data): self.id = json.loads(data)["id"]
        def recv(self): return json.dumps({"id": self.id, "error": {"message": "secret ticket"}})
    try: app.rpc(Socket(), "x", {})
    except RuntimeError as exc: assert str(exc) == "Gateway RPC failed" and "secret" not in str(exc)
    else: raise AssertionError("RPC error should raise")


@pytest.mark.parametrize("envelope", ["messages", "data"])
def test_session_history_accepts_response_envelopes_and_scopes_profile(monkeypatch, envelope):
    dash = app.make_app()
    app._REGISTRY["history-test"] = (object(), 9999999999.0)
    requested = []
    monkeypatch.setattr(app, "api_get", lambda base, path, client: requested.append(path) or {envelope: [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}]})
    callback = dash.callback_map["..transcript.children...workflow.children...usage.children.."]["callback"].__wrapped__
    result = callback("id /one?", {"id": "history-test"}, "profile /a")
    assert "hello" in str(result[1]) and "world" in str(result[1])
    assert requested == ["/api/sessions/id%20%2Fone%3F/messages?profile=profile+%2Fa"]
    app._REGISTRY.pop("history-test", None)


def test_session_history_prefers_messages_when_both_envelopes_exist(monkeypatch):
    dash = app.make_app()
    app._REGISTRY["history-test"] = (app.requests.Session(), 9999999999.0)
    monkeypatch.setattr(app, "api_get", lambda *args: {"messages": [{"role": "user", "content": "preferred"}], "data": [{"role": "user", "content": "fallback"}]})
    callback = dash.callback_map["..transcript.children...workflow.children...usage.children.."]["callback"].__wrapped__
    result = callback("session", {"id": "history-test"}, "default")
    assert "preferred" in str(result[1]) and "fallback" not in str(result[1])
    app._REGISTRY.pop("history-test", None)


def test_load_lists_reports_safe_diagnostics_and_clears_selections(monkeypatch, caplog):
    caplog.set_level("INFO", logger=app.__name__)
    dash = app.make_app()
    key = next(key for key in dash.callback_map if "profile.options" in key and "chat-status.children" in key)
    callback = dash.callback_map[key]["callback"].__wrapped__
    app._REGISTRY["lists-test"] = (app.requests.Session(), 9999999999.0)

    monkeypatch.setattr(app, "api_get", lambda _base, path, _client: {"data": [{"id": "profile-a"}]} if path == "/api/profiles" else {"data": [{"id": "session-a"}]})
    from contextvars import copy_context
    from dash._callback_context import context_value
    class CallbackContext:
        triggered_inputs = []
    context = copy_context()
    context.run(context_value.set, CallbackContext())
    assert context.run(callback, "/chat", 1, None, {"id": "lists-test"}, "old") == ([{"label": "profile-a", "value": "profile-a"}], "profile-a", [{"label": "session-a", "value": "session-a"}], None, "")
    assert "profiles=1 sessions=1" in caplog.text

    calls = []
    monkeypatch.setattr(app, "session_for", lambda key: calls.append(key))
    result = callback("/chat", 1, None, {"id": "OPAQUE-SECRET"}, "old")
    assert result[:4] == ([], None, [], None)
    assert "sign in again" in result[4] and calls == ["OPAQUE-SECRET"]

    monkeypatch.setattr(app, "session_for", lambda _key: object())
    for status in (401, 403, 500):
        error = app.requests.HTTPError("TOKEN-SECRET")
        error.response = type("Response", (), {"status_code": status, "text": "BODY-SECRET"})()
        monkeypatch.setattr(app, "api_get", lambda *_args, error=error: (_ for _ in ()).throw(error))
        result = callback("/chat", 1, None, {"id": "OPAQUE-SECRET"}, "old")
        assert result[:4] == ([], None, [], None)
        if status == 401:
            assert "sign in again" in result[4]
        elif status == 403:
            assert "not authorized" in result[4].lower()
            assert "expired" not in result[4].lower()
        else:
            assert "HTTP 500" in result[4]
        assert all(secret not in result[4] for secret in ("TOKEN-SECRET", "BODY-SECRET", "OPAQUE-SECRET"))
    assert all(secret not in caplog.text for secret in ("TOKEN-SECRET", "BODY-SECRET", "OPAQUE-SECRET"))

    monkeypatch.setattr(app, "api_get", lambda *_args: (_ for _ in ()).throw(app.requests.ConnectionError("COOKIE-SECRET")))
    result = callback("/chat", 1, None, {"id": "lists-test"}, "old")
    assert result[:4] == ([], None, [], None)
    assert "backend" in result[4] and "COOKIE-SECRET" not in result[4]
    assert "COOKIE-SECRET" not in caplog.text
    assert callback("/login", 1, None, None, None) == ([], None, [], None, "")
    app._REGISTRY.pop("lists-test", None)


def test_send_message_chat_status_is_duplicate_of_load_lists_output():
    dash = app.make_app()
    load_key = next(key for key in dash.callback_map if "profile.options" in key and "chat-status.children" in key)
    send_key = next(key for key in dash.callback_map if "workflow.children" in key and "chat-status.children" in key and "sessions.value" in key)
    load_output = next(output for output in dash.callback_map[load_key]["output"] if output.component_id == "chat-status")
    send_output = next(output for output in dash.callback_map[send_key]["output"] if output.component_id == "chat-status")
    assert not load_output.allow_duplicate
    assert send_output.allow_duplicate


def test_receive_ignores_unknown_events_and_keeps_known_workflow_nodes(monkeypatch):
    dash = app.make_app()
    key = next(key for key in dash.callback_map if "chat-status.children" in key and "workflow.children" in key)
    callback = dash.callback_map[key]["callback"].__wrapped__
    app._REGISTRY["flow-test"] = (app.requests.Session(), 9999999999.0)
    def fake_chat(_base, _client, _prompt, _selected, receive, _profile):
        for kind, payload in [
            ("session.updated", {"large": "irrelevant"}),
            ("reasoning.delta", {"text": "PRIVATE-THOUGHT"}),
            ("tool.start", {"name": "search"}),
            ("tool.complete", {"name": "search"}),
            ("error", {"message": "failed"}),
        ]:
            receive(kind, payload)
        return "stored", None
    monkeypatch.setattr(app, "chat", fake_chat)
    result = callback(1, {"id": "flow-test"}, None, "hello", [], [], "default")
    rendered = str(result[1])
    assert "session.updated" not in rendered and "irrelevant" not in rendered
    assert "PRIVATE-THOUGHT" not in rendered
    assert all(name in rendered for name in ("Reasoning activity", "search", "Arguments", "Error / failure"))
    app._REGISTRY.pop("flow-test", None)


def test_workflow_correlates_tools_preserves_content_usage_and_optional_reasoning():
    from dash import html
    events = [("tool.start", {"tool_call_id": "a", "name": "lookup", "args": {"q": "one"}}),
              ("tool.start", {"tool_id": "b", "tool_name": "write", "args": {"x": 2}}),
              ("tool.complete", {"tool_call_id": "b", "result": "saved"}),
              ("tool.complete", {"tool_id": "a", "result": {"rows": [1]}, "is_error": True})]
    rendered = str(app._workflow_turn("question", events, "answer", {"total_tokens": 9}))
    assert all(text in rendered for text in ("question", "lookup", '"q": "one"', "write", "saved", "rows", "Error", "answer", "total_tokens"))
    assert rendered.index("lookup") < rendered.index("write")
    assert "Reasoning / thinking" not in rendered


def test_workflow_rejects_untrusted_summaries_and_sanitizes_error_payloads():
    sentinel = "SENTINEL-PRIVATE-ERROR"
    rendered = str(app._workflow_turn("question", [
        ("reasoning.available", {"summary": "UNTRUSTED-GENERIC-SUMMARY", "reasoning_summary": "UNTRUSTED-REASONING-SUMMARY"}),
        ("reasoning.delta", {"text": "PRIVATE-THOUGHT-SENTINEL"}),
        ("error", {"message": sentinel, "details": {"body": sentinel}, "status": "error"}),
        ("tool.start", {"tool_call_id": "call", "name": "example", "args": {"q": "visible"}}),
        ("tool.complete", {"tool_call_id": "call", "result": sentinel, "is_error": True}),
    ], "answer"))
    assert "Reasoning activity · 2 updates (private reasoning text omitted)" in rendered
    assert all(text not in rendered for text in ("UNTRUSTED-GENERIC-SUMMARY", "UNTRUSTED-REASONING-SUMMARY", "PRIVATE-THOUGHT-SENTINEL"))
    assert "Request error" in rendered
    assert "message" not in rendered and "details" not in rendered
    assert sentinel in rendered  # Explicitly requested tool-complete results remain visible.


def test_history_renders_calls_results_and_omits_private_reasoning():
    rendered = str(app._historical_turns([
        {"role": "user", "content": "calculate"},
        {"role": "assistant", "content": "First part", "reasoning": "PRIVATE-REASONING", "tool_calls": '[{"id":"call-1","function":"{\\"name\\":\\"calc\\",\\"arguments\\":\\"{\\\\\\"n\\\\\\":2}\\"}"}]', "usage": {"output_tokens": 0}},
        {"role": "tool", "id": "call-1", "name": "calc", "content": "4"},
        {"role": "assistant", "content": "Four", "usage": {"output_tokens": 1}},
    ]))
    assert all(text in rendered for text in ("calculate", "calc", "Arguments", "4", "First part", "Four", "output_tokens"))
    assert "PRIVATE-REASONING" not in rendered


def test_idless_tool_starts_are_distinct_and_none_result_visible():
    rendered = str(app._workflow_turn("p", [("tool.start", {"name": "same"}), ("tool.start", {"name": "same"}), ("tool.complete", {"name": "same", "result": None})], "a"))
    assert rendered.count("Tool call") == 2 and "null" in rendered


def test_message_complete_content_fallback(monkeypatch):
    dash = app.make_app()
    key = next(key for key in dash.callback_map if "workflow.children" in key and "chat-status.children" in key and "sessions.value" in key)
    callback = dash.callback_map[key]["callback"].__wrapped__
    app._REGISTRY["content-test"] = (app.requests.Session(), 9999999999.0)
    def fake_chat(_base, _client, _prompt, _selected, receive, _profile):
        receive("message.complete", {"content": [{"type": "text", "text": "complete "}, {"type": "text", "text": "fallback"}]})
        return "stored", None, None
    monkeypatch.setattr(app, "chat", fake_chat)
    result = callback(1, {"id": "content-test"}, None, "q", [], [], "default")
    assert "complete fallback" in str(result[1])
    app._REGISTRY.pop("content-test", None)


def test_pending_callback_displays_accepted_prompt_in_workflow(monkeypatch):
    dash = app.make_app()
    key = next(key for key in dash.callback_map if "workflow.children" in key and "chat-status.children" in key and "sessions.value" in key)
    callback = dash.callback_map[key]["callback"].__wrapped__
    app._REGISTRY["pending-ui-test"] = (app.requests.Session(), 9999999999.0)
    monkeypatch.setattr(app, "chat", lambda *_args: ("stored", None, "accepted-pending"))
    result = callback(1, {"id": "pending-ui-test"}, None, "accepted prompt", [], [], "default")
    assert result[3] == "Prompt accepted and pending; completion is not yet correlated to this request."
    assert "accepted prompt" in str(result[1])
    assert "Workflow turn" in str(result[1])
    assert "Prompt accepted; completion is pending." in str(result[1])
    assert "No response text received" not in str(result[1])
    app._REGISTRY.pop("pending-ui-test", None)


def test_route_includes_provider_options_and_safe_status(monkeypatch):
    dash = app.make_app()
    calls = []
    monkeypatch.setattr(app, "providers", lambda base: calls.append(base) or [{"name": "local", "display_name": "Local", "supports_password": True}])
    route = dash.callback_map["page.children"]["callback"].__wrapped__
    def props_tree(component):
        serialized = component.to_plotly_json()
        result = [serialized]
        children = serialized["props"].get("children")
        for child in children if isinstance(children, (tuple, list)) else [children]:
            if hasattr(child, "to_plotly_json"):
                result.extend(props_tree(child))
        return result
    for path in ("/", "/login"):
        layout = route(path, None)
        components = props_tree(layout)
        dropdown = next(c["props"] for c in components if c["props"].get("id") == "provider")
        assert dropdown["options"] == [{"label": "Local", "value": "local"}]
    assert calls == [app.backend_url(), app.backend_url()]
    assert "page.children" in dash.callback_map
    assert not any("provider.options" in key for key in dash.callback_map)

    for result, expected in (([], "No password sign-in provider is available."), (RuntimeError("sensitive backend detail"), "Unable to load sign-in providers.")):
        def lookup(_base, result=result):
            if isinstance(result, Exception): raise result
            return result
        monkeypatch.setattr(app, "providers", lookup)
        components = props_tree(route("/", None))
        status = next(c["props"].get("children", "") for c in components if c["props"].get("id") == "login-status")
        assert expected in status
        assert "sensitive backend detail" not in status

    def forbidden(_base): raise AssertionError("chat route must not fetch providers")
    monkeypatch.setattr(app, "providers", forbidden)
    ids = []
    for component in props_tree(route("/chat", None)):
        if "id" in component["props"]: ids.append(component["props"]["id"])
    assert len(ids) == len(set(ids))
