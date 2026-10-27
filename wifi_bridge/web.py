import hmac
import hashlib
import json
import re
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import queue
import ssl
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .config import Invalid, password_matches
from .network import NetworkError
from .api import MobileAPI, ApiError
from .updates import Updates
from .secure_access import SecureAccess
from .push import PushService
from . import __version__
from .build import BUILD_ID
from .pwa import ASSETS, public_asset, revision


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    @property
    def sessions(self): return self.auth['sessions']

    @sessions.setter
    def sessions(self, value): self.auth['sessions'] = value

    @property
    def attempts(self): return self.auth['attempts']

    @attempts.setter
    def attempts(self, value): self.auth['attempts'] = value

    def __init__(self, address, controller, shared=None, capacity=16):
        self.controller = controller
        self.api = MobileAPI(controller)
        self.updates = shared.updates if shared else Updates(controller.store.path.parent, controller.demo)
        self.auth = shared.auth if shared else {'sessions': {}, 'attempts': {}}
        self.auth_lock = shared.auth_lock if shared else threading.Lock()
        self.slots = threading.BoundedSemaphore(capacity)
        self.secure_access = shared.secure_access if shared else SecureAccess(controller)
        self.owns_secure_access = shared is None
        super().__init__(address, Handler)
        if self.owns_secure_access: self.secure_access.initialize()
        self.push = shared.push if shared else PushService(controller, lambda: self.secure_access.status())

    def server_close(self):
        if self.owns_secure_access:
            self.push.close()
            self.secure_access.close()
        super().server_close()

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class SecureWebServer(WebServer):
    """Two handshake workers, sixteen queued sockets, six HTTPS request slots."""
    def __init__(self, address, http, context, hostname):
        self.context, self.hostname = context, hostname
        self.handshake_timeout = 3
        self.handshake_attempts = {}
        self.handshake_queue = queue.Queue(maxsize=16)
        self.handshake_stop = threading.Event()
        super().__init__(address, http.controller, shared=http, capacity=6)
        self.handshake_workers = [threading.Thread(target=self.handshake_worker, daemon=True) for _ in range(2)]
        for worker in self.handshake_workers: worker.start()

    def failed_handshake(self, address):
        now = time.monotonic()
        with self.auth_lock:
            attempts = {ip: value for ip, value in self.handshake_attempts.items() if now - value[0] < 60}
            since, count = attempts.get(address[0], (now, 0))
            if address[0] in attempts or len(attempts) < 512:
                attempts[address[0]] = (since, count + 1)
            self.handshake_attempts = attempts

    def process_request(self, request, client_address):
        now = time.monotonic()
        with self.auth_lock:
            self.handshake_attempts = {ip: value for ip, value in self.handshake_attempts.items() if now - value[0] < 60}
            failed = self.handshake_attempts.get(client_address[0], (now, 0))[1]
        # Only failed/slow handshakes are penalised. Successful polling/assets and
        # multiple tabs do not exhaust a per-minute connection allowance.
        if failed >= 10 or self.context is None:
            self.shutdown_request(request); return
        try: self.handshake_queue.put_nowait((request, client_address, now))
        except queue.Full: self.shutdown_request(request)

    def handshake_worker(self):
        while not self.handshake_stop.is_set():
            try: request, address, accepted = self.handshake_queue.get(timeout=.1)
            except queue.Empty: continue
            connection = request
            try:
                context = self.context
                remaining = self.handshake_timeout - (time.monotonic() - accepted)
                if context is None or remaining <= 0: raise TimeoutError
                request.settimeout(remaining)
                connection = context.wrap_socket(request, server_side=True, do_handshake_on_connect=False)
                connection.do_handshake()
                if self.context is None: raise OSError
            except (OSError, ValueError):
                self.failed_handshake(address)
                self.shutdown_request(connection)
            else:
                # Established requests have separate bounded slots/threads; they
                # cannot occupy the two handshake workers or HTTP recovery slots.
                WebServer.process_request(self, connection, address)
            finally: self.handshake_queue.task_done()

    def server_close(self):
        self.context = None; self.handshake_stop.set()
        while True:
            try: request, _, _ = self.handshake_queue.get_nowait()
            except queue.Empty: break
            self.shutdown_request(request); self.handshake_queue.task_done()
        super().server_close()
        for worker in self.handshake_workers: worker.join(timeout=self.handshake_timeout + .2)


