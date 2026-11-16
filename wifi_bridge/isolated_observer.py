"""Expiring, marked Alpaca permissions in an already owned radio namespace."""
import ipaddress
import socket

OBSERVER_MARK = 0xAB05
SETS = ('alpaca_http', 'alpaca_reply', 'alpaca_broadcast')


def declarations():
    return ('set alpaca_http { type ipv4_addr . inet_service; flags timeout; timeout 180s; size 4; }\n'
            'set alpaca_reply { type ipv4_addr . ipv4_addr . inet_service; flags timeout; timeout 5s; size 1; }\n'
            'set alpaca_broadcast { type ipv4_addr . ipv4_addr . inet_service; flags timeout; timeout 5s; size 1; }\n')


def input_rule():
    # Discovery replies are unicast and may be ct-new after a broadcast request.
    # Accept only the selected instrument -> exact bound local socket tuple.
    return 'ip protocol udp ip saddr . ip daddr . udp dport @alpaca_reply accept; '


def output_rules():
    return (f'meta mark {OBSERVER_MARK} ip protocol tcp ip daddr . tcp dport @alpaca_http accept; '
            f'meta mark {OBSERVER_MARK} ip protocol udp udp dport 32227 '
            'ip daddr . ip saddr . udp sport @alpaca_broadcast accept; ')


def flush(names=SETS):
    return ''.join('flush set inet ab_instrument '+name+'\n' for name in names)


class ObserverPermission:
    """One worker generation; old cleanup cannot erase a newer owner's sets.

    Constructing this object performs no commands or manager-lock acquisition.
    All policy I/O happens on the observer worker, outside controller.lock.
    """
    def __init__(self, manager, record, profile, address):
        if not profile.get('enabled') or not profile.get('isolated'):
            raise PermissionError('Isolated observation not enabled')
        self.manager, self.record = manager, record
        self.namespace, self.interface = record['namespace'], record['device']['name']
        self.ident, self.mac = profile['id'], profile['mac']
        self.target = str(ipaddress.IPv4Address(profile['target_ip']))
        self.address = ipaddress.IPv4Interface(address)
        if ipaddress.IPv4Address(self.target) not in self.address.network or self.target == str(self.address.ip):
            raise ValueError('Invalid isolated observation target')
        self.intent = manager.intent(profile)
        self.closed = False
        self.sockets = set()
        self.check()
        manager.observer_accesses.add(self)

    def check(self):
        record = self.record
        if (self.closed or self.manager.records.get(self.mac) is not record or not record.get('created') or record.get('observer_closing')
                or record.get('boot_id') != self.manager.boot_id or record.get('profile_id') != self.ident
                or record.get('target') != self.target or record.get('intent') != self.intent
                or record.get('namespace') != self.namespace or record['device'].get('name') != self.interface
                or record['device'].get('mac') != self.mac):
            raise PermissionError('Isolated observation ownership changed')

    def write(self, script):
        with self.manager.lock:
            self.check()
            old = self.manager.observer_permissions.get(self.mac)
            if old is not self: script = flush()+script
            if not self.record.get('observer_policy_v1'):
                # Existing owned namespaces may still have the previous native-only
                # table. One atomic replacement preserves its native/DHCP/ICMP
                # rules while introducing empty, marked-observer-only sets.
                from .isolated import KINDS, SERVICES, namespace_firewall
                transport = KINDS[self.record['kind']][0]
                ports = tuple(p for p, _ in SERVICES[self.record['kind']])
                script = ('delete table inet ab_instrument\n'
                          +namespace_firewall(self.target, transport, ports[0] if len(ports) == 1 else ports)+script)
            try: self.manager.command(self.record, ['nft', '-f', '-'], input=script, timeout=2)
            except (OSError, RuntimeError): raise PermissionError('Isolated observer policy unavailable') from None
            self.record['observer_policy_v1'] = True
            self.manager.observer_permissions[self.mac] = self
            if old is not None and old is not self: old.close()

    def endpoints(self, endpoints):
        if not isinstance(endpoints, (tuple, list)) or len(endpoints) > 4:
            raise ValueError('Invalid isolated observation endpoints')
        ports = set()
        for host, port in endpoints:
            if host != self.target or type(port) is not int or not 1 <= port <= 65535:
                raise ValueError('Invalid isolated observation endpoint')
            ports.add(port)
        script = flush(('alpaca_http',))
        if ports:
            script += 'add element inet ab_instrument alpaca_http { '+', '.join(
                self.target+' . '+str(port)+' timeout 180s' for port in sorted(ports))+' }\n'
        self.write(script)

    def discovery(self, port=None):
        script = flush(('alpaca_reply','alpaca_broadcast'))
        if port is not None:
            if type(port) is not int or not 1 <= port <= 65535: raise ValueError('Invalid discovery socket')
            local = str(self.address.ip)
            broadcast = str(self.address.network.broadcast_address)
            script += ('add element inet ab_instrument alpaca_reply { '+self.target+' . '+local+' . '+str(port)+' timeout 5s }\n'
                       'add element inet ab_instrument alpaca_broadcast { '+broadcast+' . '+local+' . '+str(port)+' timeout 5s }\n')
        self.write(script)

    def socket(self, kind):
        if kind not in (socket.SOCK_STREAM, socket.SOCK_DGRAM): raise ValueError('Invalid observer socket')
        from .isolated import namespace_socket
        with self.manager.lock:
            self.check()
            stream = namespace_socket('/run/netns/'+self.namespace, self.interface, kind, mark=OBSERVER_MARK)
            self.sockets.add(stream)
            return stream

    def release(self, stream):
        with self.manager.lock: self.sockets.discard(stream)

    def close(self):
        with self.manager.lock:
            if self.closed: return
            self.closed = True
            self.manager.observer_accesses.discard(self)
            for stream in self.sockets:
                try: stream.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                stream.close()
            self.sockets.clear()
            if self.manager.observer_permissions.get(self.mac) is not self: return
            self.manager.observer_permissions.pop(self.mac, None)
            # Never mutate a namespace no longer owned by this exact record.
            if self.manager.records.get(self.mac) is not self.record or self.record.get('namespace') != self.namespace: return
            try: self.manager.command(self.record, ['nft', '-f', '-'], input=flush(), timeout=2)
            except (OSError, RuntimeError): pass  # Empty live sockets; bounded nft expiry is the crash fallback.
