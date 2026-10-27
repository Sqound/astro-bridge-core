# Get started

[Documentation](README.md) / Get started

## Choose an installation

| Goal | Install |
| --- | --- |
| Explore without hardware | Run the demo below |
| Use a Raspberry Pi Zero W | [Raspberry implementation](https://github.com/Sqound/astro-bridge-implementations/blob/main/doc/raspberry.md) |
| Use a Linux PC | [Native Linux or Docker](https://github.com/Sqound/astro-bridge-implementations/tree/main/doc) |

## Run the demo

```sh
python3 -m wifi_bridge --demo --demo-radios 4
```

Open **http://127.0.0.1:8787**. Password: **`demo-telescope-2026`**. Configuration is stored in `.runtime/`; no real radios or telescope commands are used.

![Saved equipment in the demo](images/saved-equipment.png)

## Install a verified release

Download the wheel and `SHA256SUMS` from [the matching release](https://github.com/Sqound/astro-bridge-core/releases). Verify the checksum before installation:

```sh
# Linux; on macOS use: shasum -a 256 -c SHA256SUMS
sha256sum --ignore-missing -c SHA256SUMS
python3 scripts/install.py \
  --wheel astro_bridge_core-1.0.0-py3-none-any.whl \
  --sha256 THE_VERIFIED_WHEEL_SHA256 \
  --prefix "$HOME/.local/share/astro-bridge-1.0.0"
"$HOME/.local/share/astro-bridge-1.0.0/bin/astro-bridge" --demo
```

Use a new installation prefix. The core installer does not install drivers, host services or privileged networking; select a platform implementation for physical equipment.

## Connect physical equipment

1. Open Astro Bridge through its configured private-network address or **192.168.77.1** on its hotspot.
2. Sign in with the implementation's initial access code and change it in **Settings**.
3. Choose [Network mode](guides/network-modes.md).
4. [Add equipment](guides/add-equipment.md), then select **Connect**.
5. Open the manufacturer's app using the connection path described for that family.

Keep a working management connection while changing adapter assignments.
