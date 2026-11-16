"""Owner-enabled link_v1 producer; one shared thread, cached reads only."""
import threading

from ..config import profiles
from ..link_observations import EchoWindow
from .settings import selected_adapters


class LinkCollector:
    def __init__(self, controller, *, ping=True, probe=None):
        self.c = controller
        self.clock = controller.telemetry_store.monotonic
        self.probe = probe or EchoWindow(clock=self.clock)
        self.ping = ping and not controller.demo
        self.previous = {}
        self.disconnects = {}
        self.associated = set()
        self.thread = None
        self.halt = threading.Event()

    def connection(self, ident, connected):
        """Called with controller lock before session reconciliation on loss."""
        profile = next((p for p in profiles(self.c.store.config) if p['id'] == ident), None)
        enabled = bool(profile and 'link_v1' in selected_adapters(self.c.telemetry_store.settings, profile))
        if connected:
            if enabled: self.associated.add(ident)
            else: self.associated.discard(ident)
        else:
            if ident in self.associated:
                self.associated.remove(ident)
                if enabled:
                    self.disconnects[ident] = min(2**53, self.disconnects.get(ident, 0)+1)
                    self.c.telemetry_sessions.observe(ident, 'link.disconnects', self.disconnects[ident])
            self.previous.pop(ident, None)
            self.c.telemetry_sessions.end(ident, 'disconnect')

    def tick(self):
        c = self.c
        with c.lock:
            if self.halt.is_set() or c.telemetry_sessions.closed: return
            c.telemetry_sessions.tick()
            configured = {p['id']:p for p in profiles(c.store.config)}
            settings = c.telemetry_store.settings
            rows = {r['id']:r for r in c.state['telescopes'] if r.get('connected')}
            selected = {ident:p for ident,p in configured.items() if p['enabled'] and ident in rows
                        and 'link_v1' in selected_adapters(settings, p)}
            # These backends read only in-memory snapshots, including relay counters.
            cached = c.net.link_observations({ident:rows[ident] for ident in selected}) if hasattr(c.net, 'link_observations') else {}
            targets = {ident:(p['target_ip'], rows[ident]['interface']) for ident,p in selected.items()
                       if self.ping and not p.get('isolated') and rows[ident].get('interface')}
            self.probe.tick(targets)
            if self.halt.is_set() or c.telemetry_sessions.closed: return
            now = self.clock()
            live = {r['id'] for r in c.state['telescopes'] if r.get('connected')}
            for ident in list(self.previous):
                if ident not in selected or ident not in live: self.previous.pop(ident, None)
            for ident,p in selected.items():
                if ident not in live: continue
                adapters = selected_adapters(c.telemetry_store.settings, p)
                if 'link_v1' not in adapters: continue
                observation = cached.get(ident, {})
                radio = observation.get('radio', {})
                activity = observation.get('activity', {})
                ping = activity.get('ping', {}) if p.get('isolated') else self.probe.snapshot(ident)
                values = {'link.disconnects': self.disconnects.get(ident, 0),
                          'link.protected': c.protection.protected(ident)}
                for metric, value in (('link.rssi_dbm', radio.get('rssi_dbm')),
                                      ('link.bitrate_mbps', radio.get('bitrate_mbps')),
                                      ('link.ping_ms', ping.get('median_ms')),
                                      ('link.loss_pct', ping.get('loss_percent'))):
                    if value is not None: values[metric] = value
                classification = activity.get('consultation')
                if classification in ('none','unknown','mount_control','background_control','probable_content','unclassified'):
                    values['link.activity'] = classification
                totals = tuple(activity.get(key) for key in ('bytes_to_instrument','bytes_from_instrument'))
                generation = (p.get('mac'), observation.get('generation'))
                old = self.previous.get(ident)
                previous = old['totals'] if old and old['generation'] == generation else totals
                deltas = [max(0, new-prior) if type(new) is int and type(prior) is int and new >= prior else None
                          for new,prior in zip(totals, previous)]
                initial_activity = old is None and (
                    type(activity.get('interval_bytes')) is int and activity['interval_bytes'] > 0
                    or bool(activity.get('last_exchange')) and activity.get('last_activity_age_seconds', 2) <= 1)
                active = initial_activity or any(value and value > 0 for value in deltas)
                opened = active and ident not in c.telemetry_sessions.active
                if active:
                    # Activity extends/opens a session even if periodic sampling is not due.
                    c.telemetry_sessions.touch(ident, p['kind'], adapters, location=p.get('mac'))
                changed = not old or values != old['values'] or generation != old['generation']
                due = changed or opened or now-old['sampled'] >= 30
                if due:
                    baseline = old['sample_totals'] if old and old['generation'] == generation else totals
                    for metric,new,prior in zip(('link.bytes_to','link.bytes_from'), totals, baseline):
                        if type(new) is int and type(prior) is int and new >= prior:
                            values[metric] = min(2**53, new-prior)
                    for metric,value in values.items(): c.telemetry_sessions.observe(ident, metric, value)
                c.telemetry_sessions.readings.receive(ident, 'link_v1', values, 30, now)
                stable = {key:value for key,value in values.items() if key not in ('link.bytes_to','link.bytes_from')}
                self.previous[ident] = {'totals':totals, 'generation':generation, 'values':stable,
                                        'sampled':now if due else old['sampled'],
                                        'sample_totals':totals if due else old['sample_totals']}
            valid = set(configured)
            self.disconnects = {k:v for k,v in self.disconnects.items() if k in valid}
            self.associated.intersection_update(valid)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.loop, name='bridge-link', daemon=True)
            self.thread.start()

    def loop(self):
        while not self.halt.is_set() and not self.c.stop.is_set():
            try: self.tick()
            except Exception:
                # Never log source data, addresses or payloads.
                pass
            try: self.probe.wait(self.halt, 1)
            except OSError: self.halt.wait(1)
        self.probe.close()

    def close(self):
        self.halt.set()
        if self.thread: self.thread.join(timeout=3)
        else: self.probe.close()
