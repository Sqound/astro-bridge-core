import argparse
import json
import os
from pathlib import Path
import signal
import threading

from .config import Store
from .controller import Controller
from .network import DemoNetwork, LinuxNetwork
from .web import WebServer
from .tls import HTTPSService
from .discovery import SeestarDiscovery


def main():
    parser = argparse.ArgumentParser(description="Astro Bridge")
    parser.add_argument("--demo", action="store_true", help="Simulation without system commands")
    parser.add_argument("--demo-radios", type=int, choices=range(2, 9), default=2, help="Number of simulated Wi-Fi radios (demo only)")
    parser.add_argument("--state-dir")
    parser.add_argument("--port", type=int)
    parser.add_argument("--bind")
    parser.add_argument("--bootstrap", default="/etc/wifi-bridge/bootstrap.json", help="Private JSON file containing the initial password (real mode)")
    parser.add_argument("--allow-host", action="append", default=[], help="Additional exact HTTP management hostname or IP; repeatable")
    args = parser.parse_args()
    import ipaddress
    import re
    for host in args.allow_host:
        try: ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
                parser.error("Invalid management hostname")
    if not args.demo and (os.name != "posix" or not Path("/sys/class/net").exists() or os.geteuid() != 0):
        parser.error("Real mode requires Linux and root. On macOS use --demo.")
    state_dir = Path(args.state_dir or (".runtime" if args.demo else "/var/lib/wifi-bridge"))
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    net = DemoNetwork(args.demo_radios) if args.demo else LinuxNetwork(state_dir)
    devices = net.devices()
    if not devices:
        # Real mode must have a Wi-Fi device to derive a stable identity.
        parser.error("No Wi-Fi interface available. Check the built-in adapter.")
    onboard = next((d for d in devices if not d["usb"]), devices[0])
    store = Store(state_dir, onboard["mac"])
    bootstrap = ({"password": "demo-telescope-2026"} if args.demo else
                 json.loads(Path(args.bootstrap).read_text()))
    controller = Controller(store, net, bootstrap, demo=args.demo)
    controller.management_hosts = frozenset(h.lower() for h in args.allow_host)
    controller.update(devices=devices)
    port = args.port or (8787 if args.demo else 80)
    bind = args.bind or ("127.0.0.1" if args.demo else "0.0.0.0")
    server = WebServer((bind, port), controller)
    https = (HTTPSService(server, bind) if not args.demo and Path("/usr/libexec/astro-bridge-tls").is_file() else None)
    if https: https.start()
    worker = threading.Thread(target=controller.loop, daemon=True)
    worker.start()
    discovery = threading.Thread(target=SeestarDiscovery(controller).loop, daemon=True) if not args.demo else None
    if discovery:
        discovery.start()

    vespera = threading.Thread(target=controller.vespera_demand.loop, daemon=True) if not args.demo else None
    if vespera: vespera.start()
    dwarf = threading.Thread(target=controller.dwarf_demand.loop, daemon=True) if not args.demo else None
    if dwarf: dwarf.start()
    mounts = [threading.Thread(target=adapter.loop, daemon=True) for kind,adapter in controller.plugin_adapters.items() if kind not in ('vespera','dwarf')] if not args.demo else []
    for mount in mounts: mount.start()

    wifi_observer=threading.Thread(target=controller.candidates.loop,daemon=True)
    if wifi_observer:wifi_observer.start()

    def shutdown(*_):
        controller.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"Astro Bridge : http://{bind}:{port}" + (" · SIMULATION" if args.demo else ""), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        controller.stop.set()
        if https: https.close()
        server.server_close()
        worker.join(timeout=45)
        if discovery:
            discovery.join(timeout=3)
        if vespera: vespera.join(timeout=3)
        if dwarf: dwarf.join(timeout=3)
        for mount in mounts: mount.join(timeout=3)
        if wifi_observer:wifi_observer.join(timeout=3)


if __name__ == "__main__":
    main()
