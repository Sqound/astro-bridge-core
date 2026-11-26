# Nearby devices: list and link before any setup

A paired app can show the telescopes and mounts that the bridge hears over Wi-Fi, before the owner declares them, and link one in a single request. The bridge recognizes the device type from its Wi-Fi network name, using the prefixes declared by the installed plugins (`wifi` in each plugin manifest). Unrecognized networks are never listed.

Recognition is by network name only. It is documented per plugin; a device type listed here is not a claim of hardware validation (see each plugin's `validation`).

## Radio constraint

One free Wi-Fi adapter connects **one device at a time**. A device that is switched off or out of range does not keep the adapter: an explicit `connect` for another device takes it, and so does a `connect` after the app released the previous device (see [Radio release](NATIVE-CLIENT.md#radio-release-additive-proposed-for-contract-13)). A device in use is never replaced silently: `connect` answers `409 NO_FREE_RADIO` with `error.holders` naming it, and the app may force after the user's confirmation. Listing keeps working while that adapter is busy: the bridge shows what it heard during the last ten minutes, with the age of each observation. Fresh scans happen every 45 seconds, only on an idle adapter. `free_radios` is the number of adapters allowed for instruments, other than the private network and hotspot ones; `free_radios_idle` is how many of them no enabled profile uses.

## Scopes

| Route | Scope |
| --- | --- |
| `GET /api/v1/instruments/nearby` | `read` |
| `POST /api/v1/instruments/nearby/{nearby_id}/link` | `link` |
| `DELETE /api/v1/instruments/{instrument_id}/link` | `link` |

`link` lets an app create a disabled profile for a recognized device the bridge heard, or remove a profile it created this way. It does not allow changing bridge settings, its Wi-Fi, the hotspot, any other profile, or choosing an address, network or adapter. It is not a separate pairing choice: every app paired with `connect` has it, including apps paired before 1.2.0, so no new pairing is needed and the frozen v1 pairing contract is unchanged. On a 1.1.0 bridge these routes answer 404: fall back to the declared instruments only.

## List

`GET /api/v1/instruments/nearby`

```json
{
  "devices": [
    {"nearby_id": "4f0c…32 hex", "kind": "vespera", "category": "telescope", "name": "Vespera",
     "label": "Vespera · vespera2-a1b2c3", "network_name": "vespera2-a1b2c3", "signal": 100, "band": "2.4",
     "last_seen_seconds": 12, "state": "in_range", "instrument_id": null, "password": "none",
     "linkable": true, "reason": null}
  ],
  "free_radios": 1, "free_radios_idle": 1, "rule": "ONE_DEVICE_PER_FREE_RADIO",
  "scan_age_seconds": 20, "scan_interval_seconds": 45, "api_version": "1"
}
```

- `state`: `in_range` (heard in the last 2 minutes), `recently_seen` (2 to 10 minutes), `linked` (a profile exists for this network; `instrument_id` names it).
- `password`: `none` (open network), `factory_default` (protected; the plugin's documented factory password is used unless the app gives one), `required` (the app must give it).
- `linkable` is false with `reason: "ADDRESS_UNKNOWN"` when the plugin has no factory address for that network name.
- Show `label` or `name`; `network_name` is the raw network name.

## Link

`POST /api/v1/instruments/nearby/{nearby_id}/link`, body `{}` or `{"password": "…", "name": "…"}` (both optional). Answers **201**:

```json
{"instrument_id": "scope-1a2b3c4d5e6f", "nearby_id": "4f0c…", "created": true, "api_version": "1"}
```

The profile is created **disabled**, with the recognized type, the observed network, the plugin's factory address and a free adapter. Connect it with the existing `POST /api/v1/instruments/{instrument_id}/connect` (scope `connect`), then read its state and endpoint as for any instrument. Linking again a device linked this way updates its password or name and answers 201 with `created: false`. The password is stored like any profile password; it is never logged and never returned.

Errors (`{"error": {"code", "message"}}`):

| Status | Code | Meaning |
| --- | --- | --- |
| 400 | `INVALID_FIELDS` | Another field than `password` or `name`, or a password for an open network |
| 400 | `INVALID_PASSWORD`, `INVALID_NAME`, `PASSWORD_REQUIRED` | Password of 8 to 63 bytes, name of 1 to 64 characters, password needed |
| 403 | `INSUFFICIENT_SCOPE` | The app was paired without `connect` |
| 404 | `NEARBY_DEVICE_NOT_FOUND` | Not heard in the last 10 minutes: list again |
| 409 | `ALREADY_CONFIGURED` | The owner already declared this device; use the instrument list |
| 409 | `NO_FREE_RADIO` | No adapter is allowed for instruments |
| 409 | `ADDRESS_UNKNOWN` | No factory address for this device |

## Unlink

`DELETE /api/v1/instruments/{instrument_id}/link` removes a profile created by linking. 404 `LINK_NOT_FOUND` for any other profile; 409 `INSTRUMENT_IN_USE` while it is enabled, connected or reserved: disconnect it first.

## Sequence for an app

1. `GET /instruments/nearby` and show `label`, signal and `state`, with the one-device-per-free-radio rule.
2. On the user's choice: `POST /instruments/nearby/{nearby_id}/link`, with the password if `password` is `required` (or if the factory password was changed).
3. `POST /instruments/{instrument_id}/connect`, then `GET /instruments/{instrument_id}` and `GET /instruments/{instrument_id}/endpoint` as for any instrument. If the answer or the operation carries `displaced`, tell the user which device gave up the adapter. On `409 NO_FREE_RADIO`, show `error.holders[0].name` and its `state`; after the user's confirmation, send `connect` again with `"force_replace": ["<its instrument_id>"]`.
4. When done: `POST /instruments/{instrument_id}/disconnect`, and optionally `DELETE /instruments/{instrument_id}/link`. After switching the device off on request, send `{"reason": "switched_off"}` with the disconnect so that the bridge stops retrying and reports `released.reason: "SWITCHED_OFF"` instead of a lost link.

## States of a linked device

| Catalog entry | Meaning |
| --- | --- |
| `link: connected` | Linked |
| `link: saved` or `available`, with `retry` | Lost without any request. The bridge retries by itself (15 s, doubling up to 300 s); the adapter goes to any explicit request for another device |
| `link: saved` or `available`, with `released.reason: SWITCHED_OFF` | Switched off on request. No retry, adapter free |
| `link: saved` or `available`, with `released.reason: RADIO_YIELDED` or `REPLACED_ON_REQUEST` | Its adapter went to the device named in `released.to`. No retry |
| `link: saved` or `available`, neither field | Saved, never linked or disconnected by the user |

`retry` and `released` are absent on a 1.1.0 bridge and whenever they do not apply. Not verified on hardware yet.

## Not recognized yet

Celestron Wi-Fi modules (networks named `Celestron-…`) have no plugin, so they are not listed. Adding one needs a reviewed plugin with the module's address and port checked on hardware.
