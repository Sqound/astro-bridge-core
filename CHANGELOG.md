# Changelog

All notable changes to Astro Bridge Core. Versions follow [Semantic Versioning](https://semver.org/).

## 1.1.0 — unreleased

### Added

- Native client contract 1.2 (`docs/NATIVE-CLIENT.md`, quick start, `contracts/native-client/v1/` replay scenarios and `tools/contract-mock.py` loopback mock): scoped pairing, endpoint inventory, leases and night reservations, retained events with catch-up, telemetry sessions and operation state.
- Owner-enabled, read-only instrument telemetry adapters (Seestar events, Vespera status, Alpaca) with retained sessions, export and deletion.
- Physically attached cameras served over ASCOM Alpaca (ZWO ASI, UVC, gPhoto, INDI bridge, loopback simulator), camera descriptor tables and conformance fixtures. No vendor SDK is bundled.
- Celestron NexStar hand controls over USB serial, relayed over TCP (one client per port, stop on lost link), and the proposed contract 1.3 serial extension (`docs/SERIAL-MOUNTS.md`).
- Station profiles, wired (Ethernet) management, readiness reporting and maker-key import with encrypted storage (no key is distributed).
- Seestar `scan_iscope` probe sent as compact JSON ending in CRLF.
- `SECURITY.md` and this changelog.

### Changed

- Version 1.1.0 of the wheel, installer and OpenAPI document; the wheel now packages nested application modules and camera tables.
- README: equipment table with separate "implemented", "tested in simulation" and "verified on hardware" columns, and a section on Astro Bridge Setup.

### Known limits

- Most families are implemented and simulated but only partly verified on hardware; see the README table. Documented is not verified.

## 1.0.1 — 2026-10-28

- Compact header with activity and account menus; installer and documentation fixes. See the [1.0.1 release](https://github.com/Sqound/astro-bridge-core/releases/tag/v1.0.1).

## 1.0.0 — 2026-10-28

- First public release.
