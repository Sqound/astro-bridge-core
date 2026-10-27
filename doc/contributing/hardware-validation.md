# Validate support for a telescope or mount

[Documentation](../README.md) / Hardware contribution

Use a repeatable sequence and report exactly which model, firmware, app and topology were tested. Start with a [manifest](../plugins/README.md), then collect evidence in these stages.

## Test sequence

| Stage | Verify |
| --- | --- |
| Direct reference | Manufacturer app connects directly; ordinary control/preview work |
| Wi-Fi candidate | Prefix, security, address and documented defaults are correct |
| Identification | Read-only probe validates expected reply and physical identity |
| Manual bridge connection | Private-network and hotspot paths reach the intended instrument |
| Native app | Control, browsing and preview work through the bridge |
| Activity | Preview/browsing protects the connection; stopping transfer starts the release delay |
| App closed | Background exchanges do not continually restart that delay; unknown coverage stays protected |
| Automatic selection | Another app's supported search selects eligible equipment; test both directions |
| Recovery | Unavailable Wi-Fi, app restart, interruption and upgrade preserve configuration/protection |

Do not move a mount, interrupt guiding or replace an observing plan just to test discovery. Use a controlled setup and obtain agreement from everyone using the instrument.

## Measure activity in phases

1. Instrument powered on; native app closed.
2. Native app home screen only.
3. Live preview or a supported stored-content transfer.
4. Return to the app home screen.
5. Fully close the app and measure the quiet interval.
6. Search from another supported app and measure the switch.

Record the configured quiet window/cooldown and whether coverage is known. An autonomous acquisition may produce little network traffic; viewing metadata does not establish acquisition completion. Mount guiding needs its own explicit validation.

## Include in an issue or PR

- Core/build ID, deployment/OS, adapter chipset/driver and network mode.
- Equipment model/firmware, app version and phone OS.
- Expected behavior, actual result and the phase that reproduces it.
- Protocol reference and sanitized request/reply fixtures.
- Updated `validation` fields with tested models/checks and remaining limitations.

Remove personal names, locations, serials, SSIDs/MACs, passwords, API tokens, push endpoints and private certificates. Never attach an unreviewed private capture. Screenshots should use synthetic or anonymized data.

A manifest-only contribution is welcome even before hardware is available: label it `unverified`, supply protocol sources and leave unsupported behavior disabled.
