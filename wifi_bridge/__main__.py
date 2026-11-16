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
from .cameras.runtime import CameraRuntime, management_snapshot
from .cameras.discovery import CameraDiscovery, platform_supported
from .cameras.registry import load_registry


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
    wired_home = getattr(net, 'wired_home', None)
    wired = wired_home() if not args.demo and wired_home else None
    if not devices and not wired:
        parser.error("No network interface available.")
    onboard = next((d for d in devices if not d["usb"]), devices[0] if devices else wired)
    store = Store(state_dir, onboard["mac"])
    bootstrap = ({"password": "12345678"} if args.demo else
                 json.loads(Path(args.bootstrap).read_text()))
    controller = Controller(store, net, bootstrap, demo=args.demo)
    if controller.camera_supported and platform_supported(args.demo, indi=True):
        from .cameras.indi_platform import LinuxIndi
        controller.indi_platform = LinuxIndi()
    controller.management_hosts = frozenset(h.lower() for h in args.allow_host)
    controller.update(devices=devices)
    port = args.port or (8787 if args.demo else 80)
    bind = args.bind or ("127.0.0.1" if args.demo else "0.0.0.0")
    server = WebServer((bind, port), controller)
    https = (HTTPSService(server, bind) if not args.demo and Path("/usr/libexec/astro-bridge-tls").is_file() else None)
    if https: https.start()
    worker = threading.Thread(target=controller.loop, daemon=True)
    worker.start()
    controller.link_collector.ping = not args.demo and os.environ.get("ASTRO_BRIDGE_LINK_PING", "1") != "0"
    controller.link_collector.start()
    controller.vespera_collector.start()
    controller.alpaca_collector.start()
    controller.seestar_collector.start()
    if args.demo: controller.demo_telemetry.start()
    camera_service = server.api.cameras.service() if controller.camera_supported else None
    controller.camera_runtime = controller.camera_discovery = None
    if camera_service is not None:
        controller.camera_runtime = CameraRuntime(camera_service, lambda: management_snapshot(controller))
        controller.camera_runtime.start()
        if not args.demo:
            controller.camera_discovery = CameraDiscovery(camera_service, load_registry(), supported=controller.camera_supported,
                                                          on_event=controller.camera_native.event)
            controller.camera_discovery.start()
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
        # A signal may interrupt service_actions while it owns a journal lock.
        # Never join workers or acquire controller/session locks in signal context.
        controller.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"Astro Bridge : http://{bind}:{port}" + (" · SIMULATION" if args.demo else ""), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        controller.stop.set()
        controller.serial_mounts.close()  # sends each held mount's stop sequence
        if args.demo: controller.demo_telemetry.close()
        controller.seestar_collector.close()
        controller.maker_keys.close()
        if controller.camera_discovery: controller.camera_discovery.close()
        if controller.camera_runtime: controller.camera_runtime.close()
        if camera_service: camera_service.close()
        if https: https.close()
        server.server_close()
        worker.join(timeout=45)
        controller.link_collector.close()
        controller.vespera_collector.close()
        controller.alpaca_collector.close()
        controller.telemetry_sessions.close()
        if discovery:
            discovery.join(timeout=3)
        if vespera: vespera.join(timeout=3)
        if dwarf: dwarf.join(timeout=3)
        for mount in mounts: mount.join(timeout=3)
        if wifi_observer:wifi_observer.join(timeout=3)
        if hasattr(net, "close"):
            with controller.net_lock: net.close()


if __name__ == "__main__":
    main()
