# Authenticated local API

[Documentation](../README.md) / API

The machine-readable [OpenAPI 3.1 specification](../../wifi_bridge/static/openapi.json) is generated from the installed plugin registry. The running bridge also serves **`/api/v1/openapi.json`**.

## Authenticate

```http
POST /api/v1/auth/token
Content-Type: application/json

{"password": "YOUR_ACCESS_CODE"}
```

Store the returned token securely and send `Authorization: Bearer TOKEN`. Tokens are bound to the transport used to obtain them: authenticate again when moving from HTTP to HTTPS.

| Resource | Purpose |
| --- | --- |
| `/api/v1/info` | Product/version and API capabilities |
| `/api/v1/status` | Current connections, paths and observations |
| `/api/v1/plugins` | Installed hardware families and declared evidence |
| `/api/v1/telescopes` | Saved profiles |
| `/api/v1/settings` | Connection/switching settings |
| `/api/v1/discovery` | Explicit controlled discovery |
| `/api/v1/events` | Cursor-based connection events |
| `/api/v1/operations/{id}` | Asynchronous operation result |
| `/api/v1/system/updates` | Platform update status/actions |
| `/api/v1/system/https` | Cached secure-access state and explicit management jobs |

## Update safely

Use the current configuration revision in `If-Match` for conditional changes. Treat `202 Accepted` as a running operation, then poll its operation resource. A timeout does not authorize blindly replaying a mutation.

Revoke only the temporary token with **`DELETE /api/v1/auth/token`** when a client finishes. Global sign-out/revocation has broader session and notification consequences.

Browser requests use same-origin cookies and CSRF checks. Do not bypass Host/Origin validation or send access codes to another origin. HTTPS clients must trust the bridge's configured certificate; see [PWA setup](../guides/pwa-notifications.md).

Example clients: [JavaScript client](../../examples/astro-bridge-client.mjs). Secrets, private keys and stored Wi-Fi passwords are not normal API response fields.

## Native clients, observations and attached cameras

The [complete API guide](../../docs/API.md) and
[native-client contract](../../docs/NATIVE-CLIENT.md) cover scoped pairing,
endpoint inventory, leases, retained events, telemetry/session queries and
physically attached cameras. Replay scenarios are in `contracts/native-client/v1/`
and `python3 tools/contract-mock.py --list` lists the loopback-only fixtures.
The frozen v1 client contract remains additive.

Instrument telemetry is owner-enabled and read-only. Attached-camera services
operate only cameras physically attached to the bridge (or the explicitly
plugged loopback demo simulator). Generic code includes optional Linux reference
backends; host setup and SDK installation belong to implementations.

The owner-enabled maker-key fetch code is included in Core, alongside import,
encrypted storage and read-only listening. No authentication key, manufacturer
package or vendor binary is bundled. Physical acceptance and official ConformU
validation remain separate from portable fixtures.
