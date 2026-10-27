"""Reconcile independent home, telescope and optional access-point radios."""
import copy
import hmac
import ipaddress
import threading
import time
from collections import deque

from .config import Invalid, profiles, public_config, role_for, validate_config, password_hash, password_matches, text, network_intent, auto_policy, hotspot_ssid, reserved_hotspot_mac
from .network import NetworkError, hotspot_route_targets
from .plugins import relay_plugins, proxy_flag, plugin, require_config_plugins
from .events import EventStore, ConnectionEvents
from .tls import CertificateState
from .api import ApiError


class Controller:
    def __init__(self, store, network, bootstrap, demo=False):
        require_config_plugins(store.config)
        self.store, self.net, self.bootstrap, self.demo = store, network, bootstrap, demo
        self.lock = threading.RLock()
        self.net_lock = threading.Lock()
        self.stop = threading.Event()
        self.state = {"mode": "starting", "message": "Starting the bridge…", "devices": [],
                      "home_ip": "", "telescope_connected": False, "relay_ready": False,
                      "setup_ssid": "", "demo": demo, "last_error": "", "telescopes": [],
                      "hotspot": {"active": False, "reason": "starting"},
                      "management": {"mode":"none","paths":[],"reason":"STARTING","home_retry":{"reason":"STARTING","remaining_seconds":0}}}
        self.action_at = None
        self.configured_this_boot = False
        self.last_home_attempt = -1000
        self.next_home_attempt = 0
        self.attempts = {}
        self.applied = {}
        self.managed_roles = {"telescope"}
        self.country = None
        self.pending_deletions = set()
        self.blocked = {}
        self.topology = None
        self.password_attempts = deque()
        self.default_hash_cache = (None, False)
        self.ap_password = None
        self.routing_conflicts = set()
        self.proxy_owners = {proxy_flag(p['id']):None for p in relay_plugins()}
        self.discovery_plan = None
        self.management_plan = None
        self.management_plans = ()
        self.discovery_plans = ()
        self.cleanup_required = {}
        from .paused import PausedRoles
        self.pauses = PausedRoles(store.path.parent)
        self.certificates = CertificateState(store.path.parent)
        self.discovery_pending = False
        self.certificate_job_pending = False
        from .candidates import CandidateDiscovery
        self.candidates = CandidateDiscovery(self)

        self.event_store = EventStore(store.path.parent / "events.json")
        self.connection_events = ConnectionEvents(self.event_store)
        self._event_config = None
        self._event_contexts = {}
        self.last_cycle_ok = False
        from .operations import ConnectionOperations
        self.operations = ConnectionOperations(self)
        from .plugin_runtime import adapters
        self.plugin_adapters=adapters(self)
        # Stable aliases for existing internal/native integrations.
        self.vespera_demand=self.plugin_adapters['vespera']
        self.dwarf_demand=self.plugin_adapters['dwarf']
        self.zwo_mount_demand=self.plugin_adapters['zwo_mount']
        self.synscan_demand=self.plugin_adapters['synscan']
        from .protection import ConnectionProtection
        self.protection = ConnectionProtection(self)

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def status(self):
        with self.lock:
            result = copy.deepcopy(self.state)
            result["config"] = public_config(self.store.config)
            result["nearby_telescopes"] = self.candidates.nearby()
            result['connection_operation'] = self.operations.latest()
            result['maker_demand'] = self.operations.demand_status()
            result['maker_demands'] = {'seestar':dict(result['maker_demand']),**{kind:adapter.status() for kind,adapter in self.plugin_adapters.items()}}
            result['connection_protection'] = self.protection.status()
            result['connection_pauses'] = self.pauses.status(self.store.config)
            result['connection_activity'] = self.net.activity_status() if hasattr(self.net, 'activity_status') else {'supported':False,'profiles':{},'reason':'SIMULATION_NOT_MEASURED','idle_selection_implemented':True}
        from .runtime import capabilities
        result["deployment"] = capabilities(self.demo)
        encoded = self.store.config["admin_hash"]
        if self.default_hash_cache[0] != encoded:
            self.default_hash_cache = (encoded, password_matches("12345678", encoded) if encoded else self.bootstrap["password"] == "12345678")
        result["default_credentials"] = {"web": self.default_hash_cache[1],
            "hotspot": self.store.config.get("hotspot_password", self.bootstrap["password"]) == "12345678",
            "linux": self.store.config.get("linux_password_default", self.bootstrap["password"] == "12345678")}
        access = result.setdefault('network_access', {'available':False,'reason':'NOT_RECONCILED'})
        mode = result['config']['connection_mode']
        if access.get('mode',mode) != mode or access.get('private_lan_allowed',False) != result['config']['hotspot_private_lan']:
            access.update(available=False,reason='CONFIGURATION_PENDING')
        access.update(mode=mode,configured=mode=='hotspot_shared',private_lan_allowed=result['config']['hotspot_private_lan'],internet_verified=False)
        result["url"] = "http://" + result["config"]["hostname"] + ".local"
        return result

    def update_safe(self):
        """App-owned maintenance gate; future reservations/locks extend this hook."""
        with self.lock:
            return (not self.protection.protected() and not self.pauses.error and not self.pauses.records and not self.cleanup_required and not self.operations.pending and not self.certificate_job_pending and not self.certificates.busy() and not self.stop.is_set() and self.action_at is None
                    and not self.pending_deletions and not self.net_lock.locked()
                    and not any(p['enabled'] for p in self.store.config['telescopes'])
                    and not any(p.get('connected') for p in self.state['telescopes']))

    def save(self, payload, cause="CONFIGURATION", expected=None, revision=None):
        try:
            with self.certificates.config_guard():
                return self._save_locked(payload, cause, expected, revision)
        except (OSError, ValueError) as error:
            if isinstance(error, Invalid): raise
            raise Invalid("Configuration or certificate maintenance is already active or unavailable.") from None

    def guard_management(self, config):
        ap_mac=self.state.get('hotspot',{}).get('mac') if self.state.get('hotspot',{}).get('active') else None
        reserved = {ap_mac, reserved_hotspot_mac(config)} - {None, ''}
        if any(p['enabled'] and p['mac'] in reserved for p in profiles(config)):
            raise ApiError(409,'MANAGEMENT_RADIO_RESERVED','Stop the hotspot explicitly before assigning its adapter to an instrument.')

    def _save_locked(self, payload, cause, expected=None, revision=None):
        from .runtime import capabilities
        legacy_reboot = "telescopes" not in payload and capabilities(self.demo)['host_reboot']
        with self.lock:
            if self.operations.pending:
                raise ApiError(409, 'CONNECTION_OPERATION_PENDING', 'A connection operation is pending.')
            if self.action_at is not None:
                raise Invalid("A reboot is already scheduled.")
            if not self.store.config["admin_hash"] and not payload.get("admin_password"):
                payload = dict(payload, admin_password=self.bootstrap["password"])
            previous = self.store.config
            value = validate_config(payload, previous, self.state["devices"])
            # Storing the effective bootstrap password as a salted hash is not a
            # password rotation. Compare credentials, not randomized hash bytes.
            admin = payload.get('admin_password', '')
            password_changed = bool(admin) and not (password_matches(admin, previous['admin_hash'])
                if previous['admin_hash'] else hmac.compare_digest(admin.encode(), self.bootstrap['password'].encode()))
            access_changed = any(value[key] != previous.get(key, default) for key, default in
                                 (('connection_mode', 'private'), ('hotspot_private_lan', False)))
            access_changed |= value['hotspot'].get('ssid', '') != previous.get('hotspot', {}).get('ssid', '')
            access_changed |= value['hotspot'].get('mac', '') != previous.get('hotspot', {}).get('mac', '')
            if access_changed:
                self.protection.precondition(expected, revision or (lambda: ''))
                self.protection.reject()
            if value['auto_policy'] != auto_policy(previous):
                if expected is None or expected in ('*','"*"') or revision is None:raise ApiError(428,'PRECONDITION_REQUIRED','Automatic policy changes require the actual current revision.')
                if expected!='"'+revision()+'"':raise ApiError(412,'PRECONDITION_FAILED','Configuration changed. Reload the policy.')
            if self.operations.pending or self.discovery_pending or self.certificate_job_pending or self.certificates.busy():
                raise Invalid("Certificate maintenance is in progress.")
            if value['hostname'] != previous['hostname'] and self.certificates.rename_blocked():
                raise Invalid("Disable HTTPS before renaming the bridge; reset/setup will require renewed client trust.")
            self.protection.guard(previous, value, reboot=legacy_reboot)
            self.guard_management(value)
            changed = {p['id'] for p in profiles(previous) if self.pauses.changed(p, next((n for n in profiles(value) if n['id'] == p['id']), None))}
            if any(self.pauses.held(ident) for ident in changed):
                if self.pauses.error: raise ApiError(409, 'PAUSE_REGISTRY_RECOVERY_REQUIRED', 'Recover the pause registry before editing held network intent.')
                self.protection.precondition(expected, revision or (lambda: ''))
            self.store.save(value)
            if value['home']!=previous['home']:self.next_home_attempt=0
            self.pauses.clear(changed)
            operation_id = self.connection_events.request(previous, value, cause)
            self.blocked.clear()
            # Legacy clients retain the original reboot contract. The new UI
            # applies settings live; profile switches never restart the home radio.
            reboot = legacy_reboot
            if reboot:
                self.action_at = time.monotonic() + 4
        self.update(message="Configuration saved. Applying settings…")
        return {"ok": True, "url": "http://" + value["hostname"] + ".local", "reboot_in": 4 if reboot else 0,
                "operation_id": operation_id, "sessions_invalidated": password_changed}

    def credentials(self, payload):
        target = payload.get("target")
        if target not in ("web", "hotspot", "linux"):
            raise Invalid("Unknown account.")
        from .runtime import capabilities
        if target == "linux" and not capabilities(self.demo)['linux_credentials']:
            raise Invalid("Unknown account.")
        password = text(payload.get("password"), 128)
        if not 8 <= len(password) <= 128:
            raise Invalid("Password must contain 8 to 128 characters.")
        if target == "hotspot" and not (8 <= len(password.encode()) <= 63 or
                len(password) == 64 and all(c in "0123456789abcdefABCDEF" for c in password)):
            raise Invalid("Hotspot password must contain 8 to 63 bytes or 64 hexadecimal characters.")
        current = text(payload.get("current_password"), 128)
        with self.lock:
            if self.operations.pending: raise ApiError(409, 'CONNECTION_OPERATION_PENDING', 'A connection operation is pending.')
        with self.net_lock, self.lock:
            self.protection.reject()
            if self.operations.pending: raise ApiError(409, 'CONNECTION_OPERATION_PENDING', 'A connection operation is pending.')
            now = time.monotonic()
            while self.password_attempts and now - self.password_attempts[0] >= 60:
                self.password_attempts.popleft()
            if len(self.password_attempts) >= 5:
                raise Invalid("Too many attempts. Try again in a minute.")
            self.password_attempts.append(now)
            encoded = self.store.config["admin_hash"]
            valid = password_matches(current, encoded) if encoded else current == self.bootstrap["password"]
            if not valid:
                raise Invalid("Incorrect current administrator password.")
            self.password_attempts.clear()
            value = copy.deepcopy(self.store.config)
            if target == "linux":
                self.net.linux_password(password)
                value["linux_password_default"] = password == "12345678"
            elif target == "web":
                value["admin_hash"] = password_hash(password)
            else:
                value["hotspot_password"] = password
            self.store.save(value)
        return {"ok": True, "sessions_invalidated": target == "web" and not hmac.compare_digest(password.encode(), current.encode()),
                "hotspot_reconnect": target == "hotspot" and self.state["hotspot"]["active"]}

    def telescope_action(self, payload, cause="MANUAL", expected=None, revision=None):
        ident, action = payload.get("id"), payload.get("action")
        if action not in ("connect", "disconnect", "delete"):
            raise Invalid("Unknown action.")
        with self.lock:
            if self.operations.pending: raise ApiError(409, 'CONNECTION_OPERATION_PENDING', 'A connection operation is pending.')
            if self.discovery_pending: raise ApiError(409, 'NETWORK_BUSY', 'Discovery is in progress.')
            value = copy.deepcopy(self.store.config)
            entries = copy.deepcopy(profiles(value))
            chosen = next((p for p in entries if p["id"] == ident), None)
            if not chosen:
                raise Invalid("Unknown profile.")
            if self.pauses.held(ident):
                self.protection.reject(ident)
                self.protection.precondition(expected, revision or (lambda: ''))
            if action == "connect":
                target = payload.get("mac", chosen["mac"])
                if not isinstance(target, str) or target not in {d["mac"] for d in self.state["devices"]}:
                    raise Invalid("The selected adapter is unavailable.")
                if target == value.get("home", {}).get("mac"):
                    raise Invalid("This adapter is reserved for the private network.")
                if "expected_mac" in payload and payload["expected_mac"] != chosen["mac"]:
                    raise Invalid("Assignment changed. Refresh before moving this telescope.")
                chosen["mac"] = target
                for p in entries:
                    if p["mac"] == chosen["mac"]:
                        p["enabled"] = p["id"] == ident
            elif action == "disconnect":
                chosen["enabled"] = False
            else:
                entries.remove(chosen)
            # Blank passwords retain the stored secret for the same profile/SSID.
            value["telescopes"] = entries
            value["admin_password"] = ""
            previous = self.store.config
            normalized = validate_config(value, previous, self.state["devices"])
            self.protection.guard(previous, normalized)
            self.guard_management(normalized)
            if self.pauses.error: raise ApiError(409, 'PAUSE_REGISTRY_RECOVERY_REQUIRED', 'Recover the pause registry before retrying individual connections.')
            changed = {p['id'] for p in profiles(previous) if self.pauses.changed(p, next((n for n in profiles(normalized) if n['id'] == p['id']), None))} | {ident}
            if any(self.pauses.held(key) for key in changed): self.protection.precondition(expected, revision or (lambda: ''))
            self.store.save(normalized)
            self.pauses.clear(changed)
            operation_id = self.connection_events.request(previous, self.store.config, cause, selected=ident)
            self.blocked.clear()
            if action == "delete":
                self.pending_deletions.add(role_for(chosen))
        return {"ok": True, "operation_id": operation_id}

    def retry(self):
        from .runtime import capabilities
        if not capabilities(self.demo)['host_reboot'] and not self.demo:
            raise Invalid("The OS updater prerequisites are not installed.")
        with self.lock:
            self.protection.reject()
            if self.operations.pending: raise ApiError(409, 'CONNECTION_OPERATION_PENDING', 'A connection operation is pending.')
            if self.discovery_pending or self.certificate_job_pending or self.certificates.busy():
                raise ApiError(409, 'HTTPS_BUSY', 'Certificate maintenance is in progress.')
            self.action_at = time.monotonic() + 4
        self.update(mode="rebooting", message="Rebooting and retrying the connection…")

    def scan(self, name):
        if name not in {d["name"] for d in self.status()["devices"]}:
            raise Invalid("Unknown interface.")
        if not self.net_lock.acquire(blocking=False):
            raise Invalid("Connection in progress. Try scanning again shortly.")
        try:
            return self.net.scan(name)
        finally:
            self.net_lock.release()

    def active_ap(self, devices):
        known = self.state.get('hotspot', {}).get('mac')
        ordered = sorted(devices, key=lambda d:d['mac'] != known)
        return next((d for d in ordered if self.net.connected('setup', d)), None)

    def ap_can_stop(self, device):
        # Caller holds net_lock. Never use display clients/DHCP or cached vacancy.
        with self.lock:
            if (self.protection.protected() or self.operations.pending or self.discovery_pending
                    or self.certificate_job_pending or self.certificates.busy() or self.action_at is not None
                    or self.stop.is_set() or self.cleanup_required): return False
        if not self.net.ap_vacant(device): return False
        with self.lock:
            return not (self.protection.protected() or self.operations.pending or self.discovery_pending
                        or self.certificate_job_pending or self.certificates.busy() or self.action_at is not None
                        or self.stop.is_set() or self.cleanup_required)

    def _ap(self, devices, reserved, emergency=False, only=None):
        current = self.active_ap(devices)
        preferred = self.store.config.get('hotspot', {}).get('mac', '')
        password = self.store.config.get('hotspot_password', self.bootstrap['password'])
        current_name = self.net.hotspot_name() if current else ''
        if current and (not preferred or current['mac'] == preferred) and self.ap_password in (None,password) and current_name == hotspot_ssid(self.store.config, current['mac']):
            self.ap_password = password
            reason=self.state['hotspot'].get('reason')
            if reason not in ('requested','recovery','recovery_retained'):reason='recovery' if emergency else 'requested'
            self.update(setup_ssid=current_name,hotspot={'active':True,'reason':reason,'interface':current['name'],'mac':current['mac'],'ip':'192.168.77.1',**self.net.hotspot_clients(current)})
            return current
        if (self.protection.protected() or self.operations.pending) and not emergency:
            if current:
                self.update(setup_ssid=current_name,hotspot={'active':True,'reason':'connection_protected','interface':current['name'],'mac':current['mac'],'ip':'192.168.77.1',**self.net.hotspot_clients(current)})
                return current
            self.update(setup_ssid='',hotspot={'active':False,'reason':'connection_operation_pending' if self.operations.pending else 'connection_protected'})
            return None
        protected = {p['mac'] for p in profiles(self.store.config) if self.protection.protected(p['id']) or self.pauses.held(p['id'])}
        home_mac = self.store.config.get('home',{}).get('mac')
        candidates = [d for d in devices if d['mac'] not in protected and d.get('ap',True)
                      and (d['mac'] not in reserved or emergency and d['mac']==home_mac)
                      and (only is None or d['mac']==only['mac']) and self.net.radio_idle(d)]
        candidates.sort(key=lambda d:(d['mac']==home_mac,d['usb'],d['name']))
        selected = next((d for d in candidates if d['mac'] == preferred), None)
        if preferred:
            device = selected or (current if current and current['mac'] == preferred else None)
            if device is None and (current or emergency):
                # Keep a working management path; recovery may use another free
                # radio, but never change the persisted owner-selected adapter.
                device = current or (candidates[0] if candidates else None)
            if device is current and current and current['mac'] != preferred:
                self.update(setup_ssid=current_name,hotspot={'active':True,'reason':'selected_adapter_unavailable',
                    'interface':current['name'],'mac':current['mac'],'ip':'192.168.77.1',**self.net.hotspot_clients(current)})
                return current
        else:
            device = current or (candidates[0] if candidates else None)
        if current:self.protection.reject()
        if not device:
            self.update(setup_ssid='',hotspot={'active':False,'reason':'protected_no_recovery_radio' if emergency and protected else 'selected_adapter_unavailable' if preferred else 'no_free_radio'})
            return None
        # No backend empty-plan reset: stage isolation while retaining every valid route.
        self.firewall(extra_isolated=[device])
        ssid = hotspot_ssid(self.store.config, device['mac'])
        self.net.hotspot(device,ssid,password,self.hotspot_route_targets(devices))
        self.ap_password = password
        self.update(setup_ssid=ssid,
                    hotspot={'active':True,'reason':'recovery' if emergency else 'requested',
                             'interface':device['name'],'mac':device['mac'],'ip':'192.168.77.1',**self.net.hotspot_clients(device)})
        return device

    def hotspot_route_targets(self, devices):
        # Include inactive saved instruments so normal switching can reuse a lease.
        # The home FIB still matters when its ingress was rejected for overlap.
        networks, addresses = [], set()
        home_mac = self.store.config.get('home', {}).get('mac')
        for device in devices:
            value = self.net.address(device, timeout=2)
            if not value:
                continue
            interface = ipaddress.IPv4Interface(value)
            addresses.update((interface.ip, interface.network.network_address, interface.network.broadcast_address))
            if device['mac'] == home_mac:
                networks.append(interface.network)
        return hotspot_route_targets((p.get('target_ip') for p in profiles(self.store.config)), networks, addresses)

    def management_paths(self, home, ap):
        return self.net.management_paths(home,ap)

    def current_management(self, devices=None):
        """Recheck only advertised paths under net_lock before a network mutation."""
        devices=self.net.devices() if devices is None else devices
        paths=[]
        for path in self.state.get('management',{}).get('paths',[])[:2]:
            mac=self.store.config.get('home',{}).get('mac') if path['role']=='home' else self.state['hotspot'].get('mac')
            device=next((d for d in devices if d['mac']==mac and d['name']==path['interface']),None)
            if not device:continue
            up=self.net.connected('home',device,self.store.config['home']) if path['role']=='home' else self.net.connected('setup',device)
            if not up:continue
            address=self.net.address(device);interface=ipaddress.ip_interface(address)
            if str(interface.ip)==path['ip'] and str(interface.network)==path['subnet']:
                paths.append({'role':path['role'],'device':device,'address':address})
        return paths

    def occupied_management_networks(self, devices):
        # Rejected home ingress can still own a preferred DHCP route in the FIB.
        networks=[ipaddress.ip_network('192.168.77.0/24')]
        networks += [ipaddress.ip_interface(path['address']).network for path in self.current_management(devices)]
        home=next((d for d in devices if d['mac']==self.store.config.get('home',{}).get('mac')),None)
        if home and self.net.connected('home',home,self.store.config['home']):networks.append(ipaddress.ip_interface(self.net.address(home)).network)
        return list(dict.fromkeys(networks))

    def _management(self, devices, config, now):
        tick=time.monotonic();by_mac={d['mac']:d for d in devices}
        home=by_mac.get(config.get('home',{}).get('mac'));ap=self.active_ap(devices)
        if not ap:self.net.release_stale_hotspot(devices)
        if ap:ap=self._ap(devices,set(),emergency=not config.get('hotspot',{}).get('enabled',False))
        retry={'reason':'NOT_CONFIGURED' if not config['home'] else 'HOME_READY','remaining_seconds':0}
        home_changed='home' in self.applied and self.applied['home']!=config['home']
        if home_changed:
            self.firewall();self.net.disconnect('home');self.applied.pop('home',None);self.next_home_attempt=0
        home_up=bool(home and config['home'] and self.net.connected('home',home,config['home']))
        stopped_ap=None
        if config['home'] and not home_up:
            retry={'reason':'HOME_RADIO_MISSING' if not home else 'RETRY_WAIT','remaining_seconds':max(0,int(self.next_home_attempt-now))}
            if home and now>=self.next_home_attempt:
                if ap and ap['mac']==home['mac']:
                    if self.ap_can_stop(ap):
                        self.update(message='Empty recovery hotspot: a bounded private-network retry may briefly interrupt management.')
                        stopped_ap=ap;self.net.disconnect('setup');ap=None
                        self.update(setup_ssid='',hotspot={'active':False,'reason':'home_retry'})
                    else:retry={'reason':'HOME_RETRY_DEFERRED_AP_RESERVED','remaining_seconds':0}
                if not ap or ap['mac']!=home['mac']:
                    if self.net.radio_idle(home):
                        # Keep a separate AP and all validated routes throughout the blocking command.
                        self.firewall()
                        try:
                            self.net.connect('home',home,config['home']);home_up=self.net.connected('home',home,config['home'])
                        except NetworkError:home_up=False
                        if home_up:self.applied['home']=copy.deepcopy(config['home'])
                        if stopped_ap and not home_up:
                            try:ap=self._ap(devices,set(),emergency=True,only=stopped_ap)
                            except NetworkError:
                                self.update(hotspot={'active':False,'reason':'restore_failed'})
                            retry={'reason':'HOME_RETRY_FAILED_AP_RESTORED' if ap else 'HOME_RETRY_FAILED_AP_RESTORE_FAILED','remaining_seconds':45}
                        else:retry={'reason':'HOME_READY' if home_up else 'HOME_RETRY_FAILED','remaining_seconds':0 if home_up else 45}
                        completed=now+max(0,time.monotonic()-tick)
                        self.last_home_attempt=completed;self.next_home_attempt=completed+45
                    elif not ap or ap['mac']!=home['mac']:retry={'reason':'HOME_RETRY_DEFERRED_RADIO_BUSY','remaining_seconds':0}
        if not home_up:home=None
        instrument_networks=[];instrument_subnets={}
        for profile in profiles(config):
            device=by_mac.get(profile['mac'])
            if profile['enabled'] and device and self.net.connected(role_for(profile),device,profile):
                subnet=ipaddress.ip_interface(self.net.address(device)).network
                instrument_networks.append(subnet);instrument_subnets[profile['id']]=subnet
        self.routing_conflicts={ident for ident,subnet in instrument_subnets.items() if home and ipaddress.ip_interface(self.net.address(home)).network.overlaps(subnet)}
        if self.routing_conflicts:
            home=None;retry={'reason':'MANAGEMENT_TELESCOPE_SUBNET_CONFLICT','remaining_seconds':0}
        desired_ap=config.get('hotspot',{}).get('enabled',False) or config.get('connection_mode','private')!='private'
        reserved=({home['mac']} if home else set())|{p['mac'] for p in profiles(config) if p['enabled']}
        if not ap and (desired_ap or not home) and retry['reason']!='HOME_RETRY_FAILED_AP_RESTORE_FAILED':
            ap_network=ipaddress.ip_network('192.168.77.0/24')
            overlap=any(ap_network.overlaps(n) for n in instrument_networks+([ipaddress.ip_interface(self.net.address(home)).network] if home else []))
            if overlap:self.update(setup_ssid='',hotspot={'active':False,'reason':'subnet_conflict'})
            else:ap=self._ap(devices,reserved,emergency=not home)
        elif ap and home and not desired_ap:
            explicit_stop=self.state['hotspot'].get('reason')=='requested'
            if explicit_stop or self.ap_can_stop(ap):
                self.net.disconnect('setup');ap=None;self.update(setup_ssid='',hotspot={'active':False,'reason':'disabled'})
            else:self.update(hotspot=dict(self.state['hotspot'],reason='recovery_retained'))
        if home and ap and ipaddress.ip_interface(self.net.address(home)).network.overlaps(ipaddress.ip_network('192.168.77.0/24')):
            home=None;retry={'reason':'MANAGEMENT_SUBNET_CONFLICT','remaining_seconds':0}
        # A home association established above may add a newly conflicting route.
        if ap:self.net.hotspot_dhcp(ap,self.hotspot_route_targets(devices))
        paths=self.management_paths(home,ap)
        if paths:self.net.identity(config['hostname'],[p['device'] for p in paths])
        with self.lock:
            self.state['management']={'mode':'dual' if home and ap else 'home_only' if home else 'ap_only' if ap else 'none',
                'paths':[{'role':p['role'],'interface':p['device']['name'],'ip':str(ipaddress.ip_interface(p['address']).ip),'subnet':str(ipaddress.ip_interface(p['address']).network)} for p in paths],
                'reason':'ROUTING_CONFLICT' if self.routing_conflicts else 'READY' if paths else 'MANAGEMENT_UNAVAILABLE','home_retry':retry}
        self.update(home_ip=self.net.address(home).split('/')[0] if home else '')
        return home,ap

    def retained_forwarding(self):
        """Keep unaffected, still-owned validated routes during free-radio preparation."""
        config = self.store.config
        devices = {d['mac']:d for d in self.state['devices']}
        home = devices.get(config.get('home', {}).get('mac'))
        if not home or not self.net.connected('home', home, config['home']): home = None
        rows = {row['id']:row for row in self.state['telescopes']}
        pending = self.operations.records[self.operations.active]['public']['profile_id'] if self.operations.pending else None
        relays = []
        for profile in profiles(config):
            row = rows.get(profile['id'], {}); device = devices.get(profile['mac'])
            if (self.pauses.held(profile['id']) or profile['id'] == pending or profile['id'] == getattr(self.operations, 'victim_id', None) or not profile['enabled'] or not device
                    or row.get('state') not in ('online','waiting_service') or not row.get('connected')
                    or network_intent(self.applied.get(role_for(profile))) != network_intent(profile)):
                continue
            if not self.net.connected(role_for(profile), device, profile): continue
            network = ipaddress.ip_interface(self.net.address(device))
            if str(network.network) != row.get('subnet') or ipaddress.ip_address(profile['target_ip']) not in network.network: continue
            if home and network.network.overlaps(ipaddress.ip_interface(self.net.address(home)).network):continue
            relays.append({'profile_id':profile['id'],'device':device,'target':profile['target_ip'],
                           'kind':profile['kind'],**{proxy_flag(p['id']):bool(row.get(proxy_flag(p['id']))) for p in relay_plugins()}})
        ap = devices.get(self.state['hotspot'].get('mac')) if self.state['hotspot'].get('active') else None
        if ap and not self.net.connected('setup',ap):ap=None
        if home and (self.routing_conflicts or ap and ipaddress.ip_interface(self.net.address(home)).network.overlaps(ipaddress.ip_interface(self.net.address(ap)).network)):home=None
        return {'home':home,'telescopes':relays if home or ap else [],'setup':ap}

    def firewall(self, **kwargs):
        with self.lock:
            self.discovery_plan = None
        extra=kwargs.pop('extra_isolated',[])
        if not kwargs:
            kwargs = self.retained_forwarding()
        config = self.operations.intent(self.pauses.apply(copy.deepcopy(self.store.config)))
        macs = {p['mac'] for p in profiles(config)} - {config.get('home', {}).get('mac')}
        macs |= set(self.cleanup_required.values())
        if self.operations.pending and self.operations.allocated: macs.add(self.operations.allocated)
        macs|={d['mac'] for d in extra}
        if not kwargs.get('home'):macs.add(config.get('home',{}).get('mac'))
        isolated = [d for d in self.state['devices'] if d['mac'] in macs]
        access = self.net.firewall(isolated=isolated, **kwargs,
            sharing=config.get('connection_mode')=='hotspot_shared',
            private_lan=config.get('hotspot_private_lan',False),
            hotspot_targets=self.hotspot_route_targets(self.state['devices']) if kwargs.get('setup') else ())
        self.update(network_access=dict(access or {'available':False,'reason':'DEMO_NOT_VERIFIED'},
            mode=config.get('connection_mode','private'), configured=config.get('connection_mode')=='hotspot_shared',
            private_lan_allowed=config.get('hotspot_private_lan',False), internet_verified=False))
        paths=self.management_paths(kwargs.get('home'),kwargs.get('setup'))
        proxy=next((r for r in kwargs.get('telescopes',[]) if r.get('seestar_proxy')),None)
        with self.lock:
            self.management_plans=tuple((p['device']['name'],p['address']) for p in paths)
            self.management_plan=next(iter(self.management_plans),None)
            self.discovery_plans=tuple((p['device']['name'],p['address'],proxy['device']['name'],proxy['target']) for p in paths) if proxy else ()
            self.discovery_plan=next(iter(self.discovery_plans),None)

    def setup(self, devices, message):
        # Error recovery can establish management without claiming telescope success.
        self.update(devices=devices,home_ip='')
        reserved={p['mac'] for p in profiles(self.store.config) if p['enabled']}
        ap=self._ap(devices,reserved,emergency=True) if devices else None
        plan=self.retained_forwarding();plan.update(home=None,setup=ap)
        self.firewall(**plan)
        self.update(mode='setup',message=message)

    def _disconnect_profile(self, role):
        """Record intention, then verify disassociation before reporting it."""
        with self.lock:
            candidates = profiles(self._event_config or self.store.config)
            profile = self.applied.get(role) or next((p for p in candidates if role_for(p) == role), None)
            if profile is None:
                profile = next((s.get("profile") for s in self.connection_events.states.values()
                                if s.get("profile") and role_for(s["profile"]) == role), None)
            context = self._event_contexts.get(profile["id"]) if profile else None
            if profile:
                self.connection_events.phase(profile, "disconnecting", "DISCONNECTING", context)
        self.net.disconnect(role)
        if profile:
            device = next((d for d in self.state["devices"] if d["mac"] == profile.get("mac")), None)
            associated = bool(device and self.net.connected(role, device))
            if not associated:
                with self.lock:
                    self._event_contexts[profile["id"]] = self.connection_events.observe(
                        dict(profile, enabled=False), False, False, "saved", "PROFILE_DISABLED", context)
            else:
                with self.lock:
                    self.connection_events.phase(profile, "failed", "RECONCILE_FAILED", context)

    def _observe_cycle(self, failed=False):
        with self.lock:
            config = self._event_config or copy.deepcopy(self.store.config)
            current = {p["id"]: p for p in profiles(config)}
            previous = copy.deepcopy(self.connection_events.states)
            rows = {p["id"]: p for p in self.state["telescopes"]}
            devices = {d["mac"]: d for d in self.state["devices"]}
            mode = self.state["mode"]
        for ident in dict.fromkeys([*previous, *current]):
            profile = current.get(ident) or dict(previous[ident].get("profile", {}), enabled=False)
            if not profile.get("id"):
                continue
            row = rows.get(ident, {})
            context = self._event_contexts.get(ident)
            phase = row.get("state", "saved")
            associated = bool(row.get("connected")) if ident in current else False
            ready = bool(row.get("relay_ready")) and not failed and ident in current
            if failed or (not profile.get("enabled") and previous.get(ident, {}).get("associated")):
                # Recovery can suspend forwarding while preserving Wi-Fi on another
                # adapter. Do not turn its synthetic disconnected status into an event.
                # The desired profile may have moved while recovery kept its old
                # association. Inspect actual role ownership on both known adapters.
                macs = dict.fromkeys([previous.get(ident, {}).get("mac"), profile.get("mac")])
                try:
                    associated = False
                    for mac in macs:
                        device = devices.get(mac)
                        if device and self.net.connected(role_for(profile), device):
                            associated = True
                            profile = dict(profile, mac=mac)
                            break
                except NetworkError:
                    associated = previous.get(ident, {}).get("associated", False)
                    if associated:
                        profile = dict(profile, mac=previous[ident].get("mac"))
                ready = False
            if failed:
                phase, reason = "error", "RECONCILE_FAILED"
            elif profile.get("enabled") and profile.get("mac") not in devices:
                phase, reason, associated, ready = "missing", "RADIO_UNAVAILABLE", False, False
            elif phase == "conflict":
                reason = "CONFIGURATION_CONFLICT"
            elif phase == "invalid_ip":
                reason = "INVALID_TARGET"
            elif not self.state.get("management",{}).get("paths"):
                reason = "HOME_UNAVAILABLE"
            elif not profile.get("enabled"):
                if associated:
                    phase, reason = "error", "RECONCILE_FAILED"
                else:
                    reason = "PROFILE_DISABLED" if ident in current else "PROFILE_REMOVED"
            elif associated and not ready:
                reason = "SERVICE_UNAVAILABLE"
            elif not associated:
                reason = "CONNECTION_LOST" if previous.get(ident, {}).get("associated") else "AWAITING_ASSOCIATION"
            else:
                reason = "READY"
            with self.lock:
                self._event_contexts[ident] = self.connection_events.observe(profile, associated, ready, phase, reason, context)
        with self.lock:
            self.connection_events.prune(current, devices)
            self._event_contexts = {ident: context for ident, context in self._event_contexts.items()
                                    if ident in self.connection_events.contexts}

    def cycle(self, now=None):
        self._event_config = None
        self._event_contexts = {}
        self.last_cycle_ok = False
        try:
            # A network reconcile can take several seconds on the Zero W. Sample
            # both sides (with the collector's five-second throttle), rather than
            # stretching each interval by the reconcile plus the loop's sleep.
            if hasattr(self.net, "sample_activity"): self.net.sample_activity(policy=auto_policy(self.store.config))
            self._cycle(now)
            with self.lock: self.operations.gate_cycle()
        except Exception:
            with self.lock:
                self.discovery_plan=None;self.discovery_plans=()
                self.state['network_access']={'available':False,'reason':'PLANNING_FAILED'}
                self.state['relay_ready']=False
                for row in self.state['telescopes']:
                    row['relay_ready']=False;row['legacy_ports']=False;row['seestar_proxy']=False;row['dwarf_proxy']=False
                    for p in relay_plugins():row[proxy_flag(p['id'])]=False
                    for path in row.get('management_paths',[]):path['forwarding_ready']=False
            self._observe_cycle(failed=True)
            raise
        else:
            self._observe_cycle()
            self.last_cycle_ok = True
            if hasattr(self.net, "sample_activity"): self.net.sample_activity(policy=auto_policy(self.store.config))

    def _cycle(self, now=None):
        now = time.monotonic() if now is None else now
        devices = self.net.devices()
        self.update(devices=devices)
        with self.lock:
            config = self.operations.intent(self.pauses.apply(copy.deepcopy(self.store.config)))
            self._event_config = config
            for profile in profiles(config):
                self.connection_events.context(profile)
            self._event_contexts = copy.deepcopy(self.connection_events.contexts)
            action = self.action_at
        if action is not None:
            if now >= action:
                self.net.reboot()
                if self.demo:
                    self.action_at = None
                    self.last_home_attempt = -1000
                    self.applied.clear()
                    self.net.disconnect("home")
                    for role in self.managed_roles:
                        self._disconnect_profile(role)
                else:
                    self.stop.wait(5)
            return
        if self.country != config["country"]:
            self.net.country(config["country"])
            self.country = config["country"]
        for role, mac in list(self.cleanup_required.items()):
            self.net.disconnect(role)
            device = next((d for d in devices if d['mac'] == mac), None)
            if not device or not self.net.connected(role, device): self.cleanup_required.pop(role, None)
        if not self.configured_this_boot:
            self.firewall()
            owned = self.net.known_telescope_roles()
            wanted = {role_for(p) for p in profiles(config) if p['enabled']}
            for role in owned - wanted:
                if self.pauses.error and any(role_for(p) == role and self.protection.protected(p['id']) for p in profiles(self.store.config)): continue
                self._disconnect_profile(role)  # Remove orphan pending associations even if home is down.
            self.managed_roles |= owned
            self.configured_this_boot = True
        by_mac={d['mac']:d for d in devices}
        home,ap=self._management(devices,config,now)
        entries = profiles(config)
        desired = {role_for(p): p for p in entries if p["enabled"]}
        stale = self.managed_roles - desired.keys()
        changed = {r for r, p in desired.items() if r in self.applied and network_intent(self.applied[r]) != network_intent(p)}
        if stale or changed:
            self.firewall()
            for role in stale | changed:
                if self.pauses.error and any(role_for(p) == role and self.protection.protected(p['id']) for p in profiles(self.store.config)): continue
                self._disconnect_profile(role)
                self.applied.pop(role, None)
                self.attempts.pop(role, None)
        obsolete = self.managed_roles - {role_for(p) for p in entries}
        with self.lock:
            pending = (self.pending_deletions | obsolete) - desired.keys()
            self.pending_deletions -= pending
        for role in pending:
            self.net.remove_profile(role)
        self.managed_roles = set(desired)
        topology = (tuple(sorted(by_mac)), tuple((p["device"]["name"],p["address"]) for p in self.management_paths(home,ap)),
                    tuple((p["id"], p["mac"], p["target_ip"], p["ssid"]) for p in entries if p["enabled"]))
        if topology != self.topology:
            self.blocked.clear()
            self.topology = topology
        proxy_owners=dict(self.proxy_owners)
        proxy_kinds={proxy_flag(p['id']):p['id'] for p in relay_plugins()}
        for flag,ident in list(proxy_owners.items()):
            profile=next((p for p in entries if p['id']==ident and p['enabled']),None)
            device=by_mac.get(profile['mac']) if profile else None
            if (not profile or profile['kind']!=proxy_kinds[flag] or not device or self.pauses.held(ident)
                    or network_intent(self.applied.get(role_for(profile)))!=network_intent(profile)
                    or not self.net.connected(role_for(profile),device,profile)):
                proxy_owners[flag]=None

        rows, relays = [], []
        paths=self.management_paths(home,ap)
        occupied=[ipaddress.ip_interface(p["address"]).network for p in paths]
        if ipaddress.ip_network("192.168.77.0/24") not in occupied:occupied.append(ipaddress.ip_network("192.168.77.0/24"))
        # Reconcile validated/protected associations before new intent, without
        # changing saved order (the explicit force policy still uses that order).
        def priority(profile):
            device=by_mac.get(profile['mac']);role=role_for(profile)
            validated=(network_intent(self.applied.get(role))==network_intent(profile)
                       and device and self.net.connected(role,device,profile))
            return (not (validated and self.protection.protected(profile['id'])),not validated)
        for p in sorted(entries,key=priority):
            role, device = role_for(p), by_mac.get(p["mac"])
            row = {"id": p["id"], "target_ip": p["target_ip"], "client_ip": "", "connected": False,
                   "relay_ready": False, "interface": device["name"] if device else "", "state": "saved", "message": "Saved"}
            rows.append(row)
            if self.pauses.held(p['id']):
                row.update(state='paused', message='Connection paused: explicit owner action required.')
            if not p["enabled"]:
                continue
            if not device:
                row.update(state="missing", message="Adapter missing")
                continue
            if ap and device['mac']==ap['mac']:
                row.update(state='conflict',message='Hotspot adapter is reserved for management.')
                continue
            blocked = self.blocked.get(role)
            if blocked:
                row.update(blocked)
                continue
            up = self.net.connected(role, device, p)
            if not up:
                with self.lock:
                    if self.connection_events.states.get(p["id"], {}).get("associated"):
                        self._event_contexts[p["id"]] = self.connection_events.observe(
                            p, False, False, "waiting", "CONNECTION_LOST", self._event_contexts.get(p["id"]))
            if not up and paths and now - self.attempts.get(role, -1000) >= 15:
                if not self.operations.before_association(p):
                    row.update(state='error'); continue
                self.attempts[role] = now
                self.firewall()
                with self.lock:
                    self.connection_events.phase(p, "connecting", "CONNECTING", self._event_contexts.get(p["id"]))
                try:
                    self.net.connect(role, device, p)
                    self.applied[role] = copy.deepcopy(p)
                    up = self.net.connected(role, device, p)
                except NetworkError:
                    up = False
                    self.operations.fail_association(p)
                    with self.lock:
                        self.connection_events.phase(p, "failed", "ASSOCIATION_FAILED", self._event_contexts.get(p["id"]))
            if not up:
                row.update(state="waiting", message="Waiting for the telescope · automatic reconnection")
                continue
            address = self.net.address(device)
            row.update(connected=True, client_ip=address, state="connected", message="Wi-Fi connected")
            network = ipaddress.IPv4Interface(address)
            target = ipaddress.IPv4Address(p["target_ip"])
            if p['id'] in self.routing_conflicts:
                row.update(state='conflict',subnet=str(network.network),message='Home routing conflicts with this instrument; forwarding is suspended without disconnecting it.')
                continue
            if any(network.network.overlaps(n) for n in occupied):
                protected=self.protection.protected(p["id"])
                if not protected:self._disconnect_profile(role)
                row.update(connected=bool(protected), client_ip=address if protected else "", state="conflict", message="Subnet already in use. Connect this profile alone or change its network.")
                self.blocked[role] = copy.deepcopy(row)
                continue
            if target not in network.network or target in (network.ip, network.network.network_address, network.network.broadcast_address):
                protected=self.protection.protected(p["id"])
                if not protected:self._disconnect_profile(role)
                row.update(connected=bool(protected), client_ip=address if protected else "", state="invalid_ip", message="Telescope address is outside the subnet. Correct its IP.")
                self.blocked[role] = copy.deepcopy(row)
                continue
            if not self.operations.verify_association(p, device):
                row.update(relay_ready=False, state='error'); continue
            occupied.append(network.network)
            route = {"profile_id":p['id'],"kind":p['kind'],"device":device,"target":p['target_ip']}
            for spec in relay_plugins():
                flag=proxy_flag(spec['id']);ports={(spec['relay']['transport'],port) for port in spec['relay']['ports']}
                conflicting=any(owner and owner!=p['id'] and ports & {(plugin(proxy_kinds[other])['relay']['transport'],port) for port in plugin(proxy_kinds[other])['relay']['ports']}
                                for other,owner in proxy_owners.items())
                route[flag]=p['kind']==spec['id'] and not conflicting and proxy_owners[flag] in (None,p['id'])
            for flag in proxy_owners:
                if route.get(flag):proxy_owners[flag]=p['id']
            if paths:relays.append(route)
            ready = self.net.probe(p["target_ip"], device) if plugin(p['kind'])['readiness']=='vespera_tcp' else True
            row.update(relay_ready=bool(ready and paths), state="online" if ready and paths else "waiting_service",
                       message="Forwarding available" if ready else "Wi-Fi connected · waiting for service",
                       subnet=str(network.network),
                       **{proxy_flag(spec['id']):bool(paths and route[proxy_flag(spec['id'])]) for spec in relay_plugins()},
                       native_proxy_reason='NATIVE_PROXY_CONFLICT' if plugin(p['kind'])['relay'] and not route[proxy_flag(p['kind'])] else None,
                       management_paths=[{'role':path['role'],'host':str(ipaddress.ip_interface(path['address']).ip),'subnet':str(ipaddress.ip_interface(path['address']).network),'forwarding_ready':bool(ready)} for path in paths])
        self.proxy_owners=proxy_owners
        self.firewall(home=home, telescopes=relays, setup=ap)
        # Adopt an already-owned association only after addressing/forwarding validated.
        # Later password/target/selector changes still compare against this snapshot.
        for profile in entries:
            if any(row['id'] == profile['id'] and row.get('connected') and row.get('subnet')
                   and row['state'] in ('online', 'waiting_service') for row in rows):
                self.applied.setdefault(role_for(profile), copy.deepcopy(profile))
        order={p["id"]:index for index,p in enumerate(entries)}
        rows.sort(key=lambda row:order[row["id"]])
        any_up = any(r["connected"] for r in rows)
        any_ready = any(r["relay_ready"] for r in rows)
        self.update(home_ip=self.net.address(home).split("/")[0] if home else "", telescopes=rows,
                    mode=("online" if any_ready or not desired else "waiting_service" if any_up else "waiting_telescope") if home else "field" if any_ready else "setup" if paths else "no_management",
                    telescope_connected=any_up, relay_ready=any_ready, last_error="",
                    message="Management forwarding is available." if paths else "No confirmed management forwarding path.")

    def loop(self):
        while not self.stop.is_set():
            try:
                with self.net_lock:
                    self.cycle()
            except NetworkError as error:
                self.update(last_error=str(error), relay_ready=False)
                # An instrument error must never commandeer a working home radio.
                if not self.state["home_ip"]:
                    try:
                        with self.net_lock:
                            self.setup(self.net.devices(), str(error))
                    except NetworkError:
                        self.update(mode="error", message="Could not start the hotspot. Check its driver and service logs.")
                else:
                    self.update(mode="error", message="Network error. Check the current management paths.")
            except Exception as error:
                print("wifi-bridge: internal error " + type(error).__name__, flush=True)
                self.update(mode="error", relay_ready=False, message="Internal error. Check the service log.")
            self.stop.wait(5)
