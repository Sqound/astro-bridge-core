# Astro Bridge native client integration

The native client contract is version 1, frozen at v1.0. A native client can start with discovery, pairing, the instrument catalog, managed connections and telemetry replay. Treat advertised capabilities as runtime availability. The Bridge source and each native client are maintained separately.

## First development run

From the Bridge checkout on the shared development machine, source the workspace `env.sh`. The replay server uses Python's standard library and binds to loopback.

```sh
python3 tools/contract-mock.py --list
python3 tools/contract-mock.py --scenario pair-and-token --port 8798
```

Point the development client to `http://127.0.0.1:8798` for this scenario. Restart the mock with another scenario to reset its cursor. For a stateful HTTP exercise, run the actual application with simulated radios and disposable state.

```sh
state_dir=$(mktemp -d "${TMPDIR%/}/astro-bridge-client.XXXXXX")
python3 -m wifi_bridge --demo --demo-radios 4 --state-dir "$state_dir" --port 8797
```

Read the [native client contract](NATIVE-CLIENT.md), the [OpenAPI specification](../wifi_bridge/static/openapi.json), the v1 replay fixtures, and the [JavaScript reference client](../examples/astro-bridge-client.mjs). Use the replay for deterministic CI and the demo for stateful flows. Neither exercises real radio discovery. The fixed addresses and credentials in the replay are fictional fixture data.

## Client behavior

1. Browse `_astrobridge._tcp` on the current network. Resolve the service, fetch `GET /api/v1/info`, and verify the advertised opaque ID against `device_id`. The fixed hotspot probe at `192.168.77.1` is appropriate only after joining the Bridge hotspot.
2. Pair with the owner's consent via `POST /api/v1/auth/token` and `POST /api/v1/auth/clients`. Store the paired credentials in protected storage and discard the pairing administrator token. Use only required scopes from `read`, `connect`, `telemetry`, and `camera`. Follow the contract's transport requirements before sending credentials.
3. Read `GET /api/v1/instruments`. Preserve the separate link and service states. Use the current revision and the owner-approved replacement list for `POST /api/v1/instruments/{id}/connect`. Handle asynchronous operations and use the endpoint returned by the Bridge. Renew and release leases as documented.
4. Resume events from the last processed cursor and handle gap or boot resets. Use telemetry catch-up where the bridge advertises it. Camera control uses the separately returned Alpaca endpoint and device number. Refresh discovery and the catalog when attachment or endpoint identity changes.

## Serial mounts (proposal)

A NexStar hand control wired to the bridge by a USB serial adapter is listed and declared through management routes the app can use without the web interface: `GET /api/v1/serial-devices`, `POST /api/v1/serial-mounts`, `GET /api/v1/devices`, and the events `serial.detected` and `serial.removed`. They are a proposed 1.3 addition and the served contract is still 1.2: see the [proposal](SERIAL-MOUNTS-CONTRACT-PROPOSAL.md) and [Serial mounts](SERIAL-MOUNTS.md).

## Bench coverage

A Raspberry Pi 5 bench image was verified with automatic activation of both Realtek Wi-Fi dongles, working Internet through the shared hotspot, and automatic discovery from a home Wi-Fi network in the macOS Setup app. An ASI220MM Mini returned real 16-bit Alpaca frames at 1920 × 1080 on direct USB and 640 × 480 on the tested hub. Two full-frame hub attempts failed during USB resets. The Pi 5 bench image is a diagnostic build. Use the replay and demo for native client development.

Mobile native discovery and full manufacturer, overnight and Pi 3 acceptance remain separate hardware checks. The Seestar keep-alive policy remains gated. No release is implied by this document.
