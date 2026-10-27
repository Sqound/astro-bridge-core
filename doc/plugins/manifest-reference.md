# Manifest reference

[Documentation](../README.md) / [Plugin tutorial](README.md) / Field reference

The [JSON schema](../../plugins/schema/manifest.schema.json) assists editors. `wifi_bridge/plugins.py` also validates cross-field and registry invariants. Unknown fields and arbitrary executable payloads are rejected.

## Identity and Wi-Fi

| Field | Contract |
| --- | --- |
| `schema_version` | Integer `1` |
| `id` | Stable lowercase identifier: `[a-z][a-z0-9_]{0,31}`; filename must match |
| `version` | Descriptor `major.minor.patch`, independent of core version |
| `name`, `description` | English display strings; at most 64 / 300 characters |
| `category` | `generic`, `telescope` or `mount` |
| `wifi` | Up to 16 prefix/suffix hints; case-insensitive matching |
| `wifi[].prefix` | At most 32 UTF-8 bytes |
| `wifi[].min_suffix` | `0` or `1` |
| `wifi[].suffix` | `any`, `alnum-hyphen` or `alnum-hyphen-underscore` |
| `default_address` | A suitable private IPv4 address, or `null` when variable |
| `onboarding` | Optional documented prefix-specific `target_ip` / factory `password` defaults |

`onboarding` accepts up to 16 rules. Password suggestions are 8–63 UTF-8 bytes; do not publish a user's password. A prefix rule applies only inside the already recognized family.

## Transport and adapters

| Field | Values and constraints |
| --- | --- |
| `relay` | `null`, or `{transport: tcp/udp, ports: [...]}`; 1–16 unique ports in 1–65535 |
| `identification` | `none`, `seestar_scan_iscope`, `zwo_model`, `synscan_version` |
| `discovery.engine` | `none`, `tcp_syn`, `seestar_scan_iscope`, `dwarf_ping`, `synscan_version` |
| `discovery.target_mode` | Only with `tcp_syn`: `original` or `proxy_or_original` |
| `activity` | New families and mounts: `protect_all` |
| `readiness` | `association`; approved Vespera path may use `vespera_tcp` |

`tcp_syn` requires TCP and at most four relay ports. The approved Seestar, DWARF and SynScan discovery engines require their exact transport/port sets. ZWO identification needs a compatible single TCP port. Vespera readiness requires TCP 8082.

Registry validation rejects ambiguous cross-family Wi-Fi prefixes, overlapping TCP demand listeners and duplicate native UDP search listeners. Existing IDs `generic`, `vespera`, `dwarf`, `seestar`, `zwo_mount`, `synscan` remain available.

## Evidence and references

| Field | Values |
| --- | --- |
| `validation.level` | `unverified`, `simulation`, `partial_hardware` |
| `validation.models` | Up to 16 exact tested model names |
| `validation.checks` | `wifi`, `identity`, `native_app`, `preview`, `switching`, `private_proxy`, `hotspot_proxy` |
| `validation.limitations` | `physical_pending`, `autonomous_pending`, `guiding_pending`, `first_discovery_unavailable`, `images_pending`, `switching_pending` |
| `references` | 1–8 title/HTTPS URL pairs linking protocol/default/evidence sources |

An established connection or successful model query does not prove guiding, camera preview or every manufacturer-app feature. Record each claim separately.

## New adapter boundary

A new adapter must provide bounded input/output, deadlines, interface binding, reply validation/correlation, safe failure and sanitized tests. Descriptor JSON cannot contain Python, shell commands, raw packet expressions, arbitrary query payloads or nftables fragments. Adapter support is a reviewed core change.
