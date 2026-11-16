# Serial mounts API: proposed contract 1.3 extension

Status: **proposal, additive, implemented as management routes**. The served native
contract version is **unchanged** (1.2); `openapi-snapshot.json` is not regenerated
and no compatibility snapshot is frozen. The routes below exist today behind the
usual Bearer token (or the browser session) so a native client app can use them, but
they are not part of the frozen contract until the owner accepts this proposal.

## Why

An app should list what is plugged into the bridge, offer to declare it, declare
it, find the port to open and follow its state, without asking the user to open
the bridge web interface. The web interface and the app call the **same** routes.

## Compatibility

Everything is additive: new routes, new optional status fields (`serial_mounts`,
`serial_detected`, `declared_devices`) and two new event types. An app that does
not know them ignores them (unknown event types are skipped by cursor). Proposed
version string: `contract=1.3` in `/info` and the DNS-SD TXT, with the capability
`serial-mounts` added to `caps`. An older bridge simply lacks the routes (404) and
the capability; a newer bridge keeps serving 1.2 clients unchanged.

## Rights

Existing model, nothing improvised: reads need scope `read`; every write
(declare, rename, remove, identify) needs scope `admin` (the default for routes
not listed in `required_scope`), the Bearer rules, `If-Match` on the current
configuration revision, and the same-origin plus CSRF rules for browser calls.
A paired app today holds `read`, `connect`, `telemetry`, `camera`; it would
need `admin` to declare. **Proposed**: a narrower scope `mounts` for
declare/rename/remove/identify so an app is not given full administration. Not
implemented.

## Routes

| Method and path | Scope | Purpose |
| --- | --- | --- |
| `GET /devices` | read | every declared device with `kind`, `link_type`, `state`, `client_connected`, `endpoint`, plus `detected` |
| `GET /serial-devices` | read | adapters seen but **not declared**, with a `proposal` |
| `POST /serial-devices/identify` | admin | read-only identification of one detected adapter |
| `GET /serial-profiles` | read | profiles the bridge can relay |
| `GET /serial-mounts`, `GET /serial-mounts/{id}` | read | declared serial mounts with live state |
| `POST /serial-mounts` | admin | declare (nothing is relayed before) |
| `PATCH /serial-mounts/{id}` | admin | rename; other settings refused with `409 LINK_HELD` while a client holds the link |
| `DELETE /serial-mounts/{id}` | admin | remove; refused with `409 LINK_HELD` while a client holds the link |
| `POST /serial-mounts/{id}/identify` | admin | read-only identification; `409 LINK_HELD` while a client holds the link |

Removal while a client holds the link is **refused**: it is the safest rule (the
owner or the app disconnects first, which triggers the stop sequence). If the
mount is removed anyway by other means (configuration edit), the relay stops and
sends the stop sequence before it closes.

`link_type` is one of `wifi`, `serial_usb`, `usb` (camera). `kind` is `mount`,
`smart_telescope`, `camera` or `other`. `state` is `online`, `offline` or `unknown`.

## Events

`serial.detected` and `serial.removed`, with `instrument_id` an opaque `ser-<12 hex>`
derived from the adapter node (no path, no serial number), on the existing
`GET /events` channel. Clients react to them by re-reading `GET /serial-devices`.
The first scan after start sets the baseline and emits nothing.

## Examples

Detected adapter (`GET /serial-devices`):

```json
{"serial_devices": [{"node": "/dev/ttyUSB0", "stable_path": "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0",
  "stable_kind": "by-id", "by_path": "/dev/serial/by-path/platform-3f980000.usb-usb-0:1.2:1.0-port0",
  "vendor_id": "0403", "product_id": "6001", "manufacturer": "FTDI", "product": "FT232R USB UART", "serial_number": "A50285BI",
  "unique_by_id": true, "link_type": "serial_usb", "can_add": true, "identification": "identified",
  "identity": {"responding": true, "hand_control_version": "4.15", "model_code": 11, "model_name": "4/5 SE",
    "board_versions": {"azimuth": "5.14", "altitude": "5.14"}, "read_at": "2026-11-11T18:20:00+00:00", "source": "bridge_read"},
  "proposal": {"name": "NexStar 4/5 SE", "profile": "nexstar", "device": "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0",
    "tcp_port": 2000, "baud": 9600, "format": "8N1", "idle_timeout_seconds": 120,
    "declared_identity": {"model_code": 11, "model_name": "4/5 SE", "hand_control_version": "4.15"}}}],
 "api_version": "1", "config_revision": "…"}
```

Declare it (`POST /serial-mounts`, `If-Match: "<config_revision>"`); accepting the
proposal is enough, any field may be overridden:

```json
{"device": "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0", "name": "Garden 4SE"}
```

Answer `201`:

```json
{"serial_mount": {"id": "serial-1a2b3c4d", "name": "Garden 4SE", "profile": "nexstar", "kind": "mount", "link_type": "serial_usb",
  "device": "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0", "tcp_port": 2000, "baud": 9600, "format": "8N1",
  "idle_timeout_seconds": 120, "state": "idle", "adapter_present": true, "client": null, "identification": "identified",
  "identity_mismatch": false, "refused_clients": 0, "stops_sent": 0, "stop_pending": false,
  "client_setup": {"port": 2000, "transport": "tcp", "host": null, "preset": "Celestron (NexStar hand control, serial Wi-Fi bridge)"}},
 "api_version": "1", "config_revision": "…", "operation_id": "…"}
```

`host: null` means: use the bridge address of the network the app used to reach the
bridge (the `request_path` of `GET /info`).

Refusal while connected (`DELETE`): `409 {"error": {"code": "LINK_HELD", "message": "A client holds this mount. Disconnect it before removing the mount."}}`.

## Calls an app makes

1. `GET /info`, pair, token (as today). The capability `serial-mounts` (proposed) says the routes exist; until then a `404` on `GET /serial-devices` means "not supported".
2. `GET /serial-devices`, and again after each `serial.detected` / `serial.removed` event.
3. Show each device (`manufacturer`, `product`, `identification`, `identity.model_name`) and offer "add".
4. `GET /config` for the revision, then `POST /serial-mounts` with the proposal (and the name the user typed).
5. `GET /devices` (or `GET /serial-mounts/{id}`) for `state`, `adapter_present`, `client`, `tcp_port`.
6. Open the app's NexStar TCP connection to `<bridge address>:<tcp_port>`. Disconnect cleanly before removing the mount.

## Not in this proposal

Leases, night reservations and `instrument.*` events for serial mounts, and
listing them in `GET /instruments` (the instrument catalog), are deliberately left
for a later step, so the frozen catalog schema stays untouched.
