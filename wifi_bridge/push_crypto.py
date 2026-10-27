"""Isolated Web Push worker. Established libraries own encryption and VAPID signing."""
import base64
import http.client
import hashlib
import ipaddress
import json
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def unb64(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def encrypt(private, subscription, payload):
    import http_ece
    from cryptography.hazmat.primitives.asymmetric import ec
    from py_vapid import Vapid02
    target = urlsplit(subscription['endpoint'])
    signer = Vapid02.from_pem(private.encode())
    headers = signer.sign({'aud': 'https://' + target.hostname, 'exp': int(time.time()) + 3600,
                           'sub': 'https://planastro.space'})
    content = http_ece.encrypt(json.dumps(payload, ensure_ascii=False).encode(),
                              private_key=ec.generate_private_key(ec.SECP256R1()),
                              dh=unb64(subscription['keys']['p256dh']),
                              auth_secret=unb64(subscription['keys']['auth']), version='aes128gcm')
    urgent=payload.get('category') in ('connections','switching','failures')
    topic=hashlib.sha256(str(payload.get('tag','astro-bridge-test')).encode()).hexdigest()[:32]
    headers.update({'Content-Encoding': 'aes128gcm', 'Content-Type': 'application/octet-stream',
                    'TTL': '30', 'Urgency': 'high' if urgent else 'normal', 'Topic': topic})
    return content, headers


def send(request):
    from .push import validate_subscription
    sub = validate_subscription(request['subscription'])
    target = urlsplit(sub['endpoint'])
    # Resolve once, reject every non-public result, then pin the TCP destination.
    # The enclosing subprocess deadline bounds DNS as well as TLS/HTTP work.
    addresses = socket.getaddrinfo(target.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Non-public destination')
    data, headers = encrypt(request['private_key'], sub, request['payload'])
    connection = http.client.HTTPSConnection(target.hostname, 443, timeout=5, context=ssl.create_default_context())
    def connect():
        family, kind, protocol, _, address = addresses[0]
        raw = socket.socket(family, kind, protocol)
        try:
            raw.settimeout(5); raw.connect(address)
            connection.sock = connection._context.wrap_socket(raw, server_hostname=target.hostname)
        except BaseException:
            raw.close(); raise
    connection.connect = connect
    try:
        connection.request('POST', target.path + ('?' + target.query if target.query else ''), data, headers)
        response = connection.getresponse()
        # Never follow redirects, read arbitrary provider bodies, or log endpoints.
        return {'status': response.status}
    finally:
        connection.close()


def handle(value):
    import http_ece
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid02
    action = value.get('action')
    if action == 'probe':
        result = {'available': True}
    elif action == 'keygen':
        key = Vapid02(); key.generate_keys()
        result = {'private_key': key.private_key.private_bytes(serialization.Encoding.PEM,
                  serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
                  'public_key': b64(key.public_key.public_bytes(serialization.Encoding.X962,
                  serialization.PublicFormat.UncompressedPoint))}
    elif action == 'send':
        result = send(value)
    else:
        raise ValueError('Unknown action')
    return result


def main():
    if sys.argv[1:]==['--serve']:
        handle({'action':'probe'})  # Import optional crypto once; no keys are minted.
        while True:
            raw=sys.stdin.buffer.readline(32769)
            if not raw:return
            if len(raw)>32768 or not raw.endswith(b'\n'):raise ValueError('Invalid request')
            try:result=handle(json.loads(raw))
            except Exception:result={'available':False,'code':'PUSH_DELIVERY_FAILED'}
            print(json.dumps(result),flush=True)
            del raw,result
    else:
        print(json.dumps(handle(json.loads(sys.stdin.buffer.read(32769)))))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # No key, endpoint, HTTP body, traceback or device data crosses the boundary.
        print('{"available":false,"code":"PUSH_DELIVERY_FAILED"}')
        sys.exit(1)
