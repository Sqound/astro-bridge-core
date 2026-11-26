# Native client contract v1

## Version 1.0 (frozen 2026-11-04)

This is the generic native application contract, version **1**, frozen on
2026-11-04. It is for any authorized client. The client
discovers a bridge, pairs once, chooses an instrument by ID, and uses the endpoint
the bridge returns. It never asks a person to type an instrument address and never
constructs one from a model, target address or remembered port.

**Specification is not availability.** The bridge supplies service state, pairing,
the instrument facade, long polling/new events and telemetry
catch-up. Physically attached cameras are included in the catalog. Discovery/identity publication and
endpoint planning are implemented. `STATUS.json`
records scenario execution evidence, not hardware acceptance. All 23
scenarios pass whole flows against the real HTTP application with disposable
DemoNetwork state. This is not hardware acceptance.

The [OpenAPI document](../wifi_bridge/static/openapi.json) tags every HTTP operation
here `native-client`. `x-contract-status` is `implemented` or `planned` for this
contract. The `/info` identity extension is implemented. Existing
`/events` and `/operations/{id}` keep their implemented legacy behavior, marked
`x-existing-route-implemented`. Native operation projections are implemented;
event extensions and long polling are implemented. Administrator
and paired-client `/auth/token` login and pairing are implemented.
Telemetry catch-up is implemented on native and cookie routes. Event waiting
is implemented on both the native and browser routes.

## Existing semantics and terminology

[API.md](API.md), explicit ensure,
connection contract,
protection, and
isolated endpoints remain the implementation base.
There is one operation manager, event journal, revision mechanism and protection
engine. The new instrument facade must reuse them rather than create a parallel
scheduler or weaken protection.

| Brief term | Contract spelling / compatibility decision |
| --- | --- |
| Login `token` | Existing `access_token`, with `token_type: "Bearer"` |
| Operation connecting / ready | Existing `state: "running"` / `"succeeded"`; failure stays `"failed"`; succeeded must carry a current endpoint |
| Event sequence / cursor | Existing `sequence`, `latest_sequence`, `next_cursor`, `oldest_sequence`, `has_more`, `gap`, `reset_reason` |
| Operation instrument ID | Existing `profile_id` remains the stable instrument ID in operation records; the instrument facade uses `instrument_id` |
| Lease facade | `lease_id` identifies the same protected reservation; atomic renewal uses the protection engine's current record version internally |

The existing administrator lease API keeps its 30–900 second TTL and record-version
checks. The implemented client facade accepts 60–3600 seconds and preserves existing owner locks, foreign-client reservations and
atomic version checks. Legacy ensure
continues to require its existing automatic-allocation consents. Explicit paired
client connect has the different owner authorization described below.

All requests and responses are JSON. Request bodies are bounded to 32 KiB, unknown
fields are rejected, and browser Origin/CSRF policy is unchanged. Errors use
`{"error":{"code":"CODE","message":"Human-readable message"}}`, with documented
error-specific details **inside `error`**. Clients branch on codes, not messages.
Responses are `Cache-Control: no-store`. Tokens are transport-bound as in API.md;
HTTPS still requires explicit local certificate trust. No redirects carry secrets.

The bridge supplies independent live `connection.service` metadata on the existing
status/profile resources, as documented in [API.md](API.md). The instrument
mapper must consume `row['service']['state']`, preserving a connected link with
`service:"not_answering"`; it must not infer service availability from
`relay_ready`. The implemented instrument projection exposes its scalar `service` enum. The legacy
`service_changed` event type is the only addition for changed new-probe states; native events and long polling. Check sequences are process-local evidence,
not journal cursors. Real mount response timing remains a hardware check.

