# Declarative instrument plugins

Astro Bridge 0.6.33 describes its instrument families using versioned JSON
manifests. Settings → Plugins lists the installed families, capabilities,
configured/connected profile counts and declared test coverage. One information
button opens details; technical adapter IDs stay in a collapsed section.

A plugin is a supported family/protocol description. A saved profile is one
physical device, with its own Wi-Fi credentials, target address and radio.
Installing a plugin does not connect an instrument or enable automatic switching.
The inventory has no disable/remove controls that could orphan saved connections.

## Files and build

Physically attached camera hints are a separate reviewed `cameras/*.json`
registry, generated into the installed `wifi_bridge/cameras/tables` bundle by
`tools/build-cameras.py`. USB device/interface classes are hints; exact reviewed
vendor/product entries take precedence and network adapters are not cameras.
Tables neither install drivers/SDKs nor authorize opening devices. See
USB discovery and deployment, owner-installed ZWO ASI and
UVC owner calibration and capture limits. An installed UVC
factory does not establish calibrated or physically verified camera support.
Instrument Wi-Fi plugin behavior
and the read-only observation boundary are unchanged by camera discovery.

- `plugins/<id>.json`: one source manifest per family.
- `plugins/schema/manifest.schema.json`: generated editor schema.
- `wifi_bridge/plugins.py`: bounded standard-library validation and registry.
- `wifi_bridge/plugin_runtime.py`: adapter selection; no dynamic Python imports.
- `wifi_bridge/static/plugins.json`: generated bundle compatible with the existing
  app-only OTA installer and Python package data.
- Authenticated `GET /api/v1/plugins` / browser `GET /api/plugins`: inventory.

Run from the checkout:

```sh
python3 tools/build-plugins.py
python3 tools/build-i18n.py
python3 scripts/generate_openapi.py
python3 tools/build-plugins.py --check
python3 -m unittest discover -s tests
```

Packaging refuses a stale generated catalog. CI checks reproducibility. The
runtime loads the installed catalog at startup, so plugin changes are delivered
with reviewed application updates. Initial support does not include a live plugin
store, arbitrary file upload or executable third-party code.

## What the manifest controls

| Field | Purpose |
| --- | --- |
| `schema_version`, `id`, `version` | Descriptor compatibility, stable saved-profile type and plugin version. |
| `name`, `description`, `category` | UI name, purpose and generic/telescope/mount category. |
| `wifi` | Case-insensitive prefix and bounded suffix rules; a family hint only. |
| `default_address` | Suggested private IPv4 address; the owner's profile can override it. |
| `relay` | TCP or UDP and exact ports; null for direct routing without a native proxy. |
| `identification` | Approved read-only identity adapter, or `none`. |
| `discovery` | Approved app-demand adapter and optional TCP target mode. |
| `activity` | Reviewed traffic classifier; new families use `protect_all`. |
| `readiness` | Association/routing readiness, or the existing Vespera TCP handshake. |
| `validation` | Declared scope, exact tested models/checks and explicit untested work. |
| `references` | Protocol documentation and evidence links. |
| `telemetry` (optional) | Approved observation adapter descriptors and default adapter IDs; absent means empty lists. Metadata only, subject to owner opt-in per profile. |
| `maker_key` (optional) | Supported family, algorithm and acquisition source IDs; never key material. Currently Seestar only. |
| `protocol` (optional) | Driver protocol family a native client may choose from: `seestar`, `vespera`, `dwarf`, `lx200` or `synscan`. Absent means none (null in the instrument catalog). |
| `port_roles` (optional) | One `{port, role, protocol}` entry per relayed port: `role` is `control`, `preview`, `imaging`, `rest`, `stream` or `other`; `protocol` is `json_rpc`, `binary`, `http`, `socketio`, `websocket`, `rtsp`, `lx200`, `synscan`, `alpaca`, `indi` or `unknown`. Metadata only; it never opens, forwards or probes a port. Undeclared ports are reported as `other`/`unknown`. |
| `onboarding` (optional) | Up to16 SSID-prefix rules with documented factory `password` and/or `target_ip` defaults. Longest prefix wins inside the recognized family. |

