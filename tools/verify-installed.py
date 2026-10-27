#!/usr/bin/env python3
"""Install the wheel outside source and verify authenticated persistent demo state."""
import argparse
import hashlib
import http.cookiejar
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def exercise(url,hostname='release-check'):
    cookies=http.cookiejar.CookieJar();client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    def call(path,data=None,csrf=None):
        headers={'Content-Type':'application/json','Origin':url}
        if csrf:headers['X-CSRF-Token']=csrf
        request=urllib.request.Request(url+path,headers=headers,data=json.dumps(data).encode() if data is not None else None)
        with client.open(request,timeout=5) as response:return json.load(response)
    for _ in range(100):
        try:
            with client.open(url+'/',timeout=2) as response:
                assert b'Astro Bridge' in response.read()
            break
        except (OSError,urllib.error.URLError):time.sleep(.1)
    else:raise AssertionError('App did not become reachable')
    try: call('/api/status')
    except urllib.error.HTTPError as error: assert error.code==401
    else: raise AssertionError('Unauthenticated status leaked private configuration')
    session=call('/api/login',{'password':'demo-telescope-2026'});csrf=session['csrf']
    status=call('/api/status');assert status['demo'] and status['deployment']['platform']=='demo'
    config=status['config'];config['hostname']=hostname;config['admin_password']=''
    call('/api/config',config,csrf)
    assert call('/api/status')['config']['hostname']==hostname
    assert not call('/api/updates')['supported'],'Demo must not advertise OS OTA support'
    return hostname


def main():
    p=argparse.ArgumentParser();p.add_argument('--wheel',type=Path,required=True);args=p.parse_args()
    wheel=args.wheel.resolve();root=Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='astro-installed-') as folder:
        base=Path(folder);prefix=base/'venv';state=base/'state'
        subprocess.run([sys.executable,str(root/'scripts/install.py'),'--wheel',str(wheel),'--sha256',hashlib.sha256(wheel.read_bytes()).hexdigest(),'--prefix',str(prefix)],check=True,stdout=subprocess.DEVNULL)
        check=subprocess.check_output([str(prefix/'bin/python'),'-c','import wifi_bridge; print(wifi_bridge.__file__)'],cwd=base,text=True)
        assert str(prefix) in check and str(root) not in check,'Test imported the source checkout'
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        for cycle in range(2):
            child=subprocess.Popen([str(prefix/'bin/astro-bridge'),'--demo','--state-dir',str(state),'--port',str(port)],cwd=base,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                if cycle:
                    config=json.loads((state/'config.json').read_text());assert config['hostname']=='release-check'
                exercise('http://127.0.0.1:'+str(port))
            finally:
                child.terminate();_,error=child.communicate(timeout=20)
                if child.returncode:raise AssertionError(error.decode())
    print('PASS installed wheel, Host/Origin authentication, config write and restart persistence outside source')


if __name__=='__main__':main()