The `udp_endpoint_ownership` capability is part of the implemented legacy `/info` capability
list, reflected in `current-info.json`. Isolated UDP live endpoint metadata now
reports a shared controller owner across management paths, independently of the
reservation lease. Administrator release uses the new legacy management route
`POST /endpoints/{profile_id}/release-owner` with the actual revision; see
[API.md](API.md#isolated-udp-endpoint-owner). Its acquisition `since_seq` is local
to the UDP owner instance, not the instrument/event `since_seq` cursor. The native instrument facade and its endpoint/reservation scenarios are implemented.

## A. Discovery and identity

Discover DNS-SD `_astrobridge._tcp`, port **80**. Its instance name is the
owner-visible bridge name. Publish on confirmed management interfaces (home and
the bridge's hotspot), never an instrument radio. Keep `_http._tcp` and
`_smartbridge._tcp` advertisements for compatibility.

| TXT key | Value |
| --- | --- |
| `api` | `/api/v1` |
| `api-version` | `1` |
| `contract` | `1` |
| `product` | `astro-bridge` |
| `version` | Application version |
| `id` | Stable opaque 16 lowercase hexadecimal characters, derived from a random installation secret; never a MAC or serial |
| `name` | UTF-8 display name, at most 63 bytes |
| `caps` | Comma-separated enabled capabilities: `pairing,instruments,connect,events-wait,telemetry,alpaca-camera,maker-keys`; an empty list is allowed |
| `https` | HTTPS port, only while local HTTPS is enabled |

Each TXT string is at most 255 bytes; the whole TXT record is at most 1300 bytes,
including each string's length octet. Capability advertisement is a runtime
availability claim, never inferred from this document or a plugin descriptor.

When already joined to an `astro-bridge-*` network, a client without mDNS may probe
`http://192.168.77.1/api/v1/info`. This is the fixed product hotspot address, not an
instrument address. An arbitrary home network does not authorize this fallback.

Public `GET /api/v1/info` adds `contract_version:1`, `device_id`, `name`,
`capabilities[]` (the same enabled list as TXT `caps`),
`request_path:"home"|"hotspot"|"other"`, and
`pairing:{available:bool,method:"admin_password"}`. `request_path` is the actual
management ingress path, not a client-controlled query/header hint. Retain the
existing product, application version and API version fields.

A hostname-only settings change reloads the new instance/display name and TXT
immediately. Avahi does not reread its daemon configuration on `--reload`, so the
new `.local` hostname is effective only after the next Avahi restart (management
interface change or an explicit owner service restart). This known limitation
preserves the rule that automatic restart occurs only when interfaces change.

Worked flow: [discovery-txt.json](../contracts/native-client/v1/discovery-txt.json)
provides synthetic minimal/full/hotspot TXT inputs for the currently enabled
`pairing,instruments,connect,events-wait` runtime set. These inputs are not captured mDNS packets. Resolve its service, call public `/info`,
and compare TXT `id` with `device_id` (`0123456789abcdef` in the fixtures). Stop
pairing on mismatch. [info-public](../contracts/native-client/v1/info-public.json)
is the public identity response. The real HTTP runner uses its actual loopback socket (`request_path:"other"`);
separate HTTP tests map the socket's local destination to synthetic home/hotspot
management snapshots. TXT runtime variation and optional HTTPS are tested in
`test_announce.py`. Native `/info` fields coexist with retained authentication,
token lifetime and OpenAPI metadata; old feature flags are `legacy_capabilities`.
Platform mDNS and local-network permission behavior remain documented, not
verified here; see the discovery acceptance procedure.

## B. Pairing and authentication

1. Obtain the current administrator password from the owner and call existing
   `POST /auth/token {password}`. Keep its administrator token only for pairing.
2. With that Bearer token, `POST /auth/clients {name,scopes}` returns **201**
   `{client_id,client_secret,scopes,device_id}`. `name` is 1–48 characters.
   `client_secret` is shown once; use platform-protected storage, never a log.
3. Discard the administrator token. `POST /auth/token {client_id,client_secret}`
   returns `{access_token,token_type:"Bearer",expires_in:43200,scopes}`.

Scopes are `read`, `connect`, `telemetry`, `camera`, `admin`. The suggested default
is `read,connect,telemetry,camera`; do not request administrator access by default.
Client secrets survive service restarts; tokens remain memory-only and last
12 hours, or until revoked/restarted. Revoked credentials return **401
INVALID_CLIENT**; pair again with owner involvement rather than retrying a revoked
secret. The bridge's existing login/session limits and 429 behavior remain.
Password changes invalidate current tokens but preserve paired credentials;
administrator revoke-all removes every paired credential. Admin clients can list
`GET /auth/clients` and revoke `DELETE /auth/clients/{id}` using the actual quoted
`config_revision` from the list as `If-Match`. See [scope requirements](API.md#paired-apps-and-scopes).

Worked flow: [pair-and-token](../contracts/native-client/v1/pair-and-token.json)
logs in with the documented fixture placeholder `test-password-1`, creates a
client with `test-token-2` as its synthetic secret, then logs in as that client.
[token-revoked-repair](../contracts/native-client/v1/token-revoked-repair.json)
shows 401 followed by a new pairing. These strings are test data, not credentials
for an appliance. The pairing, revoked-credential repair, current login, info and short-event-poll scenarios use the real demo
application with disposable state and generated tokens/boot IDs.

## C. Instrument catalog

`GET /instruments` requires `read` and returns:

```json
{
  "config_revision": "revision-1",
  "boot_id": "00000000000000000000000000000000",
  "latest_event_seq": 10,
  "instruments": [{
    "id": "tel-seestar-1", "kind": "smart_telescope", "family": "seestar",
    "name": "Test telescope", "link": "connected", "service": "ready",
    "since_seq": 10, "reason": null, "client_connect": true, "blockers": [],
    "lease": {"held": false, "mine": false, "expires_in": null},
    "capabilities": {"telemetry": ["link_v1"], "maker_key": "absent"}
  }]
}
```

`GET /instruments/{id}` returns one instrument in the same shape. `kind` is
`smart_telescope|mount|camera|other`; `family` is the plugin ID or camera driver ID. `link` is
`connected|connecting|available|saved|disabled`; `service` is independently
`ready|not_answering|not_probed|unknown`. Association is not service readiness.
`reason` is a machine code or null; `blockers` is an array of codes. `since_seq`
locates the most recent state change in the existing event sequence.

`client_connect` records owner permission. Existing and new profiles default to
true; saving the profile persists it. `enabled` remains the existing desired
association flag: an inactive saved profile can still be `saved` or `available`.
An observed connected link stays `connected` even after permission is disabled.
For an unconnected profile, disabled permission yields `disabled`; a running
connection operation yields `connecting`. `available` requires a matching fresh
non-disruptive spare-radio observation less than 120 seconds old; cached scan
results and persisted observations never establish current availability.

The lease summary distinguishes any
holder from this client (`held`, `mine`) and supplies remaining seconds or null.
`capabilities.telemetry` lists only implemented, available adapter IDs from the
owner's effective profile selection (currently `link_v1`); `maker_key` is
`absent|unverified|verified|refused|storage_unavailable|null`. Metadata never enables observation. Observation
requires owner consent and remains read-only. Physically attached cameras enter
this catalog and return their Alpaca service in their endpoint; this does
not operate a remote telescope's built-in camera.

Worked flow: [instruments-list](../contracts/native-client/v1/instruments-list.json)
reads six snapshots covering connected, connecting, available, saved, service not
answering and disabled-for-clients states, then fetches the selected ID. Clients
render both state dimensions and check blockers before offering connect.

## D. Connection and reservation

`POST /instruments/{id}/connect` requires `connect` scope, an actual quoted
`If-Match: "<config_revision>"`, and this body:

```json
{"lease_seconds":900,"allow_replace":["tel-vespera-1"],"wait_seconds":0}
```

All fields are optional: `lease_seconds` 60–3600, default 900; `allow_replace` is
an explicit list of instrument IDs, default empty; `wait_seconds` 0–25, default 0.
The caller's `connect` scope and the selected instrument's `client_connect:true`
(default true) authorize allocation of a **free** radio even with global
`auto_connect` off. They never authorize arbitrary eviction. Replacing a victim
requires its `auto_replaceable` and the victim's ID in `allow_replace`. Protection,
owner locks and another client's lease always win, including after confirmation.

| HTTP result | Meaning / next action |
| --- | --- |
| 200 | `{state:"ready",instrument_id,lease:{id,expires_in},endpoint}`; use the returned endpoint |
| 202 | `{state:"connecting",operation:{id,status_url},lease:{id,expires_in}}`; poll the existing operation |
| 409 `NO_FREE_RADIO` | `error.replaceable:[{instrument_id,name}]`; ask the user, then resend with only confirmed victims. Since 1.2.0 `error.holders` names the instruments that keep the radios (see [Radio release](#radio-release-additive-proposed-for-contract-13)) |
| 409 `INSTRUMENT_PROTECTED` | `error.reason` explains the protection; do not retry around it |
| 409 `LEASED_BY_OTHER_CLIENT` | Another client's reservation wins |
| 403 `CLIENT_CONNECT_DISABLED` | Owner disabled this client action |
| 404 | Unknown instrument |
| 412 / 428 | Stale / missing or wildcard revision; re-read instruments before another action |
| 429 `CONNECT_LIMITED` | Apply backoff; existing bounded connection admission limits remain |

`GET /operations/{id}` preserves `running|succeeded|failed`; `succeeded` with a
current non-null endpoint is the brief's “ready”. `failed` includes `reason`.
A 202, Wi-Fi association or open listener is never observing-session acceptance.
Operation IDs remain `<32 hex boot id>.<32 hex random id>`. Records remain bounded
and service-run scoped; **410 OPERATION_RUN_CHANGED / OPERATION_EXPIRED** requires
re-reading current instruments. Never replay a mutation after restart blindly.

An endpoint is:

```json
{
  "instrument_id":"tel-seestar-1", "request_path":"home", "mode":"proxy",
  "primary":{"host":"192.0.2.10","ports":[{"port":4700,"transport":"tcp","service":"seestar_native"}]},
  "alternatives":[{"host":"192.168.77.1","ports":[{"port":4700,"transport":"tcp","service":"seestar_native"}]}],
  "requires_client_route":false, "target_ip":"198.51.100.1"
}
```

`mode` is `relay|proxy|routed`; transport is `tcp|udp`. The service IDs are `lx200`, `synscan`, `seestar_native`, `vespera_http`,
`vespera_socketio`, `dwarf_ws`, and `direct_route`. Contract 1.1 adds `dwarf_http` (8082) and
`dwarf_rtsp` (554), which the DWARF proxy already owned, and two optional port
fields: `role` (`control|preview|imaging|rest|stream|other`) and `protocol`
(`json_rpc|binary|http|socketio|websocket|rtsp|lx200|synscan|alpaca|indi|unknown`).
Pick a port by `role` rather than by number; treat an unknown future role as
`other`. Roles come from the family plugin declaration; they never prove that the
port answers. Camera endpoints report `control` with `alpaca` or `indi`. The bridge puts the request's actual management path
first. Alternatives have the same host/ports shape for other paths. Clients use
`primary` literally. `target_ip` is informational; never reconstruct a client
endpoint from it. `requires_client_route:true` is an explicit routing prerequisite,
not proof the client's router is configured. The bridge cannot silently create a
home-router route.

`GET /instruments/{id}/endpoint` (`read`) returns the current endpoint, or **409
NOT_CONNECTED**. Refresh after connection loss, movement or address change.

- `POST /instruments/{id}/lease {lease_id,lease_seconds}` renews this client's lease
  and returns `{id,expires_in}`. Renew before expiry, using the bridge's remaining
  duration rather than the client's clock. A lease protects and restores network
  access; it is never a manufacturer-control lock.
- `POST /instruments/{id}/release {lease_id}` returns `{ok:true}`. Normal quiet and
  switching policy applies afterwards; it does **not** immediately disconnect.
  Since 1.2.0, an instrument left connected this way gives its radio to the next
  explicit `connect` for another instrument without any confirmation, unless it is
  still in use.
- `POST /instruments/{id}/disconnect {lease_id?,reason?}` returns `{ok:true}` for the user's
  explicit disconnect request. Another client's lease returns **409
  LEASED_BY_OTHER_CLIENT**. The response acknowledges the request, not physical
  completion; follow catalog/events for the actual link state. Since 1.2.0,
  `reason: "switched_off"` records that the instrument was switched off on purpose.

While a lease runs, the bridge restores a lost link automatically. No client
retry loop should fight that recovery. A lease remains subject to expiry and the
existing service-restart boundary; refresh after any journal reset.

Worked flow: [connect-allocating](../contracts/native-client/v1/connect-allocating.json)
receives 202, polls `running`, then receives `succeeded` and uses the returned
endpoint. [connect-needs-replacement](../contracts/native-client/v1/connect-needs-replacement.json)
requires explicit user confirmation before retry. The adjacent ready, protected,
disabled, stale-revision, lease and disconnect scenarios exercise the other paths.
[hotspot-path](../contracts/native-client/v1/hotspot-path.json) shows the fixed
hotspot fallback and reversed endpoint priority.

## E. Events and recovery

`GET /events?after=&boot_id=&limit=&wait=` preserves the existing cursor protocol.
`after` is a nonnegative signed-64-bit sequence, `boot_id` the 32 lowercase hex
service epoch, `limit` 1–100 (default 50), and `wait` 0–25 seconds (default 0).
Hold a valid request until an event or timeout; an empty page on timeout is normal.
At most four requests wait concurrently; further requests receive an immediate
page. This is not a second subscription transport or an unbounded queue.

Retain `next_cursor` and `boot_id`; drain `has_more` before waiting. Use sequence
ordering, not the device's best-effort timestamp (`clock_status:"unverified"`).
Authenticated `GET /system/time` and `POST /system/time-hint` are available at read
scope, plus optional event `utc_corrected` and `clock_status:"synchronized"` when
current chrony evidence exists. See [UTC correction](API.md#best-effort-utc-correction)
and the executable [current-time scenario](../contracts/native-client/v1/current-time.json).
Client hints never set the OS clock or change sequence ordering.
On **any** non-null `reset_reason` (`BOOT_CHANGED`, `RETENTION_GAP`, `CURSOR_AHEAD`,
`HISTORY_UNAVAILABLE`), re-read `/instruments`, adopt its current cursor/boot ID,
and do not replay old events as fresh notifications.

The following instrument and lease events are implemented. The live ring is 256 events and the private
atomic journal is 1,024 (maximum 1 MiB), with writes at least ten seconds apart.
An overflowing unflushed live ring reports a retention gap. The final unflushed
interval may be lost at shutdown; shutdown never forces an extra early write.

New event types are additive to the existing journal:

| Type | Additional fields |
| --- | --- |
| `instrument.state` | `instrument_id,link,service` |
| `instrument.lost` | `instrument_id,reason` |
| `instrument.restored` | `instrument_id` |
| `instrument.moved` | `instrument_id,from_radio_id,to_radio_id` (opaque per service run) |
| `instrument.endpoint_changed` | `instrument_id,endpoint` |
| `lease.expiring` | `instrument_id,lease_id,expires_in` |
| `lease.ended` | `instrument_id,lease_id,reason` |
| `telemetry.session_started`, `telemetry.session_ended` | `instrument_id,session_id` |
| `telemetry.available` | `instrument_id,session_id,latest_sample_seq` |
| `telemetry.adapter_yielded` | `instrument_id,adapter_id,retry_after` |
| `camera.detected`, `camera.removed`, `camera.power_unstable` | `instrument_id` |
| `serial.detected`, `serial.removed` | `instrument_id` (opaque `ser-<12 hex>`; **proposed 1.3**, see [serial mounts](SERIAL-MOUNTS-CONTRACT-PROPOSAL.md)) |

`telemetry.adapter_yielded` reports an observation adapter stepping aside after
repeated peer closures. The bridge emits `adapter_id:"vespera_status_v1"` and
`retry_after:600` once per yield transition, including when no activity session
exists. It carries no manufacturer identifiers or payload. Clients continue
following the catalog; yielding never changes the instrument's control or link.

`instrument.state` uses the catalog's link/service projection, coalesced to one
emission per instrument per five seconds; a subsequent reconcile emits the latest
pending state. `instrument.lost` concerns unexpected association loss while enabled
or leased (`LINK_LOST` or `RADIO_UNAVAILABLE`). Intentional stops/moves do not emit
lost/restored. `lease.expiring` warns once per version at 60 seconds remaining;
`lease.ended.reason` is `EXPIRED` or `RELEASED`.

`camera.detected`, `camera.removed` and `camera.power_unstable` carry only
`instrument_id` and the common event envelope. New events also carry the existing sequence,
boot ID, timestamp and clock status. They carry instrument IDs, never SSIDs,
MACs or manufacturer payloads. The requested `endpoint_changed.endpoint` necessarily
contains transport addresses: it is the sole explicit address-bearing response
field in this contract. It must be assembled for the authenticated request's
management path, not stored as an address-bearing event journal payload. Persist
only the instrument-change marker, then hydrate its current endpoint on read;
if no current endpoint exists, return null and let the client re-read the catalog.
This preserves the existing journal's no-IP persistence rule. Endpoint data is
never proof of historical reachability and is never logged.

Worked flow: [lost-and-restored](../contracts/native-client/v1/lost-and-restored.json)
keeps a lease, receives lost/state/restored events and re-reads the endpoint.
[moved-to-other-radio](../contracts/native-client/v1/moved-to-other-radio.json)
refreshes transport after movement. [events-gap-reset](../contracts/native-client/v1/events-gap-reset.json)
re-reads instruments after each reset rather than replaying history.

## F. Telemetry catch-up

`GET /telemetry/samples?session_id=&after=&boot_id=&limit=`
requires `telemetry`. Session ID is mandatory. `limit` is 1–500. Cursor and reset semantics
match events. The envelope is `{samples,events,next_cursor,has_more,gap,reset_reason}`. A `telemetry.available` event lets the client request samples after
its last processed sample sequence. After a reset, re-read instruments/current
session state instead of presenting historical samples as new observations.

Only owner-enabled, read-only, normalized observations are eligible. No control,
raw payload storage or automatic manufacturer authentication is authorized.
Owner-supplied key acquisition and authentication are implemented; the Seestar events listener is described in [API.md](API.md#seestar-session-events).

Worked flow: [telemetry-catch-up](../contracts/native-client/v1/telemetry-catch-up.json)
checks buffered nonempty sample/event paging and all four reset reasons over real HTTP.
Samples expose `seq,session_id,boot_id,mono_ms,utc,utc_corrected,metric,value`;
events expose `seq,session_id,boot_id,type,reason`. Both arrays remain bounded at
500 items, with a shared combined page limit. See [session telemetry](API.md#session-telemetry)
for session inventory, settings, exports and gap filling. All scenarios now require real execution.

## Executable scenarios and replay server

Run from this checkout:

```sh
python3 tools/contract-mock.py --list
python3 tools/contract-mock.py --scenario pair-and-token --port 8798
# Or all flows, in the lexicographic order printed by --list:
python3 tools/contract-mock.py --all --port 8798
```

Use `http://127.0.0.1:8798` as the development bridge origin. The mock listens only
on loopback; it cannot touch radios or forward an instrument command. `--port 0`
selects an available port and the first stdout line reports `{"port":...,"steps":...}`.
Each request consumes exactly one next step. A mismatch returns **599** with a
field-location diff and leaves the cursor unchanged. Method, path including query
order, declared headers and parsed JSON body must match. Unspecified transport
headers are ignored. Errors never echo submitted/expected secrets. There is no
reset endpoint: restart the process. `--all` concatenates independent scenarios;
it does not carry simulated appliance state between flows. Timing is illustrative:
the mock immediately returns long-poll results and does not simulate elapsed time.

Every scenario is a JSON object with `name`, `description`, `contract_version:1`,
`requires:[capabilities]`, and `steps`. Each step has
`request:{method,path,headers,body}`, `response:{status,headers,body}`, and `note`.
A null request body means no body. `discovery` is optional synthetic DNS-SD input,
not an HTTP/mDNS mock endpoint. `real_patterns` is an optional JSON-pointer-to-regex
map for response values generated by the real app (currently `/access_token`, `/boot_id` and `/version`).
Patterns must be explicitly declared; remaining fields match exactly. A step may
add its own `real_patterns`; observed string values are substituted into later
requests without modifying the fixtures. No live
credential is written back into a fixture.

Fixed fictional values: home `192.0.2.10`, hotspot `192.168.77.1`, instrument targets
`198.51.100.x`; instrument IDs `tel-seestar-1`, `tel-vespera-1`, `mount-zwo-1`;
device ID `0123456789abcdef`; tokens/secrets `test-token-<n>`; the only password
value `test-password-1`. `astro-bridge-test` is a synthetic network-name example.
There are no real network identities or manufacturer secrets. Config revisions,
lease IDs, boot IDs, operation IDs and timestamps are illustrative and opaque.

`STATUS.json` maps every scenario to `mock` or `real`. `real` means its whole flow
passes against the application using DemoNetwork; it never means hardware tested.
The tests also reject accidentally leaving a wholly implemented flow mock-only.
Instrument scenarios use the actual WebServer, controller, operation and
protection engines with a disposable DemoNetwork fixture. Association/service
state-table inputs, quiet activity and socket-path management topology are
explicitly synthetic; no HTTP facade response or scheduler success is mocked.
New implementation work must update these scenarios/statuses and keep the suite
passing; route/schema additions alone do not justify upgrading evidence.

```sh
python3 scripts/generate_openapi.py
python3 -m unittest discover -s tests -p 'test_native_client_contract.py' -v
node --test tests/native-client.test.mjs tests/mobile-client.test.mjs
```

The standard-library schema validator covers bounded nesting/collections, refs,
types, properties, required, enums, constants, items, additionalProperties,
minimum/maximum, lengths, patterns and unions. Tests check every request/response,
strict HTTP replay, fixture privacy, TXT byte bounds and implemented flows through
`python3 -m wifi_bridge --demo` with temporary state. Schema checks do not validate
mDNS publication, delay accuracy, actual endpoint routing or radio recovery.

## JavaScript helper and versioning

[astro-bridge-client.mjs](../examples/astro-bridge-client.mjs) takes a discovered
bridge origin; discovery stays with the platform's DNS-SD/local-network API.
`info()`, `pair()`, `loginClient()`, `instruments()`, `connect()`, `renewLease()`,
`release()`, `disconnect()`, `waitEvents()`, `instrument()` and `endpoint()` use this
contract. `pair()` clears its administrator token even on failure; the application
stores the returned client secret appropriately. `AstroBridgeError.details`
preserves actionable conflict details such as `replaceable`.

Before this contract, the example's `connect`, `renewLease` and `disconnect`
called administrator telescope/versioned-reservation routes. Those helpers are
now explicitly named `connectTelescope`, `renewReservation`, `disconnectTelescope`.
Other legacy helpers are preserved. Update consumers of this example when adopting
the native contract; no server-side legacy route was renamed.

Since the contract 1 freeze, changes to contract 1 are additive only. A breaking change requires
contract **2**, served alongside 1 for one release. Keep TXT `contract=1` consistent
with `/info.contract_version=1`. Clients ignore unknown response properties,
handle unknown enum values conservatively, and skip unknown event types while
advancing their cursor. They must never turn an unknown state into readiness or
consent. The example exports `NATIVE_CONTRACT_VERSION = 1` for identity checks.

The committed [OpenAPI snapshot](../contracts/native-client/v1/openapi-snapshot.json)
freezes 17 operations on 16 native paths, their 34 transitively referenced schemas,
transport definitions and authentication. `test_native_client_contract.py`
compares the current generated contract to that snapshot. Existing route/method,
scope, security, media type, parameter, field, enum member, union branch and
response guarantees cannot disappear or change meaning. New request inputs must
be optional; response fields, enum members, disjoint typed events and routes may
be added. Existing types, bounds, defaults and patterns remain stable. Descriptive
text and planned-to-implemented status may change. This conservative structural
guard fails closed on unsupported schema transformations; runtime scenarios still
check behavior. Never regenerate the baseline to make a breaking v1 change pass.

The two reserved telemetry arrays were completed after checking the old frozen
snapshot for additive compatibility. The expanded baseline now freezes their
item schemas and 500-item bounds too; the one-time compatibility exception is
removed. All native routes and transitive schemas remain additive-only.

No fixture, schema, demo or replay success claims
iOS/Android mDNS behavior, local-network permission acceptance, physical telescope
behavior, phone trust, radio hotplug/recovery, or manufacturer-app compatibility.
Those remain documented, not verified on hardware here.

## CHANGELOG

### 1.0 — 2026-11-04

- Freeze the generic native surface; the executable scenarios pass against
  the real HTTP/controller/scheduler stack.
- Correct generated `ConnectionEvent.outcome` to include the already emitted
  `not_answering` and `unknown` service states. No scenario was weakened or changed.
- Commit the compatibility snapshot and reject breaking changes in tests, with
  explicit mutation regressions and the two narrowly reserved telemetry arrays.
- Physical and phone evidence remains outstanding; see the hardware boundaries above.

### Telemetry completion

The native telemetry page now includes normalized samples and lifecycle events;
session inventory, summaries, settings, deletion and streamed exports are additive
routes. All 23 scenarios run real HTTP flows. The completed item schemas and new
routes are included in the reviewed frozen snapshot.

### Link producer and telemetry event hooks

The first real producer completes the deferred telemetry event hooks. Shared sessions emit
`telemetry.session_started`/`telemetry.session_ended` from real lifecycle changes
and coalesce `telemetry.available` across metric batches. One-second initial
coalescing and a 30-second per-session interval bound repeated availability;
session end publishes any pending final sequence before the ended event.
DNS-SD and public info now share the available `telemetry` service capability.
Instrument telemetry lists intersect implemented runtimes with effective owner
selections: `link_v1` is available, future Vespera/Seestar/camera adapters are not
advertised. Existing discovery/info/catalog examples were updated; the frozen
route and sample/event schemas remain unchanged.

## Camera inventory/settings addition

The native contract adds `GET /cameras` (camera/admin scope) as a literal array,
and administrator `GET`/`PATCH /cameras/settings`. Patches require the actual
quoted current configuration revision. The camera-settings scenario runs against
the real disposable application, listing an empty inventory and changing/restoring
the owner switch without inventing USB hardware. Separate real HTTP tests inject
the local simulator and validate nonempty camera responses against OpenAPI,
including per-ingress Alpaca endpoint nullability, scope denial and cookie CSRF.

`state` describes the current driver state and preserves unknown additive string
values. Separate discovery lifecycle and native-camera projections are added
without replacing this array or its existing fields. `alpaca` is
null if the current bridge ingress has no successfully bound listener;
`last_image` is `{bytes:0,seconds:0}` when no retained image is available. The
optional owner includes ClientID, source address and monotonic `since` seconds.
Actual Camera V3 operations use the returned separate Alpaca port, without Bearer,
under the owner switch and management network restrictions. The frozen older
instrument/telemetry routes and the Vespera adapter-yielded event remain unchanged.

## Attached camera catalog and pages

`GET /instruments` now includes attached cameras. Saved profiles stay first in
their existing order; camera rows follow in opaque-ID order. Each page retains
the frozen 24-row maximum, with additive `next_cursor` and `has_more`. Read a
second page with `GET /instruments?after={next_cursor}` to collect all 24 profiles
and up to eight cameras. The cursor binds ordered membership and boot identity,
not state values. HTTP 400 `INVALID_CURSOR` rejects malformed/noncanonical values;
HTTP 409 `CATALOG_CHANGED` requires discarding collected pages and restarting.
The example client's unchanged no-argument `instruments()` reads one page;
`allInstruments()` makes at most six requests across three attempts. A changed
catalog never silently skips an identity.

Camera `family` is the driver ID, `link` is connected when discovery is ready
(disabled under the owner serving switch), and `service` comes from driver
state. The literal `/cameras` array adds optional `instrument_id`, `usb` and
`discovery` metadata while retaining all old fields. Use `instrument_id` when a
saved-profile collision requires a deterministic camera alias. Existing profile
identity wins; alias changes release old camera reservations. Native IDs satisfy
both frozen ID bounds. Serial numbers are never used to invent identity.

The generic catalog → connect → endpoint → lease/release/disconnect flow works
without changing instrument Wi-Fi. Camera connect returns 200 only for the
request ingress's bound Alpaca service. Its endpoint is `mode:relay`, with
`alpaca_camera` TCP service and optional `device_number` on the port entry,
default port 11111. `target_ip` remains required: for a camera it means the serving
management IPv4 host; telescope semantics are unchanged. Native camera leases
reserve bridge endpoint access only. They do not map to, claim or transfer Alpaca
ClientID/source ownership, and do not confer exposure exclusivity. Disconnect
releases the caller's native reservation without closing the device or aborting
an exposure. The independent Alpaca owner guard remains authoritative for camera
operations. Foreign native holders cannot release another client's reservation.
Removal invalidates the endpoint and releases camera reservations; expiry uses
the existing bounded lease events. No camera action changes a saved Wi-Fi profile.

`camera.detected`, `camera.removed` and `camera.power_unstable` contain only the
instrument ID/common envelope. Three disconnects in ten minutes indicate unstable
attachment, not measured power. `camera-attached` is a real HTTP/controller flow
with temporary sysfs metadata, an injected reviewed driver and a bound loopback
Alpaca listener; its addresses/ports are fixture placeholders, not hardware proof.
All preceding 26 scenarios remain real. Pi Zero W/Zero 2 W expose no camera
worker/listener/probe, including through lazy API access. Real USB, permissions,
Pi load and camera/native-app interoperability remain hardware checks.

### Optional Alpaca observation cadence

The existing telemetry settings profile gains only the optional integer
`alpaca_interval_seconds` (10–120; absent means 15). Existing `adapters` arrays,
response fields, scopes and routes retain their semantics. An adapter-only PATCH
preserves any saved cadence; empty adapters still opt out. `alpaca_observer_v1`
is advertised only for owner-selected, implemented observation outside demo.
`camera.state` adds a normalized text metric without changing the sample envelope.
The telemetry catch-up scenario now round-trips an owner selection and cadence
through real HTTP. The preceding frozen snapshot passed the additivity guard
before the two telemetry settings/status schemas were extended and frozen again.
See [Alpaca observations](API.md#alpaca-instrument-observations) for the exact
GET allowlist, activity semantics and unverified hardware/load boundaries.


The real `connect-operation-running` scenario exercises an owner-enabled running
operation veto through the existing connect/operation routes. With
`auto_policy.protect_running_operations` enabled, fresh running telemetry returns
`OPERATION_RUNNING` on the completed failed operation and deferred event. Missing,
stale or reconnecting supported operation telemetry remains unknown and also
prevents replacement, including force; its absence cannot release protection.
Disabled/unsupported operation sources keep the previous replacement policy.

### Maker-key metadata and administration

Block C's `capabilities.maker_key` retains `absent`, `unverified`, `verified` and
null, and additively includes `refused` and `storage_unavailable`. One key belongs
to a family on this bridge, independent of the requesting client/profile. Public
capabilities never include material, fingerprint, key size or modulus.

The new admin-only `/maker-keys` resources support metadata, explicit acquisition,
write-only import and revision-guarded removal. Acquisition's returned operation
URL is `/maker-keys/operations/{id}`, also admin-only; it must not be sent to the
connection-operation helper. These additions do not change connection operations,
leases, telemetry settings or the frozen event variants. Clients ask the bridge
for endpoints; no manufacturer address or download URL is accepted from clients.
See [API details](API.md#owner-supplied-maker-keys).

`maker-key-metadata` is a real HTTP scenario using metadata and an intentionally
unusable file. All preceding 25 scenarios remain real. Identity/TXT fixtures gain
the implemented `maker-keys` capability. The prior frozen schema passed the
additive guard before this extension was captured in the snapshot. Actual maker
fetch/verdict and listener behavior remain hardware checks.

The optional `TelemetryStatus.card_data` field adds with bounded normalized receipt
ages and recent-history previews; earlier clients may ignore it. Source freshness
is per metric and independent of storage batching. `history_partial` prevents
clipped previews being mistaken for complete totals. Existing sample cursors,
scopes and operation-protection behavior are unchanged; all27 scenarios remain
real HTTP flows. See [the card API](API.md#session-cards).

### Additive retained-session projection

The optional `GET /api/v1/telemetry/sessions/{id}/analysis` route and typed
`TelemetryAnalysis` cover retained frame buckets, qualified observed exposure
and exact paired raw-fetch ranges. See [API semantics](API.md#retained-session-analysis).
Older v1 servers may lack this route; clients can retain summary/export behavior.
`telemetry-catch-up` exercises the actual route after journal loss and confirms
that a derived read preserves the existing fetch audit. The preceding frozen
snapshot passed before the new route/schema were added to the freeze. Old route
and field semantics are unchanged. All evidence is synthetic and hardware-unverified.

### Camera owner administration

Camera inventory optionally includes supported sensor geometry, initial defaults,
current settings, last refused Alpaca client and an opaque owner generation.
Alpaca ClientID/source ownership is separate from paired-client bridge leases.
Administrator-only `POST /cameras/{id}/release-owner` requires explicit
`confirm_release:true`, captured `owner_generation` and actual quoted `If-Match`.
A worker-atomic mismatch returns409 `CAMERA_OWNER_CHANGED` without closing the
camera. Only this explicit owner action can interrupt an Alpaca exposure; native
reservation release/disconnect remain non-aborting. `discover` is an optional
additive settings field (legacy defaulttrue) governing actual UDP discovery.
The administrator-only `/cameras/demo` plug action creates only an explicit
simulator and loopback listener; no demo camera exists at startup. See
[attached cameras](API.md#attached-cameras-and-alpaca).

An attached camera served by INDI has an additive `indi` object on its camera
inventory row and no Alpaca device number. Use the existing endpoint route:
a ready management path returns `service: "indi_camera"`, `transport: "tcp"`
and port 7624. Native reservations retain their existing semantics. The
`camera-indi` scenario exercises that endpoint through a real HTTP controller
and a Python fake indiserver. Primary `_astrobridge._tcp` may publish the
health-gated TXT hint `indi=7624`. See [camera settings](API.md#optional-indi-camera-service).

## Contract 1.1 (additive)

Contract 1.1 keeps every 1.0 route, field, bound and meaning. The previous
snapshot passed the additive guard before the 1.1 surface was frozen into
`openapi-snapshot.json` (now 30 native paths and 61 schemas, including the
telemetry analysis route that had not been frozen yet). It only adds
optional fields, enum members, event types and headers; clients written for 1.0
keep working, and 1.1 clients must tolerate a 1.0 bridge that omits them.

### Port roles

Endpoint ports and `/endpoints` paths carry optional `role` and `protocol` from
the family plugin declaration ([port roles](PLUGINS.md#port-roles-native-contract-11)).
The DWARF proxy now also advertises 8082 (`dwarf_http`) and 554 (`dwarf_rtsp`),
which it already forwarded. Updated fixtures: every endpoint-bearing scenario.

### Protocol, model and the owner's name

Each instrument adds `protocol` and `model`. `protocol` is the family's declared
driver protocol (`seestar`, `vespera`, `dwarf`, `lx200`, `synscan`; cameras
`alpaca` or `indi`) or null for a generic profile; choose a driver from it, not
from `family` or the name. `model` is the instrument model when the bridge knows
it (a verified identity, or the attached camera's driver name), otherwise null.

`name` is the owner's personal name for the instrument: the owner sets it in the
web interface (Instrument name) or with administrator
`PATCH /api/v1/telescopes/{id} {"name": …}` and the current `If-Match` revision.
It is never derived from the Wi-Fi name or the model. `id` stays the stable
identifier a client stores and passes to `connect`; renaming never changes it.
Show `name` to people and keep `id`, `family`, `protocol` and `model` for logic.

### Verified identity

Each instrument adds `identity_source` and `verified_identity`. A verified identity
tells which physical instrument answered on the profile's **current** radio and
target, from a reply that an existing read-only path already receives:

| `identity_source` | Reply | `verified_identity` |
| --- | --- | --- |
| `vespera_status` | Status pushed to the owner-enabled `vespera_status_v1` observer | `model` and `hardware_id` |
| `zwo_model` | `:GVP#` during owner-requested reachable discovery | `model`, `hardware_id:null` |
| `synscan_version` | `:e1` during owner-requested reachable discovery | `model:null`, `hardware_id:null` (family confirmed) |
| `camera_driver` | Attached camera driver | `model`, `hardware_id:null` |
| `unverified` | None for the current link | null |

`hardware_id` is a 16-hex keyed digest of the manufacturer identifier, derived
from this bridge's installation secret: stable for one instrument on one bridge,
different on another bridge, and never the identifier itself. Use it to tell two
instruments of the same model apart (for example to attach a saved plan to the
right one). Identities are RAM-only, reset by a service restart, and dropped as
soon as the profile's radio or target changes. Nothing is sent to an instrument
to obtain them, and no identifier enters storage, events or logs. Seestar and
DWARF stay `unverified` until a reviewed read-only reply carries an identity.
Scenario: [instrument-identity](../contracts/native-client/v1/instrument-identity.json).

### Switch progress and completion notice

`GET /operations/{id}` on a native operation adds `progress:{phase,eta_seconds}`.
`phase` is `leaving` (choosing or freeing a radio), `associating` (joining the
instrument Wi-Fi), `verifying` (waiting for the instrument service), `ready`,
`restoring` (putting a replaced instrument back after a failure) or `failed`.
`eta_seconds` is a countdown estimate from typical phase budgets (about 40 s in
total), bounded by the 120 s operation deadline: 0 when ready, null after a
failure. It is never a promise; follow `state` for the outcome.

The event journal adds two types, for every connection operation (paired client,
owner or automatic): `instrument.switch_progress {operation, phase, eta_seconds}`
once per phase change, and `instrument.switch_completed {operation, outcome, reason}`
once per operation, with `outcome` `ready` or `failed` and the operation's reason
code. A client that asked the bridge to switch can simply wait on `/events` for
`instrument.switch_completed` whose `operation` is its operation ID, then read the endpoint.
Scenario: [connect-switch-progress](../contracts/native-client/v1/connect-switch-progress.json).

### Night reservation

A night reservation is an ordinary native lease with a longer horizon, for a
client that wants the bridge to keep an instrument connected and observed while
the client sleeps. Send `reserve_seconds` (60–50400, at most 14 hours) with
`POST /instruments/{id}/connect`; it takes precedence over a shorter
`lease_seconds`. The response lease adds `reservation_expires_in`, the true
remaining time; `expires_in` keeps its 1.0 bound and reports at most 3600. The
catalog lease summary adds `reservation_expires_in` (null for an ordinary lease).
Express "until dawn" as seconds from now: the bridge clock may be unverified,
so the client converts its own wall-clock target before sending.

While it runs, everything a lease does applies: the lease belongs to the paired
client (not to an access token), so it survives the client's sleep and token
renewal; the bridge restores a lost link; protection refuses replacement and
reassignment of the radio, including automatic switching, until release or
expiry; session telemetry continues because the instrument stays connected.
`POST /instruments/{id}/lease` with only `lease_seconds` never shortens a
reservation, so a 1.0 renewal loop is harmless; adding `reserve_seconds` sets a
new horizon (earlier or later). Release at dawn with `POST …/release`.
`lease.expiring` warns 60 seconds before the end and `lease.ended` reports it.
The owner revokes any reservation with the existing administrator
`POST /leases/{id}/release` (current password and revision). Reservations stay
RAM-only: a service restart ends them, like every lease. Attached cameras refuse
`reserve_seconds` (they use no radio). Scenario:
[night-reservation](../contracts/native-client/v1/night-reservation.json).

### Discovery hints and minor version

`_astrobridge._tcp` TXT adds `contract-minor` (`1` for 1.1, `2` since 1.2), `free_radios=<0–16>`,
`hotspot=1` while the bridge hotspot is active, and, only while local HTTPS is
ready, `fp=<64 lowercase hex>`: the SHA-256 of the bridge CA certificate (the
same certificate `GET /system/https/certificate` serves). Public `/info` adds the
same facts as `contract_minor`, `free_radios`, `hotspot` and `tls_fingerprint`
(null without HTTPS). `fp` is a trust-on-first-use pin: compare it with the
certificate presented over TLS before sending credentials, and treat a later
change as a new identity. `free_radios` counts instrument-capable radios with no
management role and no active profile; above zero, a connect that needs a radio
will not have to replace another instrument (protection and leases still apply).
It is a hint at announcement time, not a reservation. A 1.0 bridge omits all of
these; absence means unknown, never zero.

`boot=<8 lowercase hex>` (additive) changes at every start of the bridge service:
the first characters of the `boot_id` of its events. A client that kept an
earlier answer for this `id`, a failed check included, must read `/info` again
when `boot` differs. The bridge also restarts its announcement daemon by itself
when it finds it stopped, so the announcement returns after a restart of the
bridge without any other change.

### Retry-After

Every 429 response carries a `Retry-After` header in seconds and the same value
as `error.retry_after`. Connection admission (`CONNECT_LIMITED`) reports the
seconds until its one-minute window frees a slot; other limits report their
documented window (60 seconds by default). When the four concurrent `/events`
waits are already taken, a `wait` request is answered immediately, as in 1.0,
and now adds `Retry-After: 2`; wait that long before polling again rather than
looping. Scenario: [rate-limited-retry-after](../contracts/native-client/v1/rate-limited-retry-after.json).

### Access and simultaneous connection

Each instrument adds `access` and `simultaneous`. `access` is `direct` when the
bridge joins the instrument's own Wi-Fi with one of its radios, `attached` for a
USB camera, and `station` for instruments joined to the bridge's network
(hotspot or private LAN), used since contract 1.2. `simultaneous` is
true when the instrument is connected or connecting it needs no replacement
because a free radio exists; false means connecting it will disconnect another
instrument (after consent), so a client can suggest adding a Wi-Fi adapter to keep
every instrument connected and observed all night. `409 NO_FREE_RADIO` adds
`error.free_radios:0` and `error.simultaneous:false`. Both are hints computed
when read; protection, leases and owner policy still decide.

Replay cursors: event-reading scenarios now request `after` equal to the cursor
the real flow used, so returned sequences always follow the requested cursor.

## Contract 1.2 (additive)

Contract 1.2 keeps every 1.0 and 1.1 route, field, bound and meaning. It passed
the additive guard against the 1.1 snapshot before being frozen into
`openapi-snapshot.json` (still 30 native paths and 61 schemas; station
administration routes are owner routes, outside the native snapshot). `/info.contract_minor` and
the TXT `contract-minor` are now `2`. Clients written for 1.1 keep working, and
1.2 clients must tolerate a 1.1 bridge that omits the fields below.

### Telemetry page boot ID

Every `GET /telemetry/samples` page adds `boot_id`, the monotonic domain of its
`next_cursor`. Store the pair as returned and send both back (`after` and
`boot_id`) on the next request; there is no need to read `/telemetry` first. A
different `boot_id` still answers `reset_reason: BOOT_CHANGED` with a full
re-read. Scenario:
[telemetry-catch-up](../contracts/native-client/v1/telemetry-catch-up.json).

### Current operation

Each instrument adds `current_operation`: `null` when no read-only source reports
an operation for it, otherwise `{state, kind, since}`. `state` is `idle`,
`pending`, `running`, `paused`, `completed`, `failed`, `stopped` or `unknown`
(a selected source without fresh evidence); `kind` is set only while running
(`initialization`, `observation`, `capture`, `stacking`, `mosaic`,
`calibration`, `pointing`, `focusing`, or null when the source does not say);
`since` is the bridge UTC time this state and kind were first observed, not the
instrument's own start time. Sources are Vespera status, Seestar events (with an
owner-provided maker key) and DWARF notifications (15208 capture state, 16405 device
state at connection, 15211 GoTo as `pointing`; partially verified on a DWARF mini; a
JSON-by-JSON record is still to come); all are read-only. It
is the evidence `auto_policy.protect_running_operations` uses, so a client can
explain why a replacement was refused. The field is named `current_operation` so
it is not confused with the connection `operation` of a connect response.
Scenario: [instrument-operation](../contracts/native-client/v1/instrument-operation.json).

### Station instruments

An instrument with `access: station` joined the bridge network (hotspot or
private LAN) in its manufacturer station mode, so it uses no bridge radio and
`simultaneous` is always true. It stays in the catalog while switched off: `link`
is `connected` while it answers, `unreachable` (new value) when it does not, and
`saved` before the first check; `reason` is `NOT_ANSWERING` when unreachable.
`connect` never switches anything: it creates the lease (night reservations are
allowed) and returns `endpoint.mode: direct` (new value) with the instrument's own
address and ports by role; while unreachable it answers `409
INSTRUMENT_UNREACHABLE`. `disconnect` only releases this client's lease. A station
lease protects nothing on the radios. Treat unknown `link` and `mode` values as
`saved` and `routed` respectively. Scenario:
[station-instrument](../contracts/native-client/v1/station-instrument.json).

### Owner switch for night reservations

When the owner turns off `auto_policy.night_reservations`, a request with
`reserve_seconds` answers `403 RESERVATIONS_DISABLED`; retry without it to obtain an
ordinary lease.

### Web interface address

Public `/info` adds `management_endpoints`: one entry
`{url, path, tls}` per confirmed management path (`path` is `home` or `hotspot`,
the same paths as administrator `management`), with `url` such as
`http://192.168.77.1/`. While local HTTPS is ready, each path also lists
`https://<hostname>.local/` with `tls: true`; prefer it after checking
`tls_fingerprint`. The list is empty when the bridge knows no path. Open `url` as
given to show the owner's web interface; never build an address yourself.
Scenario: [info-public](../contracts/native-client/v1/info-public.json).

## Radio release (additive, proposed for contract 1.3)

Shipped in 1.1.1. Every field below is optional or new in a response; the frozen
1.2 snapshot is unchanged, `contract_minor` stays `2`, and a client that sends
and reads nothing new keeps working. These rules are covered by tests with
simulated radios only; they are **not verified on hardware** yet.

One free Wi-Fi adapter links one instrument at a time. An enabled profile keeps
its adapter. What happens to a `connect` for **another** instrument depends on
what the holder is doing:

| Holder `state` | Meaning | A `connect` for another instrument |
| --- | --- | --- |
| `unreachable` | Link down and at least one new attempt failed (switched off, out of range), or retries stopped after a refused password or an address conflict | Takes the radio. No confirmation |
| `standby` | Connected and quiet, and the app that used it released its reservation (`release`) | Takes the radio. No confirmation |
| `reconnecting` | Link just lost, no new attempt has failed yet | `409 NO_FREE_RADIO`; `force_replace` accepted |
| `connected` | Connected, nothing known to be running, never released | `409 NO_FREE_RADIO`; `force_replace` accepted |
| `in_service` | Connected with a running or unknown operation, recent traffic, or a mount (the bridge cannot prove a mount is idle) | `409 NO_FREE_RADIO`; `force_replace` accepted |

A paused connection, an owner lock or another client's reservation is never
taken, forced or not: the holder then has `forceable: false` and a `blocker`
(`CONNECTION_PAUSED`, `OWNER_LOCKED`, `LEASED_BY_OTHER_CLIENT`).

### Refusal

```json
{"error": {"code": "NO_FREE_RADIO", "message": "…", "replaceable": [], "free_radios": 0, "simultaneous": false,
  "holders": [{"instrument_id": "tel-vespera-1", "name": "Test telescope two", "kind": "smart_telescope",
               "state": "in_service", "current_operation": {"state": "running", "kind": "observation", "since": "…"},
               "blocker": null, "forceable": true}]}}
```

Nothing was changed on the bridge. Show the holder's `name` and what it is doing.
Treat an unknown `state` as `in_service`.

### Forcing after the user's confirmation

Send the same `connect` again with `"force_replace": ["tel-vespera-1"]` (at most 24
identifiers, taken from `holders` with `forceable: true`). Unlike `allow_replace`
it needs neither the owner's `auto_replaceable` consent nor a quiet connection,
and no replacement cooldown applies. Ask the user first, naming the instrument:
forcing cuts the bridge's link to it whatever it is doing. **A mount keeps
moving or tracking on its own when its link is cut; the bridge sends it no
command.** If the new instrument cannot be linked, the bridge restores the
replaced connection. A 1.1.0 bridge answers `400 INVALID_FIELDS` to this field.

### What gave way

When a request takes a radio, the `200` or `202` answer and the operation add:

```json
"displaced": {"instrument_id": "tel-vespera-1", "name": "Test telescope two", "kind": "smart_telescope", "reason": "UNREACHABLE"}
```

`reason` is `UNREACHABLE`, `STANDBY` or `FORCED`. On a `202` it is the bridge's
plan; the operation result is authoritative. The displaced profile is disabled
(no more attempts) and its catalog entry says why until it is linked again:

```json
"released": {"reason": "RADIO_YIELDED", "to": "tel-seestar-1", "age_seconds": 42}
```

`released.reason` is `RADIO_YIELDED` (unreachable or on standby, radio given to
`to`), `REPLACED_ON_REQUEST` (forced) or `SWITCHED_OFF` (below). `age_seconds` is
null after a bridge restart. `released` is absent otherwise. This client's own
reservation on the displaced instrument ends (`lease.ended`, `RELEASED`).

### Switched off on request

After the user switches an instrument off (or asks the instrument to shut down)
through the bridge, call `POST /instruments/{id}/disconnect` with
`{"lease_id": "…", "reason": "switched_off"}`. The bridge relays the
manufacturer protocol without reading it, so it cannot know by itself that a
shutdown was requested. The profile is disabled, its radio is free at once, no
attempt is made, and the catalog shows `released.reason: "SWITCHED_OFF"` with
`link: "saved"`: say "switched off", not "link lost". A 1.1.0 bridge answers
`400 INVALID_FIELDS`: send the request again without `reason`.

### Lost without any request

The bridge keeps the profile enabled and retries by itself, with a growing
wait: 15 s, then 15, 30, 60, 120, 240 and 300 s between attempts (ceiling: five
minutes). While at least one attempt has failed, the catalog entry adds:

```json
"retry": {"attempts": 4, "interval_seconds": 120, "next_in_seconds": 37}
```

The instrument is then `unreachable`: its radio goes to any explicit request for
another instrument. A `connect` for the lost instrument itself answers as before
(its own radio is not free) and makes the bridge try again at its next
reconcile, without waiting for `next_in_seconds`.

### Busy network

`connect` answers `409 NETWORK_BUSY` while the bridge reconciles its network
(an attempt towards an absent instrument can take up to 40 s on hardware). With
`wait_seconds`, the bridge now tries again by itself within that time; otherwise
retry after a few seconds.

### Which step of a link failed

A link that cannot be made keeps its published reason (`ASSOCIATION_FAILED`).
When the system names the step, the failed operation and the catalog entry of
an instrument the bridge keeps retrying add `failure_stage`:

| `failure_stage` | What happened | What to tell the user |
| --- | --- | --- |
| `NETWORK_NOT_FOUND` | The instrument's Wi-Fi is not heard | Check that it is on and near the bridge; if it is, restart it |
| `NOT_ASSOCIATED` | Heard, but the instrument did not accept the link | Try again; move the bridge closer |
| `ADDRESS_NOT_OBTAINED` | Linked, but the instrument gave no address | Restart the instrument: its Wi-Fi no longer gives addresses |

The field is absent when nothing names the step, on a wrong password
(`WIFI_AUTHENTICATION_FAILED`) and on earlier bridges; treat an unknown value
as absent. It is derived from NetworkManager's own codes and has been checked
with simulated answers only.

### An instrument that gives no address

Some instruments keep accepting the Wi-Fi link and stop giving addresses. For
an instrument whose own network always uses one address plan (today a SynScan
mount at `192.168.4.1/24`, nothing else), the bridge then takes a free address
of that network itself: one of `.250`, `.249`, `.248`, probed first so that an
address that answers is left to its owner, and kept only if the instrument
answers a ping. Nothing else is sent to the instrument. The catalog entry says
`"address_fallback": true` while this lasts, and every new link asks the
instrument for an address first.

The owner switches it off with `auto_policy.address_fallback: false`
(`PATCH /api/v1/settings`); it is on unless set. Without it, or for any other
instrument, the attempt fails with `failure_stage: "ADDRESS_NOT_OBTAINED"`.
Written from one bench observation and checked with simulated answers only:
**not yet verified on hardware**.

### Asking again after a failure

The two-minute pause after a failed attempt (`409 TARGET_BACKOFF`) paces the
bridge's own automatic attempts only. A `connect` sent by a paired app is a
person asking again: it starts a new attempt at once, within the usual limit of
five requests a minute (`429 CONNECT_LIMITED`). A 1.1.0 bridge answers
`409 TARGET_BACKOFF` to such a request: the bridge answered, so say that it
is busy and when to try again, not that it does not answer.