Known IDs remain `generic`, `vespera`, `dwarf`, `seestar`, `zwo_mount`, `synscan`.
Existing credentials, profiles, legacy proxy aliases and automatic-policy keys
remain unchanged. Startup refuses a registry that would orphan a saved type or
policy. Plugin IDs must not be renamed or removed without a configuration migration.

The registry drives profile validation, Wi-Fi hints, presets, native relay ports,
exclusive proxy ownership, activity adapter selection, automatic policy choices,
new TCP demand adapters, the settings UI and generated OpenAPI enums. The native
protocol implementations remain reviewed code. This is a functional registry,
not a second catalog describing an unrelated hard-coded device list.

## Add a family using an existing transport

For a simple native TCP device, add a manifest like this fictional example. Replace
all placeholders with actual documented values before submitting it:

```json
{
  "schema_version": 1,
  "id": "example_mount",
  "version": "1.0.0",
  "name": "Example mount",
  "description": "Connect Example Wi-Fi mounts.",
  "category": "mount",
  "wifi": [{"prefix": "Example_", "min_suffix": 1, "suffix": "any"}],
  "default_address": "192.168.5.1",
  "relay": {"transport": "tcp", "ports": [12345]},
  "identification": "none",
  "discovery": {"engine": "tcp_syn", "target_mode": "proxy_or_original"},
  "activity": "protect_all",
  "readiness": "association",
  "validation": {
    "level": "unverified",
    "models": [],
    "checks": [],
    "limitations": ["physical_pending", "guiding_pending"]
  },
  "references": [{"title": "Replace with manufacturer protocol", "url": "https://manufacturer.example/protocol"}]
}
```

This creates a selectable type, Wi-Fi suggestion, preset, TCP proxy, guarded
connection-demand capability, automatic-policy checkbox and inventory card. A UDP
relay can use `discovery: {"engine": "none"}` until an approved search adapter
exists. Identity `none` never invents a device model or serial.

A new model of an existing protocol family usually extends that family's Wi-Fi
hints and evidence instead of duplicating its plugin. Overlapping family hints,
TCP demand ports and duplicate native UDP discovery listeners are rejected as
ambiguous. Devices sharing factory addresses still require separate use under
the current subnet rules; a manifest does not add network namespaces.

## Adapter boundary

Available identification adapters: `none`, `seestar_scan_iscope`, `zwo_model`,
`synscan_version`. Discovery adapters: `none`, `tcp_syn`, `seestar_scan_iscope`,
`dwarf_ping`, `synscan_version`. Their code sets query formats, bounded reads,
interface binding, reply correlation and filtering. A new protocol needs a small
reviewed adapter and its fixtures; the manifest cannot provide shell commands,
Python, packet expressions, raw firewall fragments or arbitrary probe payloads.

The DWARF adapter stays passive and does not send discovery replies or synthesized
Wi-Fi settings. The SynScan adapter retains its scoped local-reply conntrack fix.
The controller computes proxy conflicts from transport/port ownership, including
Vespera/DWARF overlap. The core keeps locks, leases, management reservations,
freshness, ambiguity, release/cooldown, attempt budgets and guarded restoration.
A manifest cannot override these rules. All mount traffic remains protected;
new families cannot opt into an existing background-traffic exemption.

## Observation descriptors (schema version 1)