class Handler(BaseHTTPRequestHandler):
    server_version = "WiFiBridge"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *args):
        pass

    @property
    def controller(self):
        return self.server.controller

    def reply(self, code, payload, content_type="application/json; charset=utf-8", cookie=None, shell_revision=None, certificate=False, profile=False):
        if self.native_path() and isinstance(payload, dict) and isinstance(payload.get("error"), str):
            codes = {400: "INVALID_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 429: "RATE_LIMITED"}
            payload = {"error": {"code": codes.get(code, "REQUEST_FAILED"), "message": payload["error"]}}
        body = json.dumps(payload, ensure_ascii=False).encode() if isinstance(payload, (dict, list)) else payload
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if profile:
            self.send_header("Content-Disposition", 'attachment; filename="astro-bridge.mobileconfig"')
        if certificate:
            self.send_header("Content-Disposition", 'attachment; filename="astro-bridge-ca.crt"')
        if shell_revision:
            self.send_header("X-Bridge-Shell", shell_revision)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("Connection", "close")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    @property
    def transport(self):
        # Only the actual accepted socket proves TLS. Proxy headers never do.
        return "https" if isinstance(self.connection, ssl.SSLSocket) else "http"

    @property
    def transport_identity(self):
        return getattr(self.connection.context, "bridge_identity", None) if self.transport == "https" else None

    def current_tls_identity(self):
        if self.transport != "https": return True
        current = getattr(getattr(self.server, "context", None), "bridge_identity", None)
        return current is not None and self.transport_identity == current

    @property
    def cookie_name(self):
        return "__Host-bridge_session" if self.transport == "https" else "bridge_session"

    def cookie(self, token, lifetime):
        return (self.cookie_name + "=" + token + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=" + str(lifetime)
                + ("; Secure" if self.transport == "https" else ""))

    @staticmethod
    def authority(value, scheme):
        if not value or len(value) > 255 or any(c.isspace() for c in value): return None
        try:
            parsed = urlsplit("//" + value)
            if parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
                return None
            host, port = parsed.hostname, parsed.port
            if not host or port == 0 or value.endswith(":"): return None
            return host.lower(), port if port is not None else (443 if scheme == "https" else 80)
        except ValueError: return None

    def valid_host(self):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1: return False
        authority = self.authority(hosts[0], self.transport)
        if authority is None or authority[1] != self.server.server_port: return False
        if self.transport == "https":
            allowed = {self.server.hostname, "192.168.77.1"}
        else:
            state = self.controller.status()
            allowed = {state["config"]["hostname"] + ".local", state["home_ip"], "192.168.77.1"}
            allowed |= set(getattr(self.controller, "management_hosts", ()))
            if self.controller.demo: allowed |= {"localhost", "127.0.0.1"}
        return authority[0] in allowed

    def same_origin(self):
        if not self.valid_host() or len(self.headers.get_all("Origin", [])) != 1: return False
        try:
            origin = urlsplit(self.headers["Origin"])
            return (origin.scheme == self.transport and not origin.path and not origin.query and not origin.fragment
                    and self.authority(origin.netloc, origin.scheme) == self.authority(self.headers["Host"], self.transport))
        except ValueError: return False

    def native_path(self):
        path = urlsplit(self.path).path
        return path == "/api/v1" or path.startswith("/api/v1/")

    def session(self, bearer=False):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            if bearer:
                authorization = self.headers.get("Authorization", "")
                token = authorization[7:] if authorization.startswith("Bearer ") else ""
            else:
                token = cookie[self.cookie_name].value if self.cookie_name in cookie else ""
        except Exception:
            return None
        with self.server.auth_lock:
            now = time.monotonic()
            self.server.sessions = {k: v for k, v in self.server.sessions.items() if v["expires"] > now}
            value = self.server.sessions.get(token)
            valid = (value and value.get("kind", "browser") == ("api" if bearer else "browser")
                    and value.get("transport", "http") == self.transport
                    and value.get("tls_identity") == self.transport_identity and self.current_tls_identity())
            live = {hashlib.sha256(v['csrf'].encode()).hexdigest() for v in self.server.sessions.values()}
        self.controller.operations.retain_clients(live)
        return value if valid else None

    def protection_action(self, action, ident, data, session, expected):
        p=self.controller.protection;client=hashlib.sha256(session['csrf'].encode()).hexdigest()
        with self.controller.lock:
            if action=='reserve':value=p.create(ident,data,client,expected,self.server.api.revision)
            elif action in ('renew','release','owner_release'):
                value=p.change(ident,data,client,release=action!='renew',owner=action=='owner_release',expected=expected,revision=self.server.api.revision)
            elif action=='owner_lock':value=p.owner_lock(ident,data,expected,self.server.api.revision)
            elif action=='recover_pauses':value=self.controller.pauses.recover(self.controller,data,expected,self.server.api.revision)
            else:raise ApiError(400,'INVALID_FIELDS','Choose a supported protection action.')
            return dict(value,config_revision=self.server.api.revision())

    def do_GET(self):
        if urlsplit(self.path).path == "/api/update-health" and self.client_address[0] in ("127.0.0.1", "::1"):
            state = self.controller.status()
            self.reply(200, {"version": __version__, "build_id": BUILD_ID, "mode": state["mode"],
                "management_ready": bool(state.get("management", {}).get("paths")),
                "telescopes_connected": any(p.get("connected") for p in state["telescopes"]),
                "update_safe": self.controller.update_safe()})
            return
        if self.native_path():
            self.native_request("GET")
            return
        if not self.valid_host():
            self.reply(421, {"error": "Use the bridge .local hostname or 192.168.77.1."})
            return
        path = urlsplit(self.path).path
        if path in ASSETS or path == "/sw.js":
            body, mime, current = public_asset(path)
            self.reply(200, body, mime, shell_revision=current)
            return
        session = self.session()
        if session is None:
            self.reply(401, {"error": "Sign in to the management interface."})
            return
        if path == "/api/status":
            with self.controller.lock:
                status = self.controller.status() | {"csrf": session["csrf"], "ui_revision": revision(),
                    "config_revision": self.server.api.revision(),
                    "event_context": hashlib.sha256(session["csrf"].encode()).hexdigest(),
                    "connection_protection": self.controller.protection.status(hashlib.sha256(session["csrf"].encode()).hexdigest())}
            self.reply(200, status)
        elif path == '/api/protection':
            self.reply(200,self.controller.protection.status(hashlib.sha256(session['csrf'].encode()).hexdigest()))
        elif path.startswith('/api/discovery/'):
            try:
                ident=path.removeprefix('/api/discovery/')
                if not re.fullmatch('[a-f0-9]{32}',ident):raise ApiError(404,'OBSERVATION_UNAVAILABLE','This network observation is no longer available. Scan again.')
                self.reply(200,self.controller.candidates.lookup(ident))
            except ApiError as error:self.reply(error.status,{'error':error.message,'code':error.code})
        elif path == "/api/plugins":
            self.reply(200,self.server.api.read('plugins'))
        elif path == "/api/push":
            self.reply(200, self.server.push.status())
        elif path in ("/api/https", "/api/https/certificate", "/api/https/profile"):
            try:
                if path.endswith("/profile"):
                    self.reply(200, self.server.secure_access.profile(), "application/x-apple-aspen-config", profile=True)
                elif path.endswith("/certificate"):
                    self.reply(200, self.server.secure_access.certificate(), "application/x-pem-file", certificate=True)
                else: self.reply(200, self.server.secure_access.status())
            except ApiError as error: self.reply(error.status, {"error": error.message, "code": error.code, **getattr(error, "details", {})})
        elif path == '/api/operations':
            try:
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                if set(query) != {'id'} or len(query['id']) != 1: raise ApiError(400, 'INVALID_OPERATION_ID', 'Provide one operation ID.')
                self.reply(200, self.controller.operations.read(query['id'][0]))
            except ApiError as error: self.reply(error.status, {'error':error.message, 'code':error.code})
        elif path == "/api/events":
            self.read_events()
        elif path == "/api/updates":
            self.reply(200, self.server.updates.status())
        elif path == "/api/scan":
            name = parse_qs(urlsplit(self.path).query).get("interface", [""])[0]
            try:
                self.reply(200, {"networks": self.controller.scan(name)})
            except (Invalid, NetworkError) as error:
                self.reply(409, {"error": str(error)})
        else:
            self.reply(404, {"error": "Unknown page."})

    def read_events(self):
        try:
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            if set(query) - {"after", "boot_id", "limit"} or any(len(values) != 1 for values in query.values()):
                raise ValueError("Invalid query")
            cursor_text = query.get("after", [None])[0]
            if cursor_text is not None and (not 1 <= len(cursor_text) <= 19 or not cursor_text.isascii() or not cursor_text.isdigit()):
                raise ValueError("Invalid cursor")
            after = int(cursor_text) if cursor_text is not None else None
            limit_text = query.get("limit", ["50"])[0]
            if not 1 <= len(limit_text) <= 3 or not limit_text.isascii() or not limit_text.isdigit():
                raise ValueError("Invalid limit")
            limit = int(limit_text)
            result = self.controller.event_store.read(after, query.get("boot_id", [None])[0], limit)
            self.reply(200, dict(result, api_version="1"))
        except (ValueError, TypeError):
            if self.native_path():
                raise ApiError(400, "INVALID_EVENT_CURSOR", "Invalid event cursor, boot ID or limit.") from None
            self.reply(400, {"error": "Invalid event cursor, boot ID or limit.", "code": "INVALID_EVENT_CURSOR"})

    def do_POST(self):
        if self.native_path():
            self.native_request("POST")
            return
        if not self.same_origin():
            self.reply(403, {"error": "Request origin refused."})
            return
        try:
            if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
                raise Invalid("JSON content is required.")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32768:
                raise Invalid("Request is empty or too large.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise Invalid("A JSON object is required.")
            path = urlsplit(self.path).path
            if path == "/api/login":
                self.login(data)
                return
            session = self.session()
            if session is None:
                self.reply(401, {"error": "Session expired. Sign in again."})
                return
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
                self.reply(403, {"error": "Request refused. Reload the page."})
                return
            if path == "/api/push":
                self.reply(200, self.server.push.action(data, self.headers.get("Origin", ""))); return
            if path == "/api/https":
                self.reply(202, self.server.secure_access.action(data)); return
            if path not in ("/api/logout", "/api/protection") and self.server.updates.busy():
                self.reply(409, {"error": "Interrupted update requires console recovery." if self.server.updates.recovery_required() else "An application update is in progress. Try again shortly."})
                return
            if path == '/api/protection':
                if not isinstance(data,dict):raise ApiError(400,'INVALID_FIELDS','Provide an object.')
                action=data.get('action');ident=data.get('id');revision=data.get('config_revision')
                if not isinstance(action,str) or not isinstance(ident,str) or revision is not None and not isinstance(revision,str):
                    raise ApiError(400,'INVALID_FIELDS','Provide a fixed action and resource ID.')
                body={k:v for k,v in data.items() if k not in ('action','id','config_revision')}
                self.reply(200,self.protection_action(action,ident,body,session,'"'+revision+'"' if revision is not None else None))
            elif path == '/api/telescopes/ensure':
                self.server.api.fields(data, {'id', 'config_revision'})
                if not isinstance(data.get('id'), str) or data.get('config_revision') is not None and not isinstance(data['config_revision'], str):
                    raise ApiError(400, 'INVALID_FIELDS', 'Provide a profile ID and current configuration revision.')
                code, value = self.controller.operations.ensure(data['id'], ('"'+data['config_revision']+'"') if data.get('config_revision') is not None else None, self.server.api.revision,
                    hashlib.sha256(session['csrf'].encode()).hexdigest(), session['expires'])
                self.reply(code, value)
            elif path == "/api/discovery":
                self.reply(200, self.controller.candidates.run(data))
            elif path == "/api/telescopes/binding":
                self.server.api.fields(data, {'id', 'action', 'config_revision', 'candidate_id', 'aliases', 'owner_confirmed', 'confirm_replace', 'conflicting_profile_ids', 'append_aliases', 'remove_bssid'})
                if data.get('action') not in ('bind', 'unlink') or not isinstance(data.get('id'), str):
                    raise ApiError(400, 'INVALID_FIELDS', 'Choose a profile and binding action.')
                revision = data.get('config_revision')
                if not isinstance(revision, str): raise ApiError(428, 'PRECONDITION_REQUIRED', 'Reload configuration before binding.')
                payload = {k: v for k, v in data.items() if k not in ('id', 'action', 'config_revision')}
                self.reply(200, self.server.api.binding(data['id'], payload, '"' + revision + '"', data['action'] == 'unlink'))
            elif path == "/api/config":
                result = self.controller.save(data, expected=('"'+data["config_revision"]+'"') if isinstance(data.get("config_revision"),str) else None, revision=self.server.api.revision)
                if result["sessions_invalidated"]:
                    self.server.push.revoke_all()
                    with self.server.auth_lock:
                        self.server.sessions.clear()
                    self.controller.operations.retain_clients(set())
                self.reply(200, result)
            elif path == "/api/updates":
                if self.controller.operations.pending: raise ApiError(409, 'CONNECTION_OPERATION_PENDING', 'A connection operation is pending.')
                if data.get('action') == 'install':
                    with self.controller.lock: self.controller.protection.reject()
                self.reply(202, self.server.updates.action(data))
            elif path == "/api/credentials":
                try:
                    result = self.controller.credentials(data)
                except NetworkError:
                    raise Invalid("The Linux password could not be changed. Check the wifi-bridge-password service.") from None
                if result["sessions_invalidated"]:
                    self.server.push.revoke_all()
                    with self.server.auth_lock:
                        self.server.sessions.clear()
                    self.controller.operations.retain_clients(set())
                self.reply(200, result)
            elif path == "/api/telescopes/action":
                self.reply(200, self.controller.telescope_action(data, expected=('"'+data["config_revision"]+'"') if isinstance(data.get("config_revision"),str) else None, revision=self.server.api.revision))
            elif path == "/api/reboot":
                self.controller.retry()
                self.reply(200, {"ok": True})
            elif path == "/api/logout":
                self.server.push.revoke_all()
                with self.server.auth_lock:
                    self.server.sessions.clear()
                self.controller.operations.retain_clients(set())
                self.reply(200, {"ok": True}, cookie=self.cookie("", 0))
            else:
                self.reply(404, {"error": "Unknown action."})
        except ApiError as error:
            self.reply(error.status, {"error": error.message, "code": error.code, **getattr(error, "details", {})})
        except (Invalid, json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError) as error:
            message = str(error) if isinstance(error, Invalid) else "Invalid request."
            self.reply(400, {"error": message})

    def login(self, data, native=False):
        if not self.current_tls_identity():
            self.reply(403, {"error": "TLS identity changed. Reconnect and authenticate again."}); return
        ip = self.client_address[0]
        now = time.monotonic()
        password = data.get("password", "")
        if not isinstance(password, str) or len(password) > 128:
            raise Invalid("Invalid password.")
        with self.server.auth_lock:
            attempts = self.server.attempts
            attempts = {k: v for k, v in attempts.items() if now - v[0] < 60}
            if len(attempts) >= 512:
                self.reply(429, {"error": "Too many attempts. Try again in a minute."})
                return
            since, count = attempts.get(ip, (now, 0))
            if count >= 5:
                self.reply(429, {"error": "Too many attempts. Try again in a minute."})
                return
            attempts[ip] = (since, count + 1)
            self.server.attempts = attempts
            encoded = self.controller.store.config["admin_hash"]
            valid = password_matches(password, encoded) if encoded else hmac.compare_digest(
                password.encode(), self.controller.bootstrap["password"].encode())
            if not valid:
                self.reply(401, {"error": "Incorrect password."})
                return
            attempts.pop(ip, None)
            self.server.sessions = {k: v for k, v in self.server.sessions.items() if v["expires"] > now}
            if len(self.server.sessions) >= 32:
                self.reply(429, {"error": "Too many active sessions."})
                return
            if not self.current_tls_identity():
                self.reply(403, {"error": "TLS identity changed. Reconnect and authenticate again."}); return
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.server.sessions[token] = {"csrf": csrf, "expires": now + 12 * 3600, "kind": "api" if native else "browser", "transport": self.transport, "tls_identity": self.transport_identity}
        if native:
            self.reply(200, {"access_token": token, "token_type": "Bearer", "expires_in": 43200, "api_version": "1"})
            return
        self.reply(200, {"csrf": csrf}, cookie=self.cookie(token, 43200))

    def do_PUT(self):
        if self.native_path():
            self.native_request("PUT")
        else:
            self.reply(405, {"error": "Method not allowed."})

    def do_PATCH(self):
        if self.native_path():
            self.native_request("PATCH")
        else:
            self.reply(405, {"error": "Method not allowed."})

    def do_DELETE(self):
        if self.native_path():
            self.native_request("DELETE")
        else:
            self.reply(405, {"error": "Method not allowed."})

    def native_body(self, method):
        length = int(self.headers.get("Content-Length", "0"))
        if self.headers.get("Transfer-Encoding") or length < 0 or length > 32768:
            raise ApiError(400, "INVALID_REQUEST", "Invalid request framing or body size.")
        if method == "DELETE" and length == 0:
            return {}
        if self.headers.get_content_type() != "application/json" or not length:
            raise ApiError(400, "INVALID_REQUEST", "A JSON object is required.")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ApiError(400, "INVALID_REQUEST", "A JSON object is required.")
        return data

    def native_request(self, method):
        try:
            if not self.valid_host():
                raise ApiError(421, "INVALID_HOST", "Use the bridge's local hostname or management IP.")
            # Native clients omit Origin. Browser requests must remain same-origin;
            # no wildcard CORS, cookie authentication or CSRF exemption is added to UI routes.
            if self.headers.get("Origin") is not None and not self.same_origin():
                raise ApiError(403, "FORBIDDEN_ORIGIN", "Cross-origin browser requests are not allowed.")
            path = urlsplit(self.path).path[len(MobileAPI.PREFIX):]
            api = self.server.api
            if method == "GET" and path in ("/info", "/openapi.json"):
                self.reply(200, api.info() if path == "/info" else api.specification())
                return
            data = self.native_body(method) if method != "GET" else {}
            if path == "/auth/token" and method == "POST":
                api.fields(data, {"password"})
                self.login(data, native=True)
                return
            session = self.session(bearer=True)
            if session is None:
                raise ApiError(401, "UNAUTHORIZED", "A valid Bearer token is required.")
            if path == "/auth/token" and method == "DELETE":
                api.fields(data, set())
                with self.server.auth_lock:
                    self.server.sessions.pop(self.headers["Authorization"][7:], None)
                self.reply(200, {"ok": True})
                return
            if path == "/auth/revoke-all" and method == "POST":
                api.fields(data, set())
                self.server.push.revoke_all()
                with self.server.auth_lock:
                    self.server.sessions.clear()
                self.controller.operations.retain_clients(set())
                self.reply(200, {"ok": True})
                return
            if method != "GET" and not path.startswith("/leases/") and self.server.updates.busy():
                raise ApiError(409, "UPDATE_RECOVERY_REQUIRED" if self.server.updates.recovery_required() else "UPDATE_IN_PROGRESS", "Interrupted update requires console recovery." if self.server.updates.recovery_required() else "An application update is in progress. Try again shortly.")
            if path == "/system/https" and method in ("GET", "POST"):
                value = self.server.secure_access.status() if method == "GET" else self.server.secure_access.action(data)
                self.reply(200 if method == "GET" else 202, dict(value, api_version="1")); return
            if path == "/system/https/certificate" and method == "GET":
                self.reply(200, self.server.secure_access.certificate(), "application/x-pem-file", certificate=True); return
            parts = path.strip("/").split("/")
            if path=='/protection' and method=='GET':
                self.reply(200,dict(self.controller.protection.status(hashlib.sha256(session['csrf'].encode()).hexdigest()),api_version='1'));return
            if path=='/activity' and method=='GET':
                self.reply(200,dict(self.controller.status()['connection_activity'],api_version='1'));return
            if path=='/system/connection-pauses/recover' and method=='POST':
                self.reply(200,dict(self.controller.pauses.recover(self.controller,data,self.headers.get('If-Match'),api.revision),api_version='1'));return
            protection_action=None
            if len(parts)==3 and parts[0]=='telescopes':
                if parts[2]=='leases' and method=='POST':protection_action='reserve'
                elif parts[2]=='owner-lock' and method=='PATCH':protection_action='owner_lock'
            elif len(parts)==2 and parts[0]=='leases' and method in ('PATCH','DELETE'):
                protection_action='renew' if method=='PATCH' else 'release'
            elif len(parts)==3 and parts[0]=='leases' and parts[2]=='release' and method=='POST':protection_action='owner_release'
            if protection_action:
                self.reply(200,dict(self.protection_action(protection_action,parts[1],data,session,self.headers.get('If-Match')),api_version='1'));return
            if len(parts) == 2 and parts[0] == 'operations' and method == 'GET':
                self.reply(200, dict(self.controller.operations.read(parts[1]), api_version='1')); return
            if len(parts) == 3 and parts[0] == 'telescopes' and parts[2] == 'ensure' and method == 'POST':
                api.fields(data, set())
                code, value = self.controller.operations.ensure(parts[1], self.headers.get('If-Match'), api.revision,
                    hashlib.sha256(session['csrf'].encode()).hexdigest(), session['expires'])
                self.reply(code, dict(value, api_version='1')); return
            if path.startswith('/discovery/') and method=='GET':
                ident=path.removeprefix('/discovery/')
                if not re.fullmatch('[a-f0-9]{32}',ident):raise ApiError(404,'OBSERVATION_UNAVAILABLE','This network observation is no longer available. Scan again.')
                self.reply(200,dict(self.controller.candidates.lookup(ident),api_version='1'));return
            if path == "/discovery" and method == "POST":
                self.reply(200, dict(self.controller.candidates.run(data), api_version='1')); return
            if len(parts) == 3 and parts[0] == 'telescopes' and parts[2] == 'binding' and method in ('GET', 'POST', 'DELETE'):
                if method == 'GET': self.reply(200, api.read('telescopes', parts[1]))
                else: self.reply(200, api.binding(parts[1], data, self.headers.get('If-Match'), method == 'DELETE'))
                return
            if path == "/events" and method == "GET":
                self.read_events()
                return
            if path == "/wifi/networks" and method == "GET":
                name = parse_qs(urlsplit(self.path).query).get("interface", [""])[0]
                if name not in {d["name"] for d in self.controller.status()["devices"]}:
                    raise ApiError(400, "INVALID_INTERFACE", "Choose a present interface name from GET /interfaces.")
                try:
                    networks = self.controller.scan(name)
                except (Invalid, NetworkError):
                    raise ApiError(409, "SCAN_UNAVAILABLE", "Radio unavailable or busy. Retry after the current association.") from None
                self.reply(200, {"networks": networks, "api_version": "1"})
                return
            if path == "/system/updates" and method in ("GET", "POST"):
                if method == 'POST' and self.controller.operations.pending: raise ApiError(409, 'CONNECTION_OPERATION_PENDING', 'A connection operation is pending.')
                if method == 'POST' and data.get('action') == 'install':
                    with self.controller.lock: self.controller.protection.reject()
                self.reply(200 if method == "GET" else 202, self.server.updates.status() if method == "GET" else self.server.updates.action(data))
                return
            resources = {"/status": "status", "/config": "config", "/interfaces": "interfaces",
                         "/home": "home", "/hotspot": "hotspot", "/settings": "settings", "/telescopes": "telescopes", "/plugins":"plugins"}
            if path in resources and method == "GET":
                self.reply(200, api.read(resources[path], client=hashlib.sha256(session["csrf"].encode()).hexdigest()))
                return
            if path in ("/home", "/hotspot", "/settings") and method == "PATCH":
                self.reply(200, api.update(resources[path], data, expected=self.headers.get("If-Match")))
                return
            if path == "/telescopes" and method == "POST":
                self.reply(201, api.update("telescopes", data, create=True, expected=self.headers.get("If-Match")))
                return
            parts = path.strip("/").split("/")
            if len(parts) in (2, 3) and parts[0] == "telescopes":
                ident = parts[1]
                if len(parts) == 2:
                    if method == "GET":
                        self.reply(200, api.read("telescopes", ident)); return
                    if method == "PATCH":
                        self.reply(200, api.update("telescopes", data, ident, expected=self.headers.get("If-Match"))); return
                    if method == "DELETE":
                        api.fields(data, set())
                        self.reply(200, api.action(ident, "delete", self.headers.get("If-Match"))); return
                elif parts[2] in ("connect", "disconnect", "binding", "ensure") and method == "POST":
                    self.reply(202, api.action(ident, parts[2], self.headers.get("If-Match"), data)); return
            if path == "/credentials" and method == "POST":
                api.fields(data, {"target", "current_password", "password"})
                result = self.controller.credentials(data)
                if result["sessions_invalidated"]:
                    self.server.push.revoke_all()
                    with self.server.auth_lock:
                        self.server.sessions.clear()
                    self.controller.operations.retain_clients(set())
                self.reply(200, result); return
            if path == "/system/reboot" and method == "POST":
                api.fields(data, set())
                self.controller.retry()
                self.reply(202, {"ok": True, "reboot_in": 4}); return
            known = path in resources or path in ("/auth/token", "/auth/revoke-all", "/credentials", "/system/reboot", "/system/updates", "/wifi/networks", "/plugins", "/events", "/system/https", "/system/https/certificate", "/discovery")
            known = known or path in ('/protection','/activity') or parts[0] == 'leases' and len(parts) in (2,3)
            known = known or parts[0] == 'operations' and len(parts) == 2
            known = known or parts[0] == "telescopes" and (len(parts) == 2 or len(parts) == 3 and parts[2] in ("connect", "disconnect", "binding", "ensure", "leases", "owner-lock"))
            raise ApiError(405 if known else 404, "METHOD_NOT_ALLOWED" if known else "NOT_FOUND", "Unsupported API route or method.")
        except ApiError as error:
            self.reply(error.status, {"error": {"code": error.code, "message": error.message, **getattr(error, "details", {})}})
        except Invalid as error:
            self.reply(400, {"error": {"code": "VALIDATION_ERROR", "message": str(error)}})
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
            self.reply(400, {"error": {"code": "INVALID_REQUEST", "message": "Invalid JSON request."}})
        except NetworkError:
            self.reply(409, {"error": {"code": "NETWORK_UNAVAILABLE", "message": "Network operation failed. Check appliance status."}})
        except Exception as error:
            print("wifi-bridge API: " + type(error).__name__, flush=True)
            self.reply(500, {"error": {"code": "INTERNAL_ERROR", "message": "Internal API error."}})
