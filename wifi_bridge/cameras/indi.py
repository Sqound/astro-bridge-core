"""Supervise the optional platform INDI service on the camera ownership worker."""
from contextlib import ExitStack
import json
import os
import select
import socket
import subprocess
import time

PORT = 7624


class IndiSupervisor:
    def __init__(self, platform=None, clock=time.monotonic):
        self.platform, self.clock = platform, clock
        self.child = self.channel = self.plan = None
        self.listeners = ExitStack()
        self.guard = None
        self.state, self.addresses = 'off', frozenset()
        self.retry_at = self.last_reply = self.last_healthy = 0

    def stop(self):
        """Withdraw first. The camera stays reserved until child death is confirmed."""
        self.addresses = frozenset()
        self.state = 'unavailable'
        self.listeners.close()
        if self.channel:
            self.channel.close()
            self.channel = None
        if self.child is not None:
            # The platform monitor waits for namespace PID 1. Killing only the
            # monitor would not prove that all driver descendants were reaped.
            try:
                self.child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                return False
        if self.guard is not None:
            fence = select.poll()
            fence.register(self.guard, select.POLLIN)
            if not fence.poll(0):
                return False
            os.close(self.guard)
            self.guard = None
        self.child = self.plan = None
        return True

    def _start(self, target, paths, allowed):
        with ExitStack() as stack:
            listeners, rules = [], []
            for access in paths:
                nets = [access.subnet] if not allowed else [
                    net if net.subnet_of(access.subnet) else access.subnet for net in allowed if net.overlaps(access.subnet)]
                if not nets or not access.interface or access.subnet.prefixlen == 0:
                    raise ValueError('unconfirmed_management_network')
                listeners.append(stack.enter_context(self.platform.listen(access)).fileno())
                rules.append([str(net) for net in nets])
            self.channel, child = socket.socketpair()
            with child:
                self.child = self.platform.launch(target[1], target[2], child)
            self.channel.settimeout(0.1)
            packet = json.dumps(rules).encode('ascii')
            if socket.send_fds(self.channel, [b'L'], listeners) != 1:
                raise OSError('partial_indi_setup')
            self.channel.sendall(len(packet).to_bytes(2, 'big') + packet)
            self.channel.setblocking(False)
            self.last_reply = self.last_healthy = self.clock()
            self.listeners = stack.pop_all()

    def sync(self, enabled, target, paths, allowed):
        plan = (target, tuple((p.address, p.interface, str(p.subnet)) for p in paths), tuple(allowed))
        desired = enabled and target and paths and self.platform
        if self.child is not None and (not desired or self.plan != plan):
            if not self.stop():
                return
            self.retry_at = 0
        if not desired:
            if not enabled:
                self.state = 'off'
            else:
                self.state = 'waiting' if not target else 'unavailable'
            return
        try:
            if self.child is None:
                if self.clock() < self.retry_at:
                    return
                self._start(target, paths, allowed)
                self.plan, self.state = plan, 'waiting'
            if self.child.poll() is not None or self.clock() - self.last_reply > 4:
                raise OSError('indi_unavailable')
            if self.guard is None:
                try:
                    marker, fds, flags, _ = socket.recv_fds(self.channel, 1, 1)
                except BlockingIOError:
                    return
                if marker != b'P' or len(fds) != 1 or flags & socket.MSG_CTRUNC:
                    for fd in fds:
                        os.close(fd)
                    raise OSError('invalid_indi_guard')
                self.guard = fds[0]
                # Driver launch requires this acknowledgement. The received
                # pidfd independently proves Linux namespace PID 1 is gone.
                self.channel.sendall(b'G')
            self.channel.send(b'.')
            try:
                reply = self.channel.recv(16)
            except BlockingIOError:
                return
            if not reply or any(byte not in b'01' for byte in reply):
                raise OSError('invalid_indi_health')
            self.last_reply = self.clock()
            self.state = 'ready' if reply.endswith(b'1') else 'unavailable'
            if self.state == 'ready':
                self.last_healthy = self.clock()
            elif self.clock() - self.last_healthy >= 15:
                raise OSError('indi_health_timeout')
            self.addresses = frozenset(p.address for p in paths) if self.state == 'ready' else frozenset()
        except (OSError, ValueError, KeyError, AttributeError):
            self.stop()
            self.retry_at = self.clock() + 5