These additive optional fields do not change routing, start adapters, enable a
profile or fetch credentials. They follow the [observation and device-service
principle](ARCHITECTURE.md#observation-and-device-services): read-only, allowlisted,
local, owner-enabled per profile and no state-changing commands.

`telemetry` has required lists `adapters` and `default_enabled`, each bounded by
the approved IDs in `wifi_bridge/plugins.py`. Empty lists are valid; omitting the
block normalizes to `{"adapters": [], "default_enabled": []}`. Unknown IDs,
duplicate IDs, unknown fields and defaults absent from `adapters` are rejected.
An adapter is an ID string, or the explicit keyed-listener descriptor below:

```json
{
  "telemetry": {
    "adapters": ["link_v1", "alpaca_observer_v1", {"id": "seestar_events_v1", "requires": "maker_key"}],
    "default_enabled": []
  },
  "maker_key": {
    "family": "seestar",
    "algorithm": "rsa-pkcs1v15-sha1",
    "sources": ["maker_package", "import"]
  }
}
```

Approved IDs are `link_v1`, `vespera_status_v1`, `alpaca_observer_v1` and
`seestar_events_v1`. The last must use the object form with `requires: "maker_key"`
and a matching `maker_key` block; it cannot be default-enabled. `maker_key` accepts
only the exact Seestar family/algorithm/source descriptor shown above, and only
on the Seestar manifest. It carries no credential or download URL. Sources describe
owner-triggered maker-package acquisition or explicit import.
No manufacturer authentication occurs without the owner's supplied key, and that
key may only authenticate listening. A one-time owner-triggered download from
approved maker hosts is the sole outbound exception for these services.

All six built-in families declare `link_v1`. Vespera also declares
`vespera_status_v1` in both lists; Seestar adds the two adapters shown above with
neither enabled by default. DWARF, ZWO mounts, SynScan and generic have only
`link_v1` and empty defaults. These are capability/default declarations for later
work, not proof of implemented or physically validated telemetry. A future setup
must obtain owner opt-in before applying defaults; installing/upgrading a manifest
does not grant consent. Client applications consume the same generic descriptors.

## Evidence and contribution

Use `unverified` before testing. `simulation` means fixtures/isolated packets,
not hardware acceptance. `partial_hardware` requires named models and the checks
actually performed. For example AM5N has identification/private-proxy evidence,
while SynScan still awaits physical acceptance; neither claims validated guiding.
The inventory presents this as **declared test coverage**. Review the supporting
evidence before accepting a community claim.

Add sanitized request/reply fixtures, a protocol source, regression tests and the
[hardware test matrix](ADDING-HARDWARE.md). A synthetic extra-manifest test proves
that types/proxies/policies/operations and the UI can grow without core edits.
Real Linux tests retain original routing, SynScan same-socket discovery, activity
continuity and isolation. Simulation does not replace testing the native app.

Names/descriptions are English source strings, translated through the existing
catalogs; new community text falls back to English until translations are added.
Shared inventory labels are translated in all ten UI languages. Avoid personal
SSIDs, credentials, tokens and raw private captures in manifests or PRs. Submit a
[draft pull request](https://github.com/Sqound/astro-bridge-core/pulls) with the manifest,
rebuilt bundle/contract, fixtures and an honest validation scope.

## Two-step Wi-Fi onboarding defaults (0.6.34)

The scan assistant filters recognized Wi-Fi families, opens a prefilled profile
and keeps advanced/binding controls collapsed. Add `onboarding` only for values
supported by a manufacturer source, for example:

```json
"onboarding": [{"prefix": "Example_", "target_ip": "192.168.5.1", "password": "documented-default"}]
```

An empty prefix applies to the whole recognized family. A rule never matches an
unrelated family's Wi-Fi, runs code or reads the owner's saved password. Changed
passwords remain editable; unknown model addresses require explicit input. Keep
`default_address` null when it varies, and override only documented model prefixes.
Seestar and AMH use their documented initial12345678 password. DWARF mini and3
use model-specific rules for192.168.88.1 and DWARF_12345678; other DWARF names retain
an unknown address. Open-network status comes from observed Wi-Fi security.

Sources: [Seestar factory Wi-Fi](https://us.seestar.com/blogs/tutorial/simulated-stargazing),
[ZWO AM5 manual](https://i.zwoastro.com/wp-content/uploads/2025/04/15556de1aaee99918a91a57953f52635.pdf),
[DWARF mini file access](https://help.dwarflab.com/en/docs/help-doc-1),
[DWARF3 network guide](https://help.dwarflab.com/en/docs/Trouble-Shooting-Guide-For-DWARF-3-Users?product=dwarf-3).
Scanning uses [NetworkManager's documented list/rescan options](https://networkmanager.pages.freedesktop.org/NetworkManager/NetworkManager/nmcli.html).
New unsaved recognized observations can notify and open a retained authenticated
prefill; detection never creates a profile or changes connections automatically.


## Service readiness adapters

`readiness` accepts only curated adapter IDs: `association` (DWARF/generic,
no service probe), `vespera_tcp` (the existing 8082 check), `tcp_connect`
(ZWO mounts 4030 and Seestar 4700), and `udp_identity` (SynScan read-only version
exchange, UDP 11880). The validator checks the relay transport and approved identification adapter.
`tcp_connect` checks the first declared relay port, so a compatible community
family can reuse it without a core edit; manifests cannot supply probe payloads. New probes only report independent live `service` metadata and do not
change existing association/forwarding readiness. They are throttled and skipped
while relay activity is active; Vespera retains its existing behavior. See
[the API service state](API.md) for cache, failure, event and hardware boundaries.

### Alpaca observer implementation

`alpaca_observer_v1` is now implemented and opt-in for all bundled families except
DWARF. The generic family permits any Alpaca instrument on its assigned network;
no manufacturer-specific fallback or key is needed. Existing defaults are
unchanged. DWARF remains `link_v1` only, without an active observer connection.
See [the API policy and member allowlist](API.md#alpaca-instrument-observations).

### Owner-supplied maker keys

The `maker_key` descriptor now has an administrator service and a Seestar profile
editor section. It ships no credential and does not acquire one automatically.
One encrypted owner key is shared across the family's profiles. Public instrument
capabilities report absence/unverified/verified/refused/storage-unavailable, without
fingerprints or raw material. Acquisition and a fresh handshake are explicit owner
actions; no observer is enabled. `seestar_events_v1` is described under authenticated Seestar events below. The
maker-host/member fetch module stays private until its publication location is decided. No actual maker package or hardware is used in automated tests.

### Authenticated Seestar events

`seestar_events_v1` now listens with the supplied owner key. Supplying a key enables
only profiles without an explicit session-data selection; saved opt-outs remain
in force. The Session data switch is per profile. The Seestar plugin chip says
“Session: full (with your key)” when a usable key exists, otherwise “Session:
position only”; this describes capability, not verified event delivery. The
information dialog states the hardware validation limits. On isolated profiles,
selected authenticated listening has priority over Alpaca, whose effective
capability is unavailable until that priority ends; neither saved choice is lost.
No maker key ships with the plugin and no keep-alive method is enabled by default.

### Session-card limitation metadata

`telemetry.limitation` is optional printable English source text, bounded to240
characters. The UI translates it through the normal catalogs on cards and in
information dialogs. It cannot activate observation or introduce an adapter.
Absent metadata uses the generic read-only explanation. Capability chips derive
from the declared adapter IDs and current public maker-key state; they describe
capability, not verified delivery. All existing plugin fields remain compatible.

Camera manifests may declare the curated `indi_driver` identifier. The reviewed
mapping pairs `zwo_asi` with `indi_asi_ccd` on exact ZWO USB IDs. Manifest and
platform validation exclude remote-driver syntax, arbitrary programs, paths
and arguments. Other camera descriptors continue to use their existing drivers.

### Port roles (native contract 1.1)

`port_roles` lets a native client pick a port by purpose instead of guessing from
a number. Each entry must name a port of `relay.ports`, at most once. The core
families declare:

| Family | Port roles |
| --- | --- |
| `seestar` | 4700 `control`/`json_rpc`, 4800 `imaging`/`binary`, 4554 and 4555 `preview`/`rtsp`; 4801 and 4804 stay `other`/`unknown` (not attributed by evidence) |
| `vespera` | 8082 `rest`/`http`, 8083 `stream`/`socketio` |
| `dwarf` | 9900 `control`/`websocket`, 8082 `rest`/`http`, 554 `preview`/`rtsp` |
| `zwo_mount` | 4030 `control`/`lx200` |
| `synscan` | 11880 `control`/`synscan` |

A role is a declaration from public protocol evidence, not proof that the port
answers or that an application accepts a connection. Service readiness remains
the separate `service`/`service_state` measurement.
