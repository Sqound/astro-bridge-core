"""Disposable browser fixture: only an injected DemoNetwork, never host networking."""
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wifi_bridge import __main__ as application
from wifi_bridge.network import DemoNetwork


class WiredDemo(DemoNetwork):
    def wired_home(self):
        if json.loads(Path(os.environ['WIRED_FIXTURE']).read_text())['connected']:
            return {'name':'enp1s0','mac':'02:00:00:20:00:01','kind':'ethernet',
                    'address':'192.0.2.20/24','usb':False,'label':'enp1s0'}
        return None

    def address(self, device, timeout=20):
        return device['address'] if device.get('kind')=='ethernet' else super().address(device,timeout)

    def connected(self, role, device, config=None):
        if device and device.get('kind')=='ethernet':return role=='home' and device==self.wired_home()
        return super().connected(role,device,config)


if __name__=='__main__':
    application.DemoNetwork=WiredDemo
    application.main()
