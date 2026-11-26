# Native mobile API (v1)

[← README](../README.md) · [OpenAPI 3.1 contract](../wifi_bridge/static/openapi.json) · [JavaScript client](../examples/astro-bridge-client.mjs)

The versioned local API exposes the same controller as the web interface. An app
can manage saved instruments, select the active profile on each radio, edit home
Wi-Fi, scan networks, inspect adapters, configure the optional hotspot, change
passwords and reboot. The service boundary also permits owner-enabled, read-only
observation of a connected instrument and services for devices physically attached
to the bridge. Observation never takes control or sends state-changing commands
to the instrument's mount, built-in camera or observing session; control remains
with the instrument application/protocol. Only allowlisted normalized fields may
be exposed, never raw payloads or secrets. Observation is opt-in per profile and
local only, with no manufacturer authentication by default. The only
exception uses the owner's supplied maker key solely to listen; its
one-time download from approved maker download hosts requires an owner action and
never happens automatically. See the full [observation and device-service
principle](ARCHITECTURE.md#observation-and-device-services). Optional plugin
metadata declares future capabilities and does not start observation or key
acquisition. No observation or camera routes are added by this schema change.
Management availability also does not remove the route requirements
for apps connecting directly to an instrument IP.

API version **1** is independent of the appliance software version. The existing
cookie/CSRF `/api/*` routes remain supported by the web UI. Mobile apps should use
`/api/v1/*` with Bearer tokens. No cloud account or Internet connection is required.

## Bridge load metrics

Administrator-only `GET /api/v1/system/metrics` returns read-only measurements;
`AstroBridgeClient.systemMetrics()` calls this route. Other paired scopes receive
403. No configuration, manufacturer command, extra service probe or activity
classification is triggered. This operational route is outside the native
instrument facade contract.

The response includes `api_version`, monotonic `sample_sequence`,
`sample_interval_seconds:5`, `sample_age_seconds`, `process`, `system`,
`http_loop_lag`, `relays` and `relay_coverage:"userspace_only"`. Sampling is shared
between HTTP/HTTPS and cached under a lock: `/proc` and relay snapshots are
computed at most once per five seconds, independently of client count. The main
HTTP listener maintains samples while running, even without polling clients.

`process` contains `rss_bytes`, `threads` and `cpu_percent:{"5s", "60s"}`.
Linux `/proc/self/stat` provides resident pages, thread count and user+system
CPU ticks. CPU uses monotonic elapsed time and the nearest stored baseline at
least 5/60 seconds old, rather than wall-clock time; windows are null until
sufficient evidence exists. Percentages measure one CPU core, so multithreaded
processes on multicore hosts may exceed 100%. `system` contains the three
`load_average` values, `memory_free_bytes` (MemFree) and
`memory_available_bytes` (MemAvailable) from bounded `/proc` reads. Missing or
malformed values, including non-Linux hosts, remain null rather than zero.

`http_loop_lag:{samples,p50_ms,p95_ms}` measures scheduled timer drift of the
main HTTP server's `service_actions` callback (normally a 500 ms interval), from
at most the last 120 ticks. It measures the listener loop rather than request
handler latency or the optional HTTPS handshake pool. Relay
`forwarding_latency:{samples,p50_ms,p95_ms}` uses at most 256 recent forwarding
samples per endpoint. Empty distributions have null percentiles.

Each `relays[]` row has `profile_id`, `host`, `port`, `transport`, `service`,
`bytes_to_instrument`, `bytes_from_instrument`, `datagrams`, `drops` and
`forwarding_latency`. Counters are cumulative for that live listener and reset
when it is replaced; management paths have separate counters and can be summed.
TCP bytes count successful socket writes, UDP bytes/datagrams count both
successfully forwarded directions. Drops count rejected TCP accepts, refused or
invalid UDP datagrams and observed relay I/O failures; they are local rejection/
error events, not end-to-end packet loss or a count of missing kernel packets.
UDP ownership refusal remains independently visible in the endpoint inventory.

`service:"instrument"` identifies isolated TCP/UDP ingress paths (up to 48).
`service:"seestar_discovery"` identifies up to two live UDP discovery listeners;
its `profile_id` is null because a shared discovery listener can change selected
instruments. No manufacturer discovery reply fields are retained in metrics.
Other native TCP proxies and routed forwarding run in nftables: their bytes,
packet drops and latency are **unavailable here**, never fabricated zeros.
`dwarf_relay.py` is an offline codec with no active network relay. Thus an empty
`relays` list does not mean an idle kernel forwarding path.

Forwarding latency is local receive-to-successful-send delay, not network RTT,
manufacturer response time or a radio loss estimate. TCP uses at most 64 timing
buckets per direction alongside the existing 64 KiB byte buffers; extra fragmented
reads coalesce into the final bucket using its oldest timestamp. Percentiles
sample forwarded chunks, not bytes or protocol messages. The
echo bench and owner procedure measure RTT separately.
Pi Zero W resource numbers remain documented, not verified on hardware.

## Best-effort UTC correction

`GET /api/v1/system/time` requires `read` or `admin` scope and returns
`{clock_status, system_utc, corrected_utc, offset_ms, source, hint_age_seconds}`.
`clock_status` is `synchronized` only with current chrony tracking evidence,
otherwise `unverified`; the read-only probe is bounded to one second and cached
for five seconds. A missing utility, stale reference, local pseudo-reference or
failed check proves nothing. `source` is `ntp`, `client_hint` or `lower_bound`.
Without a hint or applicable lower bound, corrected UTC equals system UTC,
offset is zero and source is `lower_bound`; this is not synchronization evidence.

`POST /api/v1/system/time-hint` accepts exactly
`{"utc_iso":"2026-11-04T00:00:00Z","uncertainty_ms":500}` with the same scopes.
UTC must include `Z` or a zero UTC offset; uncertainty is an integer from 0 to
86400000 milliseconds. A hint more than 315576000 seconds (ten Julian years)
from the system clock is rejected. A synchronized clock ignores valid hints.
Otherwise each paired client may submit once per 60 monotonic seconds, shared
across its sessions; unpaired administrator sessions are individually limited.
The bounded fallback accepts at most 256 distinct clients during one interval.
Rate limiting returns 429. This endpoint never sets the system clock, alters NTP,
or supplies certificate issuance evidence.

Both responses, and `bridge_clock` in the web interface status, add
`last_sync: {kind, age_seconds, this_run, last_network_sync_utc}` (additive:
current clients ignore it; the OpenAPI response schema lists it, and the clients
in this repository read named fields and ignore the others). `kind` is `network`
when chrony measured network time, `client_hint` when only a client's approximate
time was received, and `never` when no setting is recorded. `age_seconds` counts
monotonic seconds since that event during this run (for `network` while
synchronized it is the age of chrony's latest measurement, at most one hour),
so a wrong or jumping wall clock cannot change it; it is null, with `this_run`
false, when the bridge has not seen it since it started. `last_network_sync_utc`
is chrony's own UTC reference time, kept in mode-600 `clock-sync.json` beside
`clock.json` (separate so a rollback to an older release loses nothing else) and
rewritten at most once an hour. After a restart without network time it is a date
to display, labelled as the last known setting, and never the basis of a duration.
A bridge upgraded to this release reports `never` until network time is next
seen, because earlier settings were not recorded.

The last eight hints stay in memory. Their offsets use a common monotonic anchor
and their median supplies corrected UTC, independent of later wall-clock jumps.
`offset_ms` reports corrected minus current system UTC; `hint_age_seconds` uses
the latest accepted hint's monotonic age, or null without an active hint.
The current run clamps estimates to the lower bound loaded at startup. Each
accepted median minus the greatest retained uncertainty is checkpointed in
mode-600 `clock.json`, clamped to that startup floor. Restart retains only that
floor: offsets, hints, client identities and age are discarded. The image's
existing `clock-lower-bound` checkpoint is also read as a floor, never rewritten.
Failed persistence leaves the live estimate available without a durability claim.

New events add optional `utc_corrected` whenever a hint or lower-bound correction
exists. Existing `timestamp` remains the system time. Future normalized telemetry
uses the same clock helper; telemetry is not part of these fields. Both times
are estimates: event/sample sequences remain the ordering authority. Existing
journals without the additive field remain valid. Offline Alpine/chrony behavior
and restart persistence on the physical appliance are documented, not verified
on hardware. See the executable [time scenario](../contracts/native-client/v1/current-time.json).

## Find and identify a bridge

Discover DNS-SD **`_astrobridge._tcp`**, port 80, on confirmed management
interfaces (home and hotspot), never an instrument radio. `_http._tcp` and the
legacy `_smartbridge._tcp` alias remain. The owner-visible name is the configured
bridge hostname, truncated to 63 UTF-8 bytes without splitting a character.
TXT contains exactly `api=/api/v1`, `api-version=1`, `contract=1`,
`product=astro-bridge`, `version=<application version>`, `id=<device_id>`,
`name=<display name>`, `caps=<comma-separated enabled capabilities>` and optional
`https=<port>` only while the local HTTPS listener is ready. Each string is at
most 255 bytes; the record including length octets is at most 1300 bytes.

Public `GET /api/v1/info` returns `product`, `version`, `api_version`,
`contract_version:1`, `device_id`, `name`, `capabilities`, `request_path`,
`pairing:{available,method:"admin_password"}`, `authentication:"bearer"`,
`token_expires_in:43200` and `openapi_url`. `capabilities` is the same enabled
native list as TXT `caps`: currently `pairing,instruments,connect,events-wait,telemetry`;
cameras and maker keys are advertised only when their runtimes become available. Prior feature flags move to `legacy_capabilities`.
Clients using those prior flags must read that field. Plugin descriptors and
planned routes do not constitute availability.

`device_id` is the first eight bytes (16 lowercase hex characters) of
HMAC-SHA256 with message `astro-bridge-id` and a 32-byte random installation
secret. Mode-0600 `install-secret` is created once in the state directory and is
shared with pairing. It is never derived from a MAC, serial or password. Preserve
this file to preserve identity; deleting the state directory creates a new install.
Compare discovery `id` with `/info.device_id` before pairing.

`request_path` uses the accepted socket's local destination address and the
controller's confirmed management paths: `home`, `hotspot` or `other` (including
ordinary loopback demos). Client addresses, query strings, forwarded headers and
Host cannot select it. `GET /api/v1/openapi.json` serves the shipped contract.
Other routes require authentication.

On Raspberry, runtime publication replaces the bootstrap static service with
`/etc/avahi/services/astro-bridge.service` atomically, only when bytes change.
Service/name/capability changes reload Avahi; changed `allow-interfaces` restarts
it. Avahi `--reload` does **not** reread `avahi-daemon.conf`: a hostname-only
change updates the display name/TXT immediately, but its `.local` hostname takes
effect only on the next Avahi restart (for example a management-interface change
or explicit owner service restart). This is a known limitation, not an unverified
claim of immediate hostname renaming. With no confirmed path, Avahi is confined
to loopback. A failed update keeps a bounded retry marker. Each successfully applied change records
`announcement_updated` with `interfaces:["home","hotspot"]` (only roles actually
present; either or both may be absent), never interface names or addresses.
Docker/native hosts remain owner-managed; see installation.
Actual hotspot resolution and Pi timing are covered by the discovery acceptance procedure.

Local HTTP remains available for recovery. Optional built-in HTTPS requires the
Alpine diskless image's certificate helper protocol 2 and OpenSSL; Raspberry Pi OS
and older OS helpers report unsupported. Application-only OTA cannot install these
OS prerequisites. Setup and client trust are explicit owner actions. HTTP does not
encrypt passwords or tokens; use a suitably protected local connection for initial
setup and never expose management to the Internet. There is no forced redirect or
HSTS. See guided HTTPS.

HTTP and HTTPS sessions/tokens are distinct: obtain a fresh token using the HTTPS
endpoint after validating the bridge certificate. An HTTP Bearer token cannot
operate over HTTPS or vice versa. HTTPS browser cookies use the Secure,
HttpOnly, SameSite=Strict `__Host-bridge_session` name; forwarded headers cannot
change transport identity. Native apps must account for local-network permissions
and HTTP policy. A cross-origin WebView is not equivalent to a native HTTP client;
this service does not enable wildcard CORS. Modern Android app targets do not
necessarily trust user-added CAs: configure the app's CA trust explicitly while
preserving hostname and chain verification ([Android documentation](https://developer.android.com/privacy-and-security/security-config#CustomTrust)).
Browser/PWA trust acceptance does not prove a native client trusts the same CA.

## Authenticate

```http
POST /api/v1/auth/token
Content-Type: application/json

{"password":"your-current-web-password"}
```

```json
{
  "access_token": "opaque-token-returned-only-at-login",
  "token_type": "Bearer",
  "expires_in": 43200,
  "api_version": "1"
}
```

Use `Authorization: Bearer <access_token>` on subsequent requests. Native requests
need neither cookies nor `Origin` nor CSRF tokens. If `Origin` is supplied, it must
match the bridge origin; browser protections on the old UI routes are unchanged.
Only the currently configured bridge hostname, known management IPs and the AP IP
are accepted as Host values. An arbitrary reverse-proxy hostname is not accepted.

Tokens expire after **12 hours**, are held only in server memory, and are invalidated
by an appliance/service restart. Login accepts the current web password, initially
`12345678` on new images. Login throttling and a shared 32-session limit apply to
browser and mobile sessions. Store a token using the app's appropriate protected
storage if persistence is needed; do not log tokens, passwords or whole client
instances. Never embed a user's credentials in a public app build.

- `DELETE /api/v1/auth/token`: revoke only the calling mobile session; no body needed.
- `POST /api/v1/auth/revoke-all` with `{}` (admin): sign out **all** apps and browsers and permanently revoke every paired client.
- Changing the web password revokes all sessions, including the calling token, but **does not revoke paired client credentials**. Paired apps can sign in again with their own secret.
- On `401`, obtain a new token with the paired credentials; `INVALID_CLIENT` requires pairing again with the owner. There is no refresh-token endpoint.

## Paired apps and scopes

An owner authenticates once with their administrator password, then calls
`POST /auth/clients {name, scopes}` with that administrator Bearer token. Names
contain 1–48 characters; scopes are a nonempty unique list chosen from `read`,
`connect`, `telemetry`, `camera`, `admin`. Suggest `read,connect,telemetry,camera`.
The **201** response is exactly `{client_id,client_secret,scopes,device_id}`.
The secret contains 32 random bytes encoded as URL-safe Base64 without padding,
is returned only once, and must go into the client's platform-protected storage.
The bridge stores only its salted PBKDF2-SHA256 hash (260,000 rounds) in the
mode-0600 `paired-clients.json` state file, with at most 16 paired clients.

Discard the administrator token and use
`POST /auth/token {client_id,client_secret}` for
`{access_token,token_type:"Bearer",expires_in:43200,scopes}`. It shares password
login throttling (five attempts per IP per minute), the 32-session limit, and
transport binding. Tokens are memory-only. Paired credentials survive restart;
revoked or incorrect credentials return **401 INVALID_CLIENT**. Successful
logins update `last_used_utc_best_effort`; this UTC value is informational and
may be wrong before the appliance clock is synchronized.

`GET /auth/clients` (admin) returns `{clients,config_revision}`. Each record contains
only `client_id,name,scopes,created_seq,last_used_utc_best_effort`; `created_seq`
is a persisted increasing sequence, independent of the wall clock. Revoke with
`DELETE /auth/clients/{id}` (admin) and `If-Match: "<config_revision>"` from a fresh
client list. Missing/wildcard revisions return 428; stale revisions return 412.
Revocation immediately invalidates that client's tokens on both listeners and
persists removal of its credential. Other clients keep access. Revocation cannot
cancel a request already authorized before the revocation was processed.
Configuration → General lists paired apps with a secondary Revoke action. Pairing
itself is an API flow; the bridge UI never displays client secrets.

`device_id` is stable across restarts: the first 16 lowercase hexadecimal characters
of HMAC-SHA256 of `astro-bridge-id`, keyed by the 32 random bytes in mode-0600
`install-secret`. It is not derived from network identifiers. Preserve that file
when migrating appliance state; recreating it changes the device identity.

Every native route has the required scope below, also recorded in OpenAPI as
`x-required-scopes`. `admin` grants all scopes. `authenticated` means any valid
Bearer token. Browser cookie/CSRF routes remain administrator-only. An unlisted
route or method requires `admin` by default. `connect` permits only the native
instrument connect/lease/release/disconnect facade, never settings or legacy
administrator connection routes. The planned instrument, telemetry and camera
services remain unavailable until they are implemented; a scope
does not itself enable a service or owner consent.

| Method | Path (relative to `/api/v1`) | Required scope |
| --- | --- | --- |
| GET | `/endpoints` | read |
| GET | `/endpoints/{id}` | read |
| POST | `/endpoints/{id}/release-owner` | admin |
| GET | `/info` | public |
| GET | `/openapi.json` | public |
| POST | `/auth/token` | public |
| DELETE | `/auth/token` | authenticated |
| POST | `/auth/revoke-all` | admin |
| GET | `/status` | read |
| GET | `/config` | admin |
| GET | `/interfaces` | read |
| GET | `/home` | admin |
| PATCH | `/home` | admin |
| GET | `/hotspot` | admin |
| PATCH | `/hotspot` | admin |
| GET | `/settings` | admin |
| PATCH | `/settings` | admin |
| GET | `/telescopes` | read |
| POST | `/telescopes` | admin |
| GET | `/plugins` | read |
| GET | `/events` | read |
| GET | `/wifi/networks` | admin |
| GET | `/telescopes/{id}` | read |
| PATCH | `/telescopes/{id}` | admin |
| DELETE | `/telescopes/{id}` | admin |
| GET / POST | `/stations` | admin |
| GET / PATCH / DELETE | `/stations/{id}` | admin |
| POST | `/telescopes/{id}/connect` | admin |
| POST | `/telescopes/{id}/disconnect` | admin |
| GET | `/discovery/{observation}` | admin |
| POST | `/discovery` | admin |
| GET | `/telescopes/{id}/binding` | read |
| POST | `/telescopes/{id}/binding` | admin |
| DELETE | `/telescopes/{id}/binding` | admin |
| POST | `/telescopes/{id}/ensure` | admin |
| GET | `/protection` | read |
| GET | `/activity` | read |
| POST | `/telescopes/{id}/leases` | admin |
| PATCH | `/leases/{id}` | admin |
| DELETE | `/leases/{id}` | admin |
| POST | `/leases/{id}/release` | admin |
| PATCH | `/telescopes/{id}/owner-lock` | admin |
| POST | `/system/connection-pauses/recover` | admin |
| GET | `/operations/{id}` | read |
| POST | `/credentials` | admin |
| GET | `/system/https` | admin |
| POST | `/system/https` | admin |
| GET | `/system/https/certificate` | admin |
| GET | `/system/updates` | admin |
| POST | `/system/updates` | admin |
| POST | `/system/reboot` | admin |
| POST | `/auth/clients` | admin |
| GET | `/auth/clients` | admin |
| DELETE | `/auth/clients/{id}` | admin |
| GET | `/instruments` | read |
| GET | `/instruments/{id}` | read |
| POST | `/instruments/{id}/connect` | connect |
| POST | `/instruments/{id}/lease` | connect |
| POST | `/instruments/{id}/release` | connect |
| POST | `/instruments/{id}/disconnect` | connect |
| GET | `/instruments/{id}/endpoint` | read |
| GET | `/telemetry/samples` | telemetry |

## Resources

All paths below are relative to `/api/v1`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/status` | Full live state, public config, management URL and default-password indicators |
| GET | `/config` | Redacted saved configuration and its revision |
| GET | `/interfaces` | Detected radios, permanent MAC, current name, bands, AP support and assignments |
| GET | `/wifi/networks?interface=wlan0` | Scan a detected radio; may return `409` during association |
| GET | `/telescopes` | Saved instruments, their target IPs and independent live connection states |
| POST | `/telescopes` | Create a profile; disabled by default; returns `201` |
| GET | `/telescopes/{id}` | Read one profile and connection state |
| GET | `/endpoints` | Read saved profiles and current instrument transport paths |
| GET | `/endpoints/{id}` | Read the same inventory restricted to one profile |
| PATCH | `/telescopes/{id}` | Edit only that profile |
| DELETE | `/telescopes/{id}` | Remove the profile and queue its connection cleanup |
| GET / POST | `/stations` | List station instruments with last reachability / declare one (`id`, `name`, `kind` seestar or dwarf, `target_ip`, `client_connect`); returns `201` |
| GET / PATCH / DELETE | `/stations/{id}` | Read / edit / remove one station instrument; removal is refused (`409`) while a reservation holds it |
| POST | `/telescopes/{id}/connect` | Activate the profile; returns `202` |
| POST | `/telescopes/{id}/disconnect` | Disable manual reconnect intent; returns `202` |
| POST | `/telescopes/{id}/ensure` | Actual ready endpoint (`200`) or explicit free-radio operation (`202`); actual revision required for mutation |
| POST | `/endpoints/{id}/release-owner` | Release isolated UDP ownership; administrator scope and actual revision required; `{id}` is the profile ID |
| GET | `/operations/{id}` | Bounded current-run connection outcome and current profile-scoped endpoint |
| GET / PATCH | `/home` | Read / partially edit home Wi-Fi |
| GET / PATCH | `/hotspot` | Read AP state / set optional-AP preference |
| GET / PATCH | `/settings` | Read / edit hostname, country and explicit Auto-connect policy |
| POST | `/credentials` | Change the single bridge password (web interface, hotspot, Linux account) |
| POST | `/system/reboot` | Schedule a reboot; returns `202` |
| POST | `/discovery` | Non-disruptive Wi-Fi observations or an existing attached protocol response |
| GET / POST / DELETE | `/telescopes/{id}/binding` | Read, correct/add/remove aliases or unlink physical metadata; actual revision required for writes |
| GET / POST | `/system/https` | Cached diagnostics / one explicit certificate action; POST returns `202` |
| GET | `/system/https/certificate` | Authenticated public CA PEM download, never private keys |

POST/PATCH bodies must be JSON objects, at most 32 KiB, without chunked transfer.
Connection commands, revoke-all and reboot take `{}`. DELETE may omit its body.
Unknown request fields are rejected. Responses never return saved Wi-Fi passwords,
Linux passwords or the administrator hash. `has_password` indicates a saved secret.

### Confirmed connection activity

`GET /api/v1/events` is an authenticated bounded replay and long-poll resource. The web interface
uses the same journal through session-authenticated `GET /api/events`; it does not
use cookies on the versioned API or relax its Origin policy. Responses are `no-store`.

The initial read returns the latest 50 events. To replay changes, pass
`?after=<last-sequence>&boot_id=<previous-boot-id>&limit=100`. Limits are 1–100;
`after` is a non-negative integer, and `boot_id` is a 32-character lowercase hex
service UUID. Unknown, repeated or malformed parameters return 400
`INVALID_EVENT_CURSOR`. Both routes accept integer `wait=0..25` seconds (default 0).
A current cursor with no available events waits on the shared journal condition;
append wakes it immediately. Initial snapshots and reset pages return immediately.
Only four requests may be parked across HTTP and HTTPS; excess requests receive
an immediate page. Timeout returns an ordinary empty page with the same cursor.
Global shutdown releases all waiters; rotating only the HTTPS listener does not
close the shared journal. Reads do not manufacture link transitions; expired lease
observations may publish their once-only lifecycle event.

The response contains `boot_id`, `oldest_sequence`, `latest_sequence`, `next_cursor`,
`has_more`, `gap`, `reset_reason`, `persistent`, `history_available`, `clock_status`
and `events`. Continue from `next_cursor` while `has_more` is true. A missing cursor
is a bounded recent snapshot. A retention gap, cursor ahead of the journal, service
restart or unavailable old history explicitly resets to the latest bounded snapshot;
refresh current status instead of replaying those old events as new notifications.
Reset reasons are `RETENTION_GAP`, `CURSOR_AHEAD`, `BOOT_CHANGED` and
`HISTORY_UNAVAILABLE`. A valid current cursor keeps working after a corrupt old
journal has been discarded. `persistent=false` means new events remain available
in memory but cannot currently be written locally.

Each legacy connection event has a sequence, service `boot_id`, UTC `timestamp`, `clock_status`,
`operation_id`, stable `profile_id`, authenticated `profile_name`, opaque `radio_id`,
`type`, English machine-readable `cause`, `reason`, `outcome`, and separate
`associated`/`ready` booleans. Radio identifiers are scoped to the service run;
they are neither adapter MACs nor telescope identities. Persisted event records contain no
IP, SSID, BSSID, passwords, tokens, packet contents or raw exception messages.

Order by **sequence**, never device time: timestamps are best-effort and currently
carry `clock_status="unverified"`. The physical Alpine clock is known to need
repair; this API makes no time-synchronization claim.

Connection commands return an additive `operation_id`. A configuration change that
affects active telescope profiles uses the same path, and a switch shares one
operation ID across the displaced and selected instruments. This is correlation
for event consumers, not a completed operation/reservation/scheduler API. Acceptance
emits `requested`; it cannot emit success. Actual reconcile phases include:

- `disconnecting` and `connecting`: work started, without success implied;
- `disconnected`: absence of the previous association confirmed, including unplug;
- `associated`: the backend confirms its Wi-Fi association/IPv4 criterion, while
  forwarding or telescope service may still be unavailable;
- `waiting`: association, home network or service readiness is still pending;
- `connected`: association and the existing routing/probe readiness criteria passed,
  **after** successful firewall reconciliation;
- `failed`: association attempt, configuration conflict, missing radio or reconcile
  failed; enabled profiles may retry according to existing behavior;
- `restored`: actual readiness returned after an unexpected loss. An intentional
  profile move emits `connected`, not a misleading restoration.

Hotspot recovery additionally emits change-only `hotspot_unavailable` events with
`HOTSPOT_RADIO_UNAVAILABLE` or `HOTSPOT_START_FAILED`, followed by
`hotspot_restored` / `HOTSPOT_RESTORED` only after an actual AP starts again.
These management records use `profile_id:"hotspot"`, an empty `profile_name`,
and an opaque run-scoped `radio_id` when known. Their `associated`/`ready`
booleans describe AP access, independently of instrument readiness. Repeated
unchanged failures and status reads emit no event; initial normal startup is not
a restoration. No adapter MAC, Wi-Fi name or raw service error is journaled.
The example client's `events({after, bootId, limit})` performs short polling;
`waitEvents({after, bootId, limit, wait})` uses the implemented long-poll contract.

`automatic_switch_deferred` is reserved in the event contract for a future policy;
this version does not implement an automatic scheduler. Routing/probe readiness is
not manufacturer-app or physical observing-session acceptance.

Block E also publishes `instrument.lost` on unexpected association loss while
enabled or leased, `instrument.restored` on association recovery, and
`instrument.moved` after association on another opaque radio ID. Explicit stops
and moves do not create loss/restoration notices. `instrument.state` uses the same
link/service projection as the native catalog, including fresh cached discovery,
and coalesces changes to at most one event per instrument per five seconds.
The latest state is emitted on a subsequent reconcile once the interval elapses.
`lease.expiring` occurs once per lease version when 60 seconds or less remain;
`lease.ended.reason` is `EXPIRED` or `RELEASED`. Lease events carry no client identity.

`instrument.endpoint_changed` records only an instrument ID and the common event
metadata. On authenticated read, its `endpoint` is hydrated from the current
snapshot for the accepted socket's management path, or null if unavailable. The
endpoint is current information, never historical reachability evidence, and its
addresses are never persisted in the journal. Telemetry's three named event types
are implemented; attached cameras add `camera.detected`, `camera.removed` and
`camera.power_unstable`. See [block E](NATIVE-CLIENT.md#e-events-and-recovery)
for the allowlisted native payloads. The browser activity view and push notices
retain the legacy presentation and ignore additive native records.

The live ring retains 256 events; the root-only `events.json` retains up to 1,024
(mode 0600, maximum 1 MiB). Atomic disk writes are batched at least ten monotonic
seconds apart, including failed attempts. Reconcile flushes pending batches even
when no new event arrives. After the initial empty journal, a deterministic 1,200-event test measures six
batch writes at 10, 20, 30, 40, 50 and 60 seconds, with no extra write at shutdown.
Read-only replay merges the disk tail with the live ring without retaining a second
large RAM journal. More than 256 events before a batch can evict unflushed records;
internal holes produce `RETENTION_GAP`, never silently skipped replay. Shutdown
preserves the write-rate bound, so the last unflushed interval can be lost. The
current-profile sequence index is bounded to 24 IDs and keeps catalog `since_seq`
from decreasing when a quiet instrument's last event leaves the live ring.
Sequence continues across service restarts while valid retained storage is available;
a fresh service UUID detects restart or lost/corrupt history. Deleted profiles'
recent names remain in this bounded authenticated history. In-memory profile/radio
tracking is purged after confirmed disassociation and profile removal.

The UI polls every three seconds with at most one event request in flight and
bounded pagination, avoiding long-lived SSE request slots. It shows one bounded
recent panel from all main tabs and groups foreground notifications by operation.
Confirmed outcomes trigger a fresh status read before notifications, so cards and
toasts describe the same actual state. Only the cursor is retained in browser session
storage; no event names, status responses or credentials are cached. Reloads and
independent tabs do not create server events or duplicate entries within the panel.
Logout/expired authentication clears event names and pending toasts, and stale
in-flight responses cannot repopulate them. Notifications require this page to remain
open. Optional background Web Push is separate, with explicit per-device consent,
category preferences and strict internal click routes; see PWA notifications.
Browser `/api/push` uses the existing authenticated HTTPS cookie/CSRF transport; it
does not add native v1 token fields or change this event schema.

### List and configure instruments

`GET /telescopes` returns this shape (fictional example):

```json
{
  "api_version": "1",
  "config_revision": "configuration-digest",
  "telescopes": [{
    "id": "terrace",
    "name": "Terrace instrument",
    "kind": "vespera",
    "ssid": "Telescope-DEMO",
    "mac": "02:00:00:00:00:01",
    "has_password": false,
    "target_ip": "10.0.0.1",
    "enabled": true,
    "connection": {
      "id": "terrace",
      "state": "online",
      "connected": true,
      "relay_ready": true,
      "service": {"state": "ready", "checked_at_seq": 1, "method": "vespera_tcp", "failures": 0},
      "target_ip": "10.0.0.1",
      "client_ip": "10.0.0.2/24",
      "interface": "wlan0",
      "subnet": "10.0.0.0/24",
      "legacy_ports": true
    }
  }]
}
```

`target_ip` is the instrument's address. `connection.client_ip` is the **Pi's**
address on that instrument network and includes a CIDR prefix. `enabled` is the
saved reconnection preference; `connected` is actual Wi-Fi association and IPv4;
`relay_ready` describes routing/probe readiness, not manufacturer-app validation.
Home `connection.client_ip` is the Pi's plain home IPv4, without a CIDR prefix.

Every live instrument connection also exposes `service` independently of the
existing association, `relay_ready` and event `associated`/`ready` meanings:

```json
{"state":"not_answering","checked_at_seq":7,"method":"tcp_connect","failures":2}
```

`state` is `ready`, `not_answering`, `not_probed` or `unknown`. `checked_at_seq` is
the process-local, monotonically increasing service-check sequence, **not** a
journal cursor or timestamp; it is null before a check. It resets on restart.
`method` is `tcp_connect`, `udp_identity`, `vespera_tcp`, or null for an unprobed
family. `failures` counts consecutive failed checks and resets on success; an
unknown result does not add a failure. Cached or skipped checks leave all fields
unchanged. A changed target/radio/address clears the previous result, while
retaining the per-profile retry deadline. A lost association also clears its
cached result. Status and profile GET requests never initiate probes.

ZWO mounts use a TCP connect-and-close on 4030, Seestar on 4700 (two-second socket
limit, no payload). SynScan reuses the bounded, read-only motor-board version
identification exchange on UDP 11880 with strict reply origin/shape checks. These
new probes run at most once per 30 seconds per saved profile, skip active relay
traffic (including a raw-active activity sample), and allow only one in flight per
radio. Isolated mounts use their existing radio namespace. DWARF and generic
remain `not_probed`; demo mode never sends these probes. Vespera retains its
existing TCP 8082 probe and scheduling, mirrored into this metadata.

A failed service check never disconnects Wi-Fi or changes legacy forwarding
readiness. Thus `connected:true`, `relay_ready:true`, `service.state:"not_answering"`
is valid; the UI displays **Wi-Fi connected · not answering**. TCP acceptance is
not proof of application compatibility or physical instrument readiness.
`service_changed` events use `SERVICE_READY`, `SERVICE_UNAVAILABLE` or
`SERVICE_UNKNOWN`, with `outcome` carrying `ready`, `not_answering` or `unknown`
(all three are included in the frozen OpenAPI schema). They are emitted
only when a new-probe state changes, never for a repeated failure or status read;
their existing `associated`/`ready` booleans retain legacy meanings. Vespera keeps
its existing connection-event path. These checks are documented, not verified on
hardware; real mount timing belongs to P4.


Create an inactive profile:

```http
POST /api/v1/telescopes
Authorization: Bearer <token>
Content-Type: application/json

{
  "name": "Terrace instrument",
  "kind": "vespera",
  "ssid": "Telescope-DEMO",
  "mac": "02:00:00:00:00:01",
  "open": true,
  "target_ip": "10.0.0.1"
}
```

Required fields are `name`, `ssid`, `mac` and `target_ip`, plus a valid `password`
or explicit `open: true` for an open network. `kind` defaults to `generic` and may
be `generic`, `vespera`, `dwarf` or `seestar`. The type does not certify app support.
An optional stable `id` may be supplied; otherwise the server generates one.
Up to 24 profiles may be saved. Configure home Wi-Fi before creating instruments.

IDs use 1–32 lowercase letters, digits or hyphens, starting with a letter/digit;
names use 1–64 characters. SSIDs use 1–32 UTF-8 bytes. Target addresses must be
private IPv4 addresses. Wi-Fi passwords accept 8–63 UTF-8 bytes or 64 hex digits.

`PATCH /telescopes/{id}` supports `name`, `kind`, `ssid`, `mac`, `password`, `open`,
`bssid`, `hidden`, `target_ip` and `enabled`. IDs cannot be edited. Omitted fields
are preserved. An empty/omitted password retains the previous password only for
an unchanged SSID. If the SSID changes, supply a password or explicit `open: true`.
Profiles with missing adapters retain their assignments but cannot be activated
until that adapter is present. A newly assigned MAC must identify detected hardware.

### Switch a shared radio

`POST /telescopes/{id}/connect` with `{}` saves `enabled: true` for the selected
profile and `false` for other profiles using the **same MAC**. Home Wi-Fi is not
restarted. Creating/editing an enabled profile uses the same exclusivity rule.
Other radio assignments remain unchanged. `disconnect` disables that profile's
reconnection; it does not select another saved instrument automatically.

A successful command means **accepted**, not already connected. Poll the profile
or `/status`, for example every five seconds. Live states include `pending`,
`saved`, `waiting`, `missing`, `connected`, `online`, `waiting_service`, `conflict`,
`invalid_ip` and `recovery`. Render state keys rather than parsing human messages,
which currently use French. Confirmed changes can also be replayed through `/events`.
There are no WebSocket notifications or completed scheduler/job resources.

One radio is reserved for home. Each simultaneous instrument needs another radio
and a non-overlapping subnet. Overlapping profiles can be saved and switched, but
identical instrument subnets cannot be routed simultaneously in this version.
Optional AP use yields to instrument assignments; recovery AP takes priority when
home fails. A scan on the AP radio may fail: try another present radio and filter
its scan results to the selected adapter's supported bands.

### Home, radios and hotspot

`GET /interfaces` lists present radios. Assign using permanent `mac`; use the
current `name` only for scanning. `assignments` contains desired home/telescope
roles and the active or explicitly reserved AP role. `available_for_telescope` identifies a non-home radio
without an enabled instrument reservation (an optional AP can be released).
`can_assign_telescope` also includes occupied non-home radios that can be reused
for an explicit profile switch. A radio is reserved even if its enabled instrument
is currently powered off.

`PATCH /home` accepts the Wi-Fi fields above, with the same password-retention
rules. For first configuration, supply SSID, MAC and password/open choice. This
can change the management IP and interrupt the requesting connection. Save the
intended hostname and rediscover/reconnect before retrying. A lost HTTP response
is not proof that a write failed.

`PATCH /hotspot` accepts `enabled`, `ssid`, and/or `mac`. A custom `ssid` uses 1–32
UTF-8 bytes; an empty string restores the automatic device-name/AP-MAC-suffix
name. Changing the name requires the actual current `If-Match` revision and
respects connection protections. It restarts the hotspot and disconnects its
clients; reconnect to the renamed network. `ssid` in the response is the live
name; `configured_ssid` is the saved override (empty for automatic naming).
`mac` is the permanent MAC of the preferred AP-capable radio; empty selects
automatically. Legacy omitted selections remain automatic. A newly selected
adapter must be present and AP-capable, separate from home Wi-Fi and, when the
hotspot is requested, from enabled telescope profiles. A previously selected
missing adapter may be retained in saved configuration. Changes require the
actual current `If-Match` revision and respect the existing protection guards.
The UI saves the name and adapter together; an applied change may disconnect
hotspot clients. A busy/unavailable selection never takes over another connection:
`selected_adapter_unavailable` is reported while any working AP is retained.
When the selected adapter is absent or AP startup fails, an enabled hotspot may
use another free compatible Auto/hotspot radio within the same reconcile cycle,
including while home Wi-Fi remains available. Each eligible radio is attempted
at most once per AP reconciliation. The preferred MAC and role reservation stay
saved; the AP returns to that adapter when it is present and eligible again.
Other role reservations, enabled instrument assignments and connection protection
remain enforced. A working fallback stays active while the preferred adapter is
unavailable. If every start fails, `start_failed` is reported and the next cycle
retries without discarding working home/instrument paths. A failed Linux start
releases its partial AP ownership before another radio is attempted. A requested
fixed AP radio is reserved from telescope allocation even before the AP starts.
Omitted fields are preserved. Its response distinguishes
saved preference from live `connection.active` and `connection.reason`, including
`no_free_radio`, `subnet_conflict`, `requested`, `disabled` and `recovery`.
`PATCH /settings` accepts `hostname` (without `.local`), country code and strict
boolean `auto_connect`. Global `auto_connect` and per-profile `auto_eligible` both
default to false; omission preserves existing values. They authorize explicit
profile-ID ensure requests, independently of manual `enabled` reconnect intent.
Hostname changes require opening the new address; sessions are tied to the appliance
service, but native tokens can be reused at the new accepted management address.

### Passwords and reboot

`POST /credentials` changes the **single bridge password**:

```json
{"current_password":"current-bridge-password","password":"new-bridge-password"}
```

One password protects the web interface, the hotspot and the Linux account
(**root** on the Raspberry Pi 5 image, **bridge** on the Pi Zero W and live
images; none on other installations). One request changes all of them, all or
nothing: the Linux account is changed first and, if that fails, nothing else
changes. `target` is optional; `all`, `web`, `hotspot` and `linux` (sent by
earlier Setup versions) all change the three together. `linux` is refused where
the bridge has no Linux account. The new password has 8 to 63 printable ASCII
characters without spaces (no accented letters) and cannot be `12345678`.
The request is refused while a reservation or an owner lock protects a
connection. The reply keeps its format: `sessions_invalidated` (every session
ends, paired apps keep their own credentials) and `hotspot_reconnect` (the
hotspot restarts with the new key; each phone joined to it must join again).

Nothing is stored in clear: the web interface keeps a salted hash, the hotspot
keeps only its derived WPA key bound to its network name (earlier installations
are migrated at start), the Linux account keeps its own crypt hash. Renaming the
hotspot after a personal password was chosen needs `current_password` in the
settings save, so the key can be derived for the new name (`409 PASSWORD_REQUIRED`
otherwise). A first administrator password given in a settings save
(`admin_password`) is the single password too.

`default_credentials.password` is the one flag "the initial password is still
accepted somewhere"; `web`, `hotspot` and `linux` repeat it for earlier clients.
`ssh` in `/status` reports what SSH really accepts: `{"account":"root"|"bridge"|null,
"password_login":true|false|null}`, written by the image rule at each SSH start
(`null` where no image rule runs). SSH accepts the password only once it is no
longer the initial one; key login is unchanged.

`POST /system/reboot` with `{}` returns `202`. Service restart revokes tokens and
requires reauthentication after the bridge returns. Neither command waits for
Wi-Fi reconnection or a complete physical reboot.

## Concurrency, failures and retries

Resource reads include `config_revision`, an opaque keyed digest of saved configuration.
Revisions also change after a service restart; reload after reauthentication.
To avoid overwriting another client's change, send `If-Match: "<config_revision>"`
on telescope create/edit/delete/connect/disconnect and home/hotspot/settings PATCH.
The check and save run under the same controller lock. This is a **global config**
revision, not an instrument-only version. Live DHCP/probe changes do not change it.
A stale write returns `412 PRECONDITION_FAILED` without applying any change.
Preconditions are optional for compatibility; reload and merge before retrying.

Errors are objects, unlike the legacy UI API:

```json
{"error":{"code":"SCAN_UNAVAILABLE","message":"Radio unavailable or busy. Retry after the current association."}}
```

| Status | Handling |
| --- | --- |
| 400 | Correct malformed JSON, unsupported fields or validation errors |
| 401 | Token absent, expired or revoked; authenticate again |
| 403 | Unaccepted browser Origin |
| 404 | Route or telescope not found |
| 405 | Unsupported method |
| 409 | Busy radio/network operation, or duplicate profile ID |
| 412 | Reload config, merge the intended edit, retry with its new revision |
| 421 | Use an accepted management hostname/IP |
| 429 | Login/session limit; back off before retrying |
| 500 | Internal error; inspect appliance logs |

Network errors and timeouts can leave writes applied but their responses lost.
Reload before retrying; use caller-supplied stable telescope IDs to reconcile an
ambiguous creation (`409 TELESCOPE_EXISTS` means inspect that profile). Do not
blindly repeat credential changes or reboot commands. Allow slow ARMv6 password
hashing and Wi-Fi association; the example client defaults to a 60-second timeout.

## Example client

```js
import { AstroBridgeClient } from './astro-bridge-client.mjs';

const bridge = new AstroBridgeClient('http://astro-bridge-12abcd.local');
await bridge.login(webPassword); // Supplied by the user; do not log it or the result.
const { interfaces } = await bridge.interfaces();
const { telescopes, config_revision } = await bridge.telescopes();
await bridge.connectTelescope(selectedProfileId, config_revision);
// Poll bridge.telescope(selectedProfileId) until its connection state is ready.
```

`connectTelescope()` uses the existing `/telescopes/{id}/connect` route;
`connect()` uses the implemented `/instruments/{id}/connect` route in the
[native client contract](NATIVE-CLIENT.md#javascript-helper-and-versioning).

The fetch-based module includes CRUD, scans, home/hotspot/settings, credentials,
logout, revoke-all and reboot. It omits cookies, preserves structured error codes,
and clears its in-memory token after `401`, logout or web-password rotation. It
is an integration example, not a complete mobile application or secure-storage layer.
The OpenAPI JSON is the language-neutral contract for other client stacks.

For Seestar profiles, live `connection.seestar_proxy` reports selection for the
native-app LAN discovery and TCP proxy. A single connected Seestar is selected
across the confirmed home/AP management paths. The native app uses the matching
Pi management IP; `target_ip` continues to
report the real instrument IP. This does not change API authentication.

## Instrument endpoint inventory

`GET /api/v1/endpoints` requires a Bearer token with `read` or administrator scope.
`GET /api/v1/endpoints/{profile_id}` returns the same envelope with one entry;
an unknown profile returns `404 TELESCOPE_NOT_FOUND`. Browser sessions use
`GET /api/endpoints` and `/api/endpoints/{profile_id}` with the same fields.
These read-only snapshots initiate no scan, service probe, connection, route
change or reservation. The example client exposes `endpoints()` and
`endpoints(profileId)`.

```json
{
  "api_version": "1", "config_revision": "opaque-current-revision",
  "boot_id": "00000000000000000000000000000000",
  "endpoints": [{
    "profile_id": "mount-one", "kind": "zwo_mount",
    "target": {"ip": "192.168.4.1", "ports": [4030]},
    "paths": [{
      "path": "home", "host": "192.168.12.20", "port": 14030,
      "transport": "tcp", "service": "lx200", "role": "control", "protocol": "lx200",
      "ready": true, "service_state": "not_answering", "requires_client_route": false
    }]
  }]
}
```

Every saved profile is listed in saved order. Disconnected, disabled or missing
live profiles have `paths:[]`. `target.ip`/`ports` describe the instrument;
each path's `host`/`port` is the actual client destination. `path` is `home` or
`hotspot`; choose a path available to the client. Refresh after reconnection,
DHCP/configuration changes or a changed `boot_id` (the existing service/event
epoch). DHCP changes do not change the global configuration revision.

Isolated mounts publish their dedicated TCP/UDP ports on available management
paths and never publish direct routes. Native shared proxies publish `lx200`,
`synscan`, `seestar_native`, `vespera_http` (8082), `vespera_socketio` (8083) or
`dwarf_ws` (9900), `dwarf_http` (8082) or `dwarf_rtsp` (554). Other declared
instrument ports remain direct-route entries; no HTTP/WebSocket protocol is
invented for them. Since native contract 1.1 every path also carries the plugin's
declared `role` (`control`, `preview`, `imaging`, `rest`, `stream`, `other`) and
application `protocol` (see [port roles](PLUGINS.md#port-roles-native-contract-11));
undeclared ports report `other`/`unknown`. Routed profiles additionally
publish `direct_route` entries to the instrument IP with
`requires_client_route:true`: the client/router must provide the route. The
bridge does not verify that route. Generic profiles without declared ports
expose their target IP with empty port/path lists rather than inventing a port.

`ready` describes forwarding/listener readiness, independently of
`service_state` (`ready`, `not_answering`, `not_probed`, `unknown`). A ready
forwarding path can lead to an instrument that is not answering. Isolated UDP
paths additionally carry `owner:null` or `owner:{address,since_seq}`, the existing
shared controller ownership snapshot, and `refused_clients` (0–9007199254740991).
This counts refused datagrams, including repeats from one source; it is shared
per profile and repeated on home/hotspot paths, so do not sum those values.
The count survives owner release/expiry but resets on profile teardown/service
restart; it is never persisted. TCP paths carry neither ownership field. No
SSIDs, BSSIDs, radio MACs, passwords, raw manufacturer payloads or authentication
material are included. The inventory
is tested in simulation; phone/path/maker-app acceptance remains documented,
not verified on hardware.

## Multi-instrument isolation

See multi-instrument isolation. Isolated Vespera
profiles (`isolated: true`, one radio each) get a bridge-assigned, persisted pair
`client_ports` relaying TCP 8082 (HTTP) and 8083 (Socket.IO); owners cannot choose
them. `client_port` repeats the first. `client_endpoint.alternatives` keeps one
entry per path for the first port and `client_endpoint.services` lists every
relayed port (`service` and `target_port` on each). The inventory and native
endpoints list both ports with their roles. Validated with synthetic radios only.

## Hotspot client inventory (0.5.0)

Authenticated status and hotspot reads include `clients` and `clients_available`
in the hotspot connection object while the AP is active. Each client contains
`mac`, `ip` and `hostname`. Linux obtains current stations from `iw station dump`
and enriches them with matching unexpired `hotspot.leases` entries. Old DHCP
leases alone never count as connected clients. A station without a usable lease
has empty IP/hostname fields. `clients_available: false` means inspection failed,
not that no clients are connected. No hotspot password is included.

The Pi's own relay readiness is not proof that the home router has a static
route configured. The UI reports that router-route state as unverified; this
release does not add a router-management integration or a route-verification API.

## Select a destination connection (0.5.1)

`POST /api/v1/telescopes/{id}/connect` accepts an optional body:

```json
{"mac":"02:00:00:00:00:02","expected_mac":"02:00:00:00:00:01"}
```

`mac` is the destination radio's permanent MAC. Omitting it keeps the existing
assignment. Moving a profile and enabling it is one atomic configuration write:
other enabled profiles on the destination are disabled, and the previous radio
is released during reconciliation. Saved passwords and home Wi-Fi settings are
preserved. The selected destination must be present and cannot be the home radio.
`expected_mac` rejects a stale source assignment (HTTP 400 validation error);
`If-Match` remains the stronger optional global configuration precondition.
Poll live state after HTTP 202. Successful acceptance is not proof of association
or maker-app access, and the overlapping-subnet guard still applies.

The example client supports `connect(id, revision, {mac, expected_mac})`.
An application should explain and confirm a move from another enabled connection,
including any destination instrument that will be disconnected. The web UI does
so before issuing the request; canceling the dialog sends no network change.

### Unassigned saved profiles

An inactive telescope can be created without `mac` (or with `mac: ""`). The
normalized profile contains an empty MAC and `enabled: false`. Connecting that
profile requires a destination `mac` in the connect action; `expected_mac: ""`
can guard its initial unassigned state. An enabled profile always requires a
real non-home radio. Existing assigned profiles remain compatible.


### Application update maintenance

Update status additionally reports `current_build_id` (null for an unpackaged
source checkout), `supported` and `maintenance`. During prepared/applying/verifying
transactions, authenticated writes return 409 with `UPDATE_IN_PROGRESS`; a corrupt
transaction returns `UPDATE_RECOVERY_REQUIRED` and recovery_required update state.
Reads/authentication/logout remain available. Browser writes retain Origin/CSRF
requirements. A rolled_back state means previous application/config files were
restored; `rollback_service_ready=false` explicitly means service restart control
failed. Physical management recovery still requires readback.

The localhost-only `/api/update-health` returns process-loaded version/build_id
and positive update_safe, alongside management/telescope readiness. It is not a
public update/import endpoint. Updater2 requires explicit safety and never treats
missing fields as idle. See the application OTA notes for platform/legacy compatibility,
owner-local delivery, OS prerequisites and the existing 0.6.0 bootstrap gate.


## Optional secure local access

`GET /system/https` is a cheap cached read. `supported` describes the fixed OS
helper capability, not client trust; `listener.ready` describes the actual HTTPS
listener. `identity.state`, CA/leaf UTC expiry and SHA-256 fingerprint describe
public certificate observations. `diagnostics_stale` and
`diagnostics_age_seconds` qualify that snapshot: `current_clock` is the clock
observation when diagnostics ran, not continuous NTP proof. Historical
`issuance_clock_evidence` remains historical. Expired/hostname-mismatched identities
may expose their public CA fingerprint for removing old client trust; that does
not validate the chain or enable HTTPS.

POST an object with `action`: `setup`, `renew`, `enable`, `disable`, `reset` or
`refresh`. All five mutations require `current_password` and share the credential
change limit of five password checks per minute. `reset` additionally requires
`confirm_reset: true`. Only setup/renew may contain `owner_confirmed_utc`, an
integer UTC epoch independently verified by the owner for **that operation only**.
The helper compares it with bridge UTC (within 120 seconds); this neither sets the
clock nor proves NTP synchronization. Omit it only when the helper can establish
positive current chrony synchronization. Unknown fields/types are rejected.

The bounded `operation` contains an opaque ID, fixed action, state and stable code;
`202` means accepted, not completed or ready. Poll GET until state is `succeeded`
or `failed`, then assess listener readiness separately. One operation runs, with
no queue; refresh during an existing job coalesces to that job. Further diagnostic
refreshes are limited to one per 30 seconds. No helper or OpenSSL is executed by
ordinary status polls. Pending certificate work blocks configuration mutation and
sets `update_safe` false; OTA maintenance rejects certificate operations.

Versioned failures use `{error: {code, message}}`, including `INVALID_FIELDS` (400),
`REAUTHENTICATION_FAILED` (403), `REAUTHENTICATION_LIMITED` and
`HTTPS_REFRESH_LIMITED` (429), and `HTTPS_BUSY`, `UPDATE_IN_PROGRESS`,
`HTTPS_UNSUPPORTED`, `SYSTEM_SHUTTING_DOWN`, `HTTPS_CLOCK_UNVERIFIED`, `HTTPS_OPERATION_FAILED` or
`HTTPS_CERTIFICATE_UNAVAILABLE` (409). Async failures appear as `operation.code`.
Requests retain the existing authentication, Host/Origin and browser CSRF rules.
The browser equivalents are `/api/https` and `/api/https/certificate`.

The public CA download is authenticated, `application/x-pem-file`, attachment
`astro-bridge-ca.crt`, `Cache-Control: no-store`. It is outside the service-worker
allowlist. Before installing trust, compare its fingerprint through the physical
console or already trusted/pinned SSH; a fingerprint on the same HTTP page is not
independent verification. No endpoint installs/removes OS/browser trust, accepts
arbitrary certificate paths or returns CA/server private keys.

## Safe discovery and separate physical bindings

POST `/discovery` with `{ "mode": "wifi" }` prefers a free radio, then the
owned connected home station, then an owned instrument station only with fresh
quiet/no-TCP/no-raw-activity telemetry and no protection. It never disconnects a
radio or scans an AP. If fresh scanning cannot run, read-only cached NetworkManager
results are returned. `scan_source` is `fresh`, `connected_station` or `cached`;
the latter does not confirm current radio availability. No cached results and no
scan-capable station yields `NO_SPARE_RADIO`. Maintenance/update/operation guards
remain enforced. `{ "mode": "reachable" }` queries only supported existing,
directly attached associated profile targets through reviewed read-only adapters.
No subnet sweep, association or arbitrary URL is accepted.

Results distinguish Wi-Fi observations from inferred family/defaults. Optional
`suggested.password` is a documented public factory password from the plugin,
never a stored owner's credential. The opaque `candidate_id` remains usable for
physical binding for120 monotonic seconds; the interactive15-second rate limit
is unchanged. Background cached observations do not invalidate this snapshot.

GET `/discovery/{observation}` requires Bearer authentication and returns a retained
Wi-Fi observation with current plugin defaults. `observation` is32 lowercase hex
characters; absent/invalid IDs return404 `OBSERVATION_UNAVAILABLE`. Up to64 private
SSID/BSSID/signal/frequency/security/time observations survive app restart, with
no stored password or verified identity. The browser cookie equivalent is GET
`/api/discovery/{observation}`. New-unsaved-device notifications use only the fixed
`/#configuration/telescopes?add=<observation>` destination. Authenticated current
state still controls form access and saving; an old notice never connects a radio.

Authenticated status includes `nearby_telescopes`: `candidates` (SSID, plugin kind,
signal and opaque observation ID only), `scan_age_seconds` (null before a fresh
scan), `interval_seconds:45` and an observation `reason`. Status reads are memory
only. Automatic scans require no selected/connecting/connected profile and a
positively idle adapter outside home, hotspot, reservations and protected profiles.
They never fall back to scanning a connected station; their command budget is10s.
The observer retries temporary maintenance contention after5s, otherwise runs every45s.
Manual scan fallback behavior remains unchanged. Background reads preserve the
current physical-binding tokens. Fresh homepage suggestions exclude saved/unknown
SSIDs, deduplicate by SSID and expire after120s; persisted notification lookup is
separate and remains usable after restart. A connection selection immediately
suppresses suggestions before the next network reconcile.

GET `/telescopes/{id}/binding` reads the redacted profile. POST binds using
`candidate_id` or owner-confirmed SSID/BSSID aliases; `append_aliases:true` preserves
existing validated observations. `remove_bssid` removes one alias; DELETE `{}`
unlinks. Writes require the actual quoted current If-Match revision (428 if absent
or wildcard, 412 if stale). The same actual-revision rule applies to a mutating ensure request. Conflict
409 `BINDING_CONFLICT` includes `conflicting_profile_ids`; explicit replacement
requires `confirm_replace:true` and the exact list. Aliases are capped at eight;
Wi-Fi-observed aliases can include a last-seen MHz hint, never guessed for manual
bindings. No caller can assign verified provenance, serial, arbitrary path or URL.

The profile ID, bridge adapter `mac`, association selector `bssid`, physical AP
aliases, future client observation and future reported identity are different
concepts. Binding/unlinking changes metadata only, including during later reconcile;
it does not move/connect an instrument or change its saved selector/password.
Full config saves preserve omitted or unchanged binding metadata; changing it uses
the dedicated endpoint. The browser has equivalent authenticated/CSRF routes.
The JavaScript client offers `discover`, `binding`, `bind` and `unlink` methods.
See P5A contract and limitations, including explicit
correction, save-then-bind partial failure and older-app metadata rollback limits.
Explicit ensure is now available as described below; leases and eviction remain unimplemented.


## Explicit profile-ID ensure

`POST /api/v1/telescopes/{id}/ensure {}` reads current association/physical selector
and returns `200 {state:"ready",profile_id,endpoint,...}` only for the actual usable
profile. This read-only fast path does not require automation consent or If-Match.
For new allocation, send the actual quoted `config_revision` in If-Match (428 for
missing/wildcard, 412 for stale). Both global `auto_connect` and that telescope's
`auto_eligible` must be true. Existing connect/disconnect remains the manual path;
ensure refuses an enabled but unready profile's existing manual reconnect intent.

New work returns 202 with an opaque current-run `id`, `status_url`, `state:"running"`,
`phase` and `reason`. Acceptance is not readiness. Poll the returned GET URL with
normal bearer authentication. Equivalent pending requests for the same profile
and original/current revision coalesce to the same ID; conflicting requests return
409. Records expire after ten minutes and are capped at 64; expired records return
410 `OPERATION_EXPIRED`, an earlier service epoch returns `OPERATION_RUN_CHANGED`.
Do not retry a mutation blindly after losing its response: read current status first.
There is one active operation, no queue, and five new requests per minute per
session (429 `ENSURE_LIMITED`); coalesced/ready reads do not consume that quota.

The browser uses authenticated CSRF POST `/api/telescopes/ensure` with
`{id,config_revision}` and GET `/api/operations?id=...`. Browser status exposes the
latest bounded `connection_operation`; reads never emit events or start work.
The fetch client provides `ensure(id,revision)` and `operation(id)`.

Allocation scans only confidently free managed radios under the network lock,
protecting home/emergency AP, existing associations, enabled owners and pending
management work. Actual observed band must fit the radio. A saved selector must
agree with physical aliases; without one, exactly one matching observed AP is
required. Ambiguous APs, already-owned physical APs and overlapping target/lease
subnets are rejected. Last-seen alias frequency is only a selection hint. An unbound
profile may explicitly ensure, but scan/association is not manufacturer identity
verification. See P5B behavior and limits.

Only confirmed success saves selected `mac`, observed `bssid` and `enabled:true`.
Transient intent is never persisted or replayed after restart. Failure clears only
the attempt, preserves current saved credentials/settings, and reports restoration
separately; unconfirmed cleanup remains isolated and blocks maintenance. Existing
validated relays/proxy ownership remain intact while another free radio is prepared.
Config/manual moves/binding/discovery/credentials/TLS/reboot/OTA mutations return
busy during pending work; reads and logout remain available.

`endpoint` is profile-scoped, with actual target/bridge addresses and a native proxy
only if that profile currently owns it. Historical success is not continuing
availability: `endpoint_current:false` and null endpoint indicate it is no longer
validated. Direct-route availability, manufacturer identity and maker-app acceptance
remain unknown/unverified. The legacy endpoint `service_readiness` annotation retains its Vespera-only
meaning; use the live connection `service` object above for the new ZWO, Seestar
and SynScan evidence. An accepted/ready ensure response does not prove those
services answer. One operation ID correlates real
P3 requested/association/ready/failure events; event sequence orders notifications.
Client reservations and owner locks are available below. Generic maker-app demand
interception outside the explicit Seestar adapter remains unsupported. P6B below
describes consented replacement/demand; a general scheduler remains unimplemented.


## Activity and connection protection

GET `/api/v1/activity` returns cached per-profile allowed-path packet/byte matches,
established forwarded TCP count, generation, age and unsupported/unknown/active/quiet
state. It does not launch commands per client and never proves an exposure ended.
Quiet requires120 continuous monotonic seconds; cooldown60, sample5, stale15.
An initial/adopted unclassified association, missing OS support, classification gap,
uncertain association or lost counter history cannot qualify as idle. See
P6A contract for kernel attribution, bootstrap and bounds.
There is no eviction in this version. App OTA cannot install conntrack OS support.

GET `/protection` reports `leases` (max32) and `owner_locks` (profile IDs, max24).
Each lease has opaque32-hex `id`, `profile_id`, integer `version`,
`remaining_seconds` and `owned` relative to this authenticated session. No client
identifier or token is returned. Browser status uses the same ownership context.

POST `/telescopes/{id}/leases {ttl_seconds:120}` requires actual quoted current
If-Match (missing/wildcard428, stale412). It protects a currently ready/applied
connection, never starts one; one lease/profile/session, max8/session. TTL must be
an integer30–900. Repeated creation returns the existing lease without extending it.
PATCH `/leases/{id} {version,ttl_seconds}` renews the owning session's lease and
increments its version. DELETE `/leases/{id} {version}` releases its own lease.
Another session receives403 LEASE_NOT_OWNED; stale version412 LEASE_VERSION_CHANGED;
expired/restarted record410 LEASE_EXPIRED. No lease is automatically renewed by the
browser merely because a tab remains open.

Logout, token expiry/revocation and global browser logout do not imply an exposure
ended: orphaned reservations remain until TTL or explicit owner release. Restart
loses RAM reservations; use a persistent owner lock for reboot-surviving protection.
POST `/leases/{id}/release` requires `{version,confirm_release:true,current_password}`
and actual If-Match. This lets the authenticated owner explicitly release another
client's protection after reauthentication; concurrent renewal invalidates the view.
Password checking shares the existing bounded attempt protection.

PATCH `/telescopes/{id}/owner-lock {locked,current_password}` requires actual
If-Match and reauthentication, persisting `owner_locked`. Full config omission or
unchanged echo preserves this metadata; changing it through CRUD/config is rejected.
Lock/eligibility changes do not reconnect or emit connection-request events.

Protected direct/indirect network edits, moves/deletes/disconnects, home/country/AP
changes, credentials, binding, reboot, mutating TLS and OTA are refused; ready ensure,
read/logout and safe TLS diagnostics remain available. Remove owner locks explicitly
and release reservations first. Owner lock removal does not release client leases.
Positive update_safe includes both protections. Optional AP changes defer; recovery
can use an unprotected radio but still suspends telescope forwarding (P6C open).

Browser GET `/api/protection` and CSRF POST fixed actions reserve/renew/release/
owner_release/owner_lock provide parity. The JavaScript client exposes protection,
activity, reserve, renewLease, releaseLease, ownerReleaseLease and ownerLock. Native
errors retain `{error:{code,message}}`; public responses are no-store. No traffic,
reservation or private identity payload belongs in browser storage/public SW cache.

## Consenting replacement and durable pauses (P6B)

Explicit `ensure` now prefers ready→free→least recently active eligible quiet→advanced
force-first eligible. Victims require separate default-off `auto_replaceable` plus
`auto_eligible`; lease/owner protection, management/AP and actual role validation
always win. `PATCH /settings` accepts `auto_policy` with quiet120/cooldown60 integer
bounds30..3600 and default-off force_first_profile/seestar_demand/vespera_demand/dwarf_demand. Changed policy
requires actual If-Match; enabling advanced flags additionally needs transient
`acknowledge_interruption:true`. No acknowledgement is persisted. Omission preserves.

Operation/events fixed `cause` is AUTOMATIC, SEESTAR_DISCOVERY, VESPERA_CONNECTION or DWARF_DISCOVERY (no packet source identity).
Operation `displaced` gives profile_id/reason; restoration remains running until
actual readiness or a durable failed-restoration hold. Its states include in_progress,
restored, failed and superseded. No automatic hidden retry after failure. An unresolved individual hold excludes its profile/adapter; registry uncertainty
blocks all new allocation. Both block OTA; status `connection_pauses`
explains them. `POST /system/connection-pauses/recover` takes current password,
confirm_recovery:true and actual revision, then pauses ALL current profiles (never
releases leases/locks). Individual explicit guarded actions clear holds. Browser
CSRF action recover_pauses and JS recoverConnectionPauses provide parity.

Maker demand requires explicit owner consent. Seestar uses read-only LAN discovery;
Vespera uses passive incoming TCP initial SYN requests to a configured target IP on
8082/8083. Neither creates an authenticated HTTP session. Both share one global
attempt per max(120s,cooldown), independent of HTTP sessions. A reachable matching
instrument is preserved; otherwise exactly one eligible matching profile is required.
Several Vesperas sharing a target IP require explicit eligibility/selection, never
saved-order guessing. The Vespera signal does not authenticate Singularity or identify
a physical instrument. Packets must reach the Pi (hotspot DHCP route or private-network
router route); after association the client may need to retry. No HTTP reply is
fabricated. Seestar replies only after an actual correlated upstream reply.
DWARF accepts strict generic LAN broadcast Ping discovery (default-off
`dwarf_demand`): it can request the same guarded association operation for exactly
one eligible DWARF, preserving any reachable DWARF. A validated ready DWARF may own
Pi-IP TCP ports 9900/8082/554 on each confirmed management path. Its operation
endpoint reports `proxy.protocol:"dwarf"` and the actual Pi address separately from
`target_ip`. Vespera and DWARF share port 8082: an existing valid proxy owner is
preserved, and a conflicting DWARF reports `native_proxy_reason:"NATIVE_PROXY_CONFLICT"`
without discovery advertisement. Direct target routing may remain available.

In version 0.6.13, `dwarf_demand` enables passive switching only: no UDP Echo
advertisement/reply or instrument discovery query is sent. There is no reply
thread, listener or executable opt-in. Use the Pi IP already remembered by the
native app; first-time IP-free discovery is unavailable. The strict generic
broadcast observer, unique eligible profile rule, explicit consent and all
allocation/protection gates remain unchanged; no TCP demand trigger is added.

`maker_demands.dwarf` adds `passive_only:true` and `replies_disabled:true`. Its
backward-readable `discovery_relay` diagnostic always reports `supported:false`,
`last_result:"REPLIES_DISABLED"`, `last_result_age_seconds:null`,
`relay_plan_available:false`, `passive_only:true`, `replies_disabled:true` and
`maker_app_acceptance:"unverified"`. TCP `proxy_available` still reflects actual
successful reconciliation. This is not a native-app/camera connection detector.
The `dwarf_discovery_relay` capability is withdrawn and `dwarf_passive_demand` is
advertised alongside `dwarf_discovery_demand` and `dwarf_native_proxy`.

Disabling passive demand preserves the active connection, Pi-IP TCP proxy and
explicit ensure/manual actions. No synthesized SSID, STA IP, RSSI or credential
metadata is advertised. The unsafe 0.6.11 virtual-LAN experiment was withdrawn in
0.6.12; 0.6.13 removes replies entirely. Actual manual Pi-IP controls/image and
cached reconnect acceptance are distinct from fresh discovery and from the next
native HOME-triggered switching test. See the DWARF contract.

A ready TCP proxy does not confirm native-app/camera compatibility.
Manual app IP should be an advertised **Pi** management address when its proxy is
available; no home-router change is required for that Pi-IP TCP path. See
DWARF discovery and proxy. Other generic maker-app triggers remain unsupported. Legacy status
`maker_demand` remains Seestar-only; additive `maker_demands.vespera` reports
`supported`, `observer`, reason/age/shared cooldown and protocol `vespera-tcp-syn`.
Disabled/simulation/unsupported raw capture is explicit; no unrestricted fallback.
API/runtime status remains unverified for actual maker-app acceptance. See
P6B contract for crash/current-credential behavior,
quarantine initialization/recovery, activity race and old-image boundaries.

### Field management paths (P6C)

`GET /api/v1/status` adds `management` independently of legacy `home_ip`: none,
home_only, ap_only or dual; at most two confirmed role/interface/IP/subnet paths,
and an explicit home-retry reason. Telescope connection rows add `management_paths`
with `forwarding_ready`. Wi-Fi association (`connected`) alone is not readiness.
A real home-route overlap can leave Wi-Fi connected while all affected forwarding
and endpoints are suspended. All payloads remain authenticated/no-store.

Ensure's profile-scoped endpoint adds `management_endpoints` (role, host, client
subnet, nullable proxy). Existing `proxy` prefers home then AP, and both paths use
the same selected native-proxy owner. This adds no stable per-instrument native port
or automatic client/router route. `field_management_paths` in `/info` identifies
this additive contract. Refresh endpoints after path changes; never substitute an
unrelated profile's Pi-native proxy. See P6C behavior and limits.

### Canonical human messages and UI locale (P7A)

Backend human-readable messages and developer diagnostics now use English;
previously French legacy browser error text was not a stable identifier. Versioned
error/status/reason codes are preserved. Native clients should branch on those
codes. The browser localizes known canonical messages from its catalogs without
changing names, SSIDs, protocol values or IDs. Locale selection does not alter API
semantics, authentication, cookies or network configuration.

### Fresh hotspot-only configuration

A private home network is optional. On a fresh appliance, country/hostname and
telescope CRUD can be saved with no home network, then a telescope can be manually
connected on a genuinely free radio while the active management AP stays reserved.
Add home later through its existing partial update endpoint. An empty or malformed
request never clears a configured home or silently reuses its password for a new
SSID. Reservations, owner locks, revision and maintenance checks still apply.

The example native client rejects every HTTP redirect (`redirect: "error"`),
including same-origin redirects, and never retries mutations automatically.
Connectivity errors are redacted; reload actual status before deciding to retry.

## Instrument plugin inventory

Authenticated `GET /api/v1/plugins` returns schema_version1, the installed versioned
family manifests and normal API/config revision metadata. It contains public
protocol/evidence metadata, not Wi-Fi passwords, profile identities or private
connection data. Profiles keep their stable `kind` IDs. Capabilities, validation
scope and curated adapter IDs are separate from connection state and policy opt-in.
Optional `telemetry` lists approved adapters/defaults; `maker_key` describes supported
key family/algorithm/sources and contains no key. Absent telemetry means empty
adapter/default lists. These descriptors never activate observation or download a
key; the example client's `plugins()` method returns them unchanged.
See [declarative plugins](PLUGINS.md) and the generated OpenAPI PluginCatalog schema.


## Isolated ZWO and SynScan mount endpoints (development candidate)

Opt-in `isolated:true` profiles now provide per-radio network namespaces and saved
`client_port` endpoints for ZWO/LX200 and SynScan mounts with overlapping target
addresses. Existing routed profiles retain their behavior. This is a mount-only
candidate; the simultaneous Vespera design remains future work. See
Isolated mount endpoints for the API, prerequisites,
actual Linux fixture coverage, and pending physical acceptance procedure.

## Persistent Wi-Fi adapter roles

See Wi-Fi adapter roles for the revision-checked role map, current-use fields and restart behavior.

## Native client contract v1.0 (frozen 2026-11-04)

[The native-client specification](NATIVE-CLIENT.md) defines discovery, pairing,
the instrument catalog, explicit client connection/reservations, long-poll events
and telemetry catch-up. It reuses this API's operation manager, error envelope,
revision preconditions, protection engine and event cursors. Pairing, identity
and the instrument facade are implemented. The linked scenarios distinguish real
HTTP execution from planned replay; neither constitutes hardware acceptance.

OpenAPI operations tagged `native-client` carry `x-contract-status`. Existing
public info, administrator token login, operation polling and short event polling
continue to work. Telemetry catch-up, session queries and exports are implemented.
Telemetry/camera event producers remain separate adapter work. Capabilities are
advertised only when their runtime is available and owner-enabled. The expanded
`contracts/native-client/v1/openapi-snapshot.json` freezes the completed telemetry
items and additive routes; no empty-array exception remains. This freeze is independent of hardware acceptance.


### Isolated UDP endpoint owner

Isolated UDP live `client_endpoint` values and their alternatives add `owner`
(`null` or `{address:"IPv4:port",since_seq:integer}`), `refused_clients` and
`owner_idle_seconds` (default 20). One source owns all home/hotspot paths for the
profile. Only its incoming datagrams extend ownership; another source is dropped
and counted while the owner is active. At the idle boundary the next source can
claim it. The refusal count counts datagrams, not distinct client identities;
sequence and count saturate at `2**53-1`. The sequence is endpoint-local and is not
an event cursor. Neither ownership nor counters are persisted or logged.

`POST /api/v1/endpoints/{id}/release-owner` takes `{}` and an actual quoted
`If-Match` revision, requires administrator scope, and returns `200` with `ok`,
`id` (profile ID), `owner:null`, retained `refused_clients`, `owner_idle_seconds`,
`api_version` and `config_revision`. Current password-issued native tokens have
administrator scope; tokens without it receive `403 INSUFFICIENT_SCOPE`.
Missing/wildcard preconditions return `428 PRECONDITION_REQUIRED`; stale ones
return `412 PRECONDITION_FAILED`. Unknown/non-isolated IDs return
`404 ENDPOINT_NOT_FOUND`; TCP endpoints return `409 OWNER_NOT_SUPPORTED`.
Repeated release succeeds without sending any manufacturer command. A previous
owner still sending can reclaim it immediately; stop its traffic before handover.
Pending replies from an old owner generation are discarded. TCP relay semantics
are unchanged. See deterministic behavior and validation boundaries.
Endpoint inventory GET routes remain reserved.

## Native instrument facade

The implemented `/api/v1/instruments` facade follows blocks C and D of
[NATIVE-CLIENT.md](NATIVE-CLIENT.md). DNS-SD and `/info` now advertise
`pairing,instruments,connect,events-wait`. Read scope permits catalog, one-instrument and
endpoint reads; connect scope permits connect, lease renewal, release and explicit
disconnect. Administrator sessions retain the usual scope override.

Profiles accept `client_connect` (boolean, default true on migration and creation)
through existing revision-checked profile edits. This is owner consent for paired
apps to allocate a free radio independently of global `auto_connect` and
`auto_eligible`. The browser exposes the setting in Advanced connection settings
with an information dialog. Turning it off does not disconnect an existing link.

`POST /instruments/{id}/connect` requires an actual quoted current `If-Match` even
for a ready connection. Optional fields are `lease_seconds` (60–3600, default 900),
`allow_replace` (at most 24 profile IDs) and `wait_seconds` (0–25). The existing
operation scheduler performs all allocation and restoration. Replacing an existing
instrument additionally requires its `auto_replaceable` and its explicit ID in
`allow_replace`; existing quiet/forced policy, cooldown, management reservation,
owner protection and foreign-client lease gates remain enforced. Native connect
does not require a victim's legacy `auto_eligible`. A lack of capacity returns 409
`NO_FREE_RADIO` with redacted candidate IDs/names. Candidate listing is not a promise
that a later fresh safety check will permit replacement.

Since 1.2.0 an explicit native connect also takes the radio of an enabled profile
that is only being retried (`unreachable`) or that its app left connected after
releasing its reservation (`standby`), without `auto_replaceable`. The 409 adds
`holders` (who keeps the radios and what each is doing), and the optional
`force_replace` (at most 24 profile IDs) replaces a named holder after the user's
confirmation; pauses, owner locks and foreign-client leases still refuse. The answer
and the operation name the displaced profile (`displaced`), whose catalog entry
carries `released`. `disconnect` accepts `reason: "switched_off"`. Lost links are
retried with a growing wait (15 s doubling up to 300 s), reported as `retry`.
Details: [Radio release](NATIVE-CLIENT.md#radio-release-additive-proposed-for-contract-13).

A ready 200 carries the endpoint and lease; accepted 202 carries the existing
operation ID/status URL and lease. At most four connect requests wait concurrently;
extra waiters receive 202 immediately. Wait timeout does not cancel the operation.
A failed operation still has an inspectable terminal record; release its lease or
let it expire. Native-issued operation responses omit physical selectors and use
the native endpoint shape, while existing legacy-issued operation shapes remain.
The same five-operation/minute admission budget applies, keyed by paired client ID
across token refreshes (`CONNECT_LIMITED`); already-ready lookups and pending
operation deduplication retain existing ensure accounting semantics.

Lease ownership uses paired `client_id`, with an opaque session identity fallback
for administrator tokens. The shared RAM-only protection engine retains its
32-total/eight-per-client limits. Renewal checks and increments the record version
atomically under its existing lock. Legacy administrator TTL and explicit-version
rules are unchanged. Release leaves connection intent intact; explicit disconnect
removes only the caller's protection atomically with the controller request and
restores it if the request fails. Owner locks and foreign leases always win.
`lease_id` may be omitted on disconnect; if supplied it must identify this client's
current lease. Normal reconciliation restores lost links while desired connection
intent and a lease remain. Service restart loses leases; owner locks persist.
The internal `ConnectionProtection.lease_hook(type, instrument_id, lease_id,
expires_in)` reports the 60-second warning once per renewal and lease end. The event journal
owns publication of these events.

Catalog/endpoint GETs never scan or probe. Independent `service` comes directly
from the live service probe, even with a connected link or a ready relay. Only recent non-disruptive
spare-radio observations can yield `available`; inactive `enabled:false` profiles
otherwise remain saved and connectable. There are no camera entries or telemetry
adapters in this section. Native endpoints reuse the endpoint inventory and prefer the accepted
socket's home/hotspot path, with other-path alternatives and the original
`requires_client_route` prerequisite. They contain no Wi-Fi credentials, SSIDs,
BSSIDs or MACs. Association timing and physical restoration remain documented,
not verified on hardware.


## Session telemetry

Owner-enabled observations remain local, read-only and normalized. The bridge
never stores raw manufacturer messages. All GET routes below require `telemetry`
(or `admin`); PATCH/DELETE require `admin` and the actual quoted `If-Match`
configuration revision. Missing/wildcard revisions return 428; stale revisions
return 412. Browser cookie mirrors use `/api/telemetry` with the same paths;
writes additionally require same-origin and `X-CSRF-Token`.

| Method | Native path | Result |
| --- | --- | --- |
| GET | `/api/v1/telemetry` | Enabled/persistent state, retention, logical used bytes, session count, clock status, boot ID, latest real sequence, effective profile adapters and config revision |
| GET | `/api/v1/telemetry/sessions?profile_id=&after=&limit=` | Sessions ordered by start sequence, next cursor, has_more; limit 1–100 |
| GET | `/api/v1/telemetry/sessions/{id}` | Summary, start/end, frame counters, rejection reasons, pointing failures, gaps and paired readers |
| GET | `/api/v1/telemetry/samples?session_id=&after=&boot_id=&limit=` | Combined samples/events page; limit 1–500; carries `boot_id`, the boot domain of `next_cursor` (contract 1.2) |
| GET | `/api/v1/telemetry/sessions/{id}/export?format=csv\|json` | Streamed attachment |
| PATCH | `/api/v1/telemetry/settings` | Retention and explicit profile adapter selection |
| DELETE | `/api/v1/telemetry/sessions/{id}` | Remove one session and its samples/events/readers |
| DELETE | `/api/v1/telemetry/sessions` | Remove all sessions |

Settings accept only `retention_nights` (1–365), `max_bytes` (4–512 MiB expressed
in bytes), and `profiles:{id:{adapters:[...]}}` with optional `alpaca_interval_seconds`. The profile map merges by ID; omitted profiles and fields retain their explicit choices.
GET `/telemetry` returns the effective `profiles:{id:{adapters:[...]}}` map so
clients can read and edit those choices. Every adapter must belong
to that saved profile's plugin. Empty adapter selections explicitly disable observation for
that profile. Without an explicit selection, `link_v1` is enabled for each profile
plus its plugin defaults (`vespera_status_v1` for Vespera). These defaults record
the owner-approved observation policy. The shared `link_v1` collector runs for
connected, enabled profiles; other declared producers are separate.
Settings are private telemetry metadata and participate in the configuration revision.
Enabling an adapter does not itself start a session or send an instrument command.

`link_v1` samples existing traffic sessions every 30 seconds and when normalized
link state changes. Confirmed forwarded/relayed byte increments open or extend a
session; Wi-Fi association, successful echo replies and repeated passive samples
do not. Metrics are `link.rssi_dbm`, `link.bitrate_mbps`, `link.ping_ms` (median),
`link.loss_pct` (0–100), `link.bytes_to` / `link.bytes_from` (directional deltas
since the preceding emitted sample), `link.disconnects` (confirmed disconnects
since process start), `link.protected` (boolean owner lock or live reservation),
and `link.activity` (the existing bounded traffic classification). Missing or
stale source measurements are omitted, never invented as zero. Byte counter
resets and association changes establish a new baseline. No profile identifiers,
network addresses, SSIDs, raw payloads or lease owners appear inside metric values.

Radio metadata reuses current association reads. Isolated mounts reuse their
existing relay ping windows; routed profiles use one ICMP echo at most every ten
seconds, with a one-second deadline and a ten-minute median/loss window. This
sends no manufacturer protocol commands. Set the service environment variable
`ASTRO_BRIDGE_LINK_PING=0` to disable routed echoes; default is enabled. Disabling
`link_v1` for a profile disables its routed echo too. A missing/forbidden ICMP
socket yields unknown measurements, not a false loss percentage. The collector
uses one shared thread and no subprocess per profile or tick. Demo mode never
opens an ICMP socket. Physical timing and Pi load remain **documented, not verified
on hardware**.

The shared session lifecycle emits the already-declared native events
`telemetry.session_started`, `telemetry.session_ended` and `telemetry.available`.
Availability carries the latest real sample sequence, coalesces a sample batch
for one second and repeats at most once per 30 seconds per session; session end
first publishes any pending final sequence. Profile catalog capabilities contain
only implemented, currently available adapters in the owner's effective selection
(`link_v1`, `vespera_status_v1` and opt-in `alpaca_observer_v1`); declared future adapters remain absent. The global
telemetry capability describes the available history/API service, including when
all profiles explicitly opt out of further observation.

A sample is `{seq,session_id,boot_id,mono_ms,utc,utc_corrected,metric,value}`.
Values are normalized numbers, booleans or bounded text. Lifecycle events use
`{seq,session_id,boot_id,type,reason}` with `session.started` or `session.ended`.
Sequence ordering is global across sessions and events; another instrument's
records are not gaps. `latest_seq` excludes unused crash reservations. Reads
merge committed history with the bounded pending batch without forcing SD writes.

The cursor envelope is `{samples,events,next_cursor,has_more,gap,reset_reason}`.
`BOOT_CHANGED`, `CURSOR_AHEAD`, `RETENTION_GAP` and `HISTORY_UNAVAILABLE` follow
event replay semantics: a reset or omitted cursor returns the most recent page,
the current global cursor and `has_more:false`. Historical recovery then uses
session inventory/export; do not treat the reset page as new live observations.
A normal catch-up advances across both sample and event sequences. On its last
page the cursor advances to the current global sequence even when this session
has no new samples. Loss elsewhere in the shared journal conservatively signals
a reset too. Retention/deletion/crash holes are never silently treated as intact.

```js
const state = await bridge.telemetry();
let cursor = savedCursor;
for (;;) {
  const page = await bridge.samples({ sessionId, after: cursor, bootId: savedBootId, limit: 500 });
  if (page.gap) {
    const sessions = await bridge.sessions({ limit: 100 });
    // Refresh local session state; reconcile older history from an export.
    break;
  }
  consumeInSequenceOrder([...page.samples, ...page.events]);
  cursor = page.next_cursor; // Save only after successful consumption.
  if (!page.has_more) break;
}
```

Summary counters are maxima of normalized cumulative frame metrics; rejection
reasons have at most 128 distinct names per session. `pointing_failures` counts
normalized `error.code=pointing_failed` samples, never inferred raw errors. Gaps
report up to 100 missing global ranges during the session. Readers identify only
paired app names and fetched sample ranges (at most 16 names/session), never IPs;
cookie/password-admin reads are not paired readers. Audit updates coalesce in a
bounded batch and may lose the final 30 seconds on power loss.

CSV is UTF-8 with CRLF rows and exact columns
`seq,utc,utc_corrected,metric,value`; absent corrected UTC is empty. JSON is
`{session,metrics_dictionary,samples,events}`. Both stream pages of at most 128
records; no full-history array is materialized. Filenames are `kind-date.csv`
or `.json`, with no network identifiers. Exports use a fixed upper sequence and
release the store lock between chunks; concurrent retention/deletion can remove
records before a later chunk. Download before deleting a session when preserving
its complete retained history matters. The example client's `exportSession()`
returns a Response for incremental consumption of `body`.

These are synthetic/storage/API guarantees, **documented, not verified on
hardware**. They do not establish manufacturer observation or phone acceptance.

## Attached cameras and Alpaca

`GET /api/v1/cameras` requires **camera** (or admin) scope and returns an array of
at most eight attached-camera summaries. Each summary contains `id`, `name`,
`driver`, `state`, `alpaca`, `last_image:{bytes,seconds}`, and optional
`owner:{client_id,address,since,generation}`. `since` is bridge monotonic seconds, not UTC.
`alpaca` is `{port,device_number}` only when the runtime has successfully bound a
management listener on the current request ingress; otherwise it is null. No raw image or device reply is in
this inventory. A completed frame reports byte size and exposure duration after
it is first retrieved. An empty list means no registered camera; simulation and
physical discovery are never silently substituted for missing hardware.

Attached-camera discovery adds optional `instrument_id`, `usb` and `discovery` fields. `usb` contains
only vendor/product IDs, device/interface classes, speed in Mbit/s and numeric
bus/port path. `discovery:{state,power_unstable,disconnects}` describes attachment
lifecycle separately from driver `state`: `detected` → `preparing` → `ready`,
`driver_missing` or `unsupported`. The UVC driver is installed; missing/invalid
owner pixel-pitch calibration or unsupported capture properties produce
`unsupported`, with no Alpaca endpoint. `driver_missing` is reserved for an
unavailable driver factory. Both unserved states have `state:error`, `alpaca:null`
and neutral `last_image`. See UVC owner calibration and limits.
Three disconnects within ten minutes flag unstable
attachment; this is not a measured voltage or confirmed power-supply diagnosis.
Discovery uses five-second sysfs reconciliation and optional bounded netlink
hints. See USB deployment and limits.

The native `/instruments` catalog includes cameras with `kind:camera`, `link:connected`
when discovery is ready and service projected from current driver state. It
preserves the frozen maximum of 24 rows per page: saved profiles retain their
ordering, followed by cameras sorted by opaque ID. Responses add
`next_cursor:null|string` and `has_more:boolean`; pass `after=next_cursor` for the
remaining rows. At most 32 instruments are available. Invalid/duplicate/unknown
query fields or malformed cursors return 400 `INVALID_CURSOR`. Membership/order
changes or bridge restart return 409 `CATALOG_CHANGED`; discard collected pages
and restart without a cursor. State-only changes preserve the cursor.
`instruments()` remains a single-page helper; `allInstruments()` collects up to
two pages with at most three attempts. A conflicting saved profile keeps its ID;
the camera receives a deterministic `cam-...` alias in `instrument_id`. A changed
alias invalidates its prior bridge reservation. No serial number is probed to
resolve identity. Attachment IDs/aliases are not physical identity across port
moves or replacement by an identical model at the same port.

Generic native camera connect requires the same `connect` scope and actual
quoted revision as telescope connect, returns 200 only with a bound Alpaca
endpoint on the accepted request ingress, and never switches Wi-Fi or opens a
camera. The endpoint is `mode:relay`, `requires_client_route:false`, with
`service:alpaca_camera` and `device_number:0..7` on its TCP port entry (default
11111). Its required `target_ip` is the serving management IPv4 host; telescope
`target_ip` retains its existing instrument-address meaning. An unbound/missing
driver/disabled or different-ingress service returns 409 `NOT_CONNECTED` without
creating a reservation.

For cameras, native connect/renew/release/disconnect reserve or release the
paired client's **bridge endpoint reservation only** (60–3600 seconds, shared
bounded lease capacity). They never claim or transfer Alpaca ClientID/source
ownership, close a device, abort an exposure, or promise exposure exclusivity.
Alpaca retains its independent owner guard. A foreign holder cannot renew/release
another reservation. Native disconnect releases only the caller's reservation;
the attached camera and any Alpaca exposure remain available. Removal invalidates
its endpoint and releases native reservations, producing `lease.ended` with the
existing `RELEASED` reason. Requests for a removed instrument return 404 until it
is detected again. Native `camera.detected`, `camera.removed`,
`camera.power_unstable` and legacy `camera_detected`, `camera_removed`,
`camera_power_unstable` enter the existing event journal. Native camera events
contain only the instrument ID/common envelope, never USB identifiers or payloads.

Administrator `GET /api/v1/cameras/settings` returns
`{settings:{serve,discover,port,allowed_subnets},config_revision}`. Administrator
`PATCH /api/v1/cameras/settings` accepts a nonempty partial settings object and
requires the actual current quoted revision in `If-Match`; missing/wildcard
preconditions return 428 and stale revisions 412. `serve` is boolean (default
true when a camera is present), `discover` is boolean (default true, including legacy
settings files) and gates actual UDP 32227 discovery replies without removing the
TCP endpoint, `port` is 1024–65535 (default 11111), and
`allowed_subnets` is at most 16 distinct canonical IPv4 CIDRs (default empty).
The empty list permits peers on confirmed management subnets only. Additional
CIDRs narrow that access and cannot grant access from instrument interfaces.
The owner decision and port/subnets persist privately. These routes reject
unknown query/body fields. Camera settings participate in the common
`config_revision`; an edit invalidates stale settings revisions.

Alpaca camera control uses its own TCP port, with no Bearer token, exclusively on
confirmed home/hotspot management interfaces and under the owner serving switch.
The bridge API never interprets `/api/v1/camera/{n}/{member}` as its own route.
See [the Camera V3 member table, ImageBytes framing and discovery boundary](CAMERA-ALPACA.md).
`AstroBridgeClient.cameras()`, `.cameraSettings()` and
`.editCameraSettings(patch, revision)` call the generic bridge resources.

Camera administration adds optional inventory `sensor:{width,height,bits,max_bin}`,
`defaults:{bin,gain}`, `current:{bin,exposure_seconds}` and
`refused:{client_id,address,since}` (last foreign-owner refusal, at most 120 seconds).
The owner `generation` is an opaque 32-hex consent version. Optional `dhcp_name`
comes only from an actual current hotspot DHCP record matching the owner address;
Alpaca ClientID is never interpreted as a paired-app identity. No request payload
or image enters these summaries. Initial bin2 applies only to images larger than
3 MiB on confirmed 2.4 GHz-only radios, and only when the driver supports it.
Explicit subsequent Alpaca settings are retained; bin1-only UVC is never changed.

Administrator `POST /api/v1/cameras/{id}/release-owner` requires actual quoted
`If-Match` and `{confirm_release:true,owner_generation}`. It closes the camera and
can interrupt an exposure. The service worker compares the exact captured owner
generation atomically before closing: changed/expired owners, even a new exposure
by the same ClientID/address after release or replacement attachment, return 409
`CAMERA_OWNER_CHANGED`; absent cameras return 404. No automatic busy takeover is
performed. Native bridge endpoint reservations retain their independent,
non-aborting release/disconnect behavior. `.releaseCameraOwner(id,generation,revision)`
uses this administrator route.

Administrator `POST /api/v1/cameras/demo` with `{plugged:boolean}` and actual
`If-Match` explicitly plugs/unplugs only `SimulatedCamera` in demo mode; real mode
returns 404. `.plugDemoCamera(plugged,revision)` calls it. Demo startup has no camera
or listener. After explicit plug, Alpaca binds loopback only on the configured
port; a failed bind has no endpoint. Simulated LAN/hotspot addresses are never
published as camera endpoints, and host USB/sysfs is never inspected. Browser
aliases under `/api/cameras` retain administrator cookie/CSRF/Origin guards.

Driver waits and shutdown are bounded. A stalled driver returns service
unavailable without spawning a replacement worker; canceled queued changes are
not executed. Settings/configuration-revision reads stay independent of the
camera driver queue. Bounded USB discovery is also provided. Served cameras
require the Pi 3/4 tier; Pi Zero W remains for connections and telemetry. USB,
real Alpaca applications, LAN/hotspot discovery and sustained exposure behavior
are **documented, not verified on hardware**. Nested-package app/OTA delivery
is a separate delivery step and is not covered here.

### Vespera observation availability

`vespera_status_v1` is now implemented as owner-enabled, read-only observation
of a connected Vespera profile. Native instrument capability lists intersect the
owner's selected adapters with implemented runtime availability. Demo mode opens
no Vespera observer sockets and does not claim this adapter is available. A
runtime yield temporarily removes it from available capabilities. After more
than three established peer closures within two minutes, the observer yields for
ten minutes; refusals and malformed packets do not count as peer closures.

The native events page adds `telemetry.adapter_yielded` with `instrument_id`,
`adapter_id:"vespera_status_v1"` and `retry_after:600`. It is journaled even without
an active telemetry session. This event reports an observation pause; it does not
disconnect the instrument, take/release control or act on the maker application.
Actual Vespera payloads and maker-app coexistence remain **documented, not verified
on hardware**; see the synthetic mapping and limits in [Architecture](ARCHITECTURE.md).

### Alpaca instrument observations

Owners opt in per saved profile through the existing revision-guarded settings
route. `alpaca_observer_v1` is available to generic Alpaca instruments as well as
bundled telescope/mount plugins, except DWARF, which remains link-only. It is
disabled by default. This observer reads an instrument already connected to the
bridge; it is separate from the attached-camera Alpaca server.

```json
{"profiles":{"primary":{"adapters":["link_v1","alpaca_observer_v1"],"alpaca_interval_seconds":15}}}
```

`alpaca_interval_seconds` is an optional integer from 10 through 120, default 15
for old settings without the field. GET `/telemetry` returns it when explicitly
configured. Adapter-only, retention-only and other-profile PATCH requests preserve
an existing cadence. Empty adapters remain an explicit observation opt-out.
Selection changes participate in the existing configuration revision; no new scope
is added. `updateTelemetrySettings(settings, revision)` in the example client
supports these generic owner choices. The native catch-up scenario includes a
successful selection/cadence round trip; existing clients can continue submitting
and reading adapter-only profiles unchanged.

Discovery is limited to the profile's instrument subnet and bound interface or
isolated namespace. It broadcasts on UDP 32227, accepts only responses from the saved profile target, validates source/port,
then GETs `/management/v1/configureddevices`. Other Alpaca hosts on the same network are ignored, and a changed saved target
retires the old observer. No client-entered endpoint is added. The deterministic
first telescope and camera on that instrument are polled, at the configured cadence, using only:

| Type | GET members | Normalized metrics |
| --- | --- | --- |
| Telescope | `rightascension`, `declination`, `tracking`, `slewing`, `atpark` | `target.ra_hours` (0–24), `target.dec_deg` (−90–90), boolean `mount.tracking`, `mount.slewing`, `mount.at_park` |
| Camera | `camerastate`, `ccdtemperature` | `camera.state` (`idle`, `waiting`, `exposing`, `reading`, `downloading`, `error`), `device.temperature_c` |

Every GET includes a stable bridge-derived `ClientID` and shared increasing
32-bit `ClientTransactionID`; mismatched response transactions are discarded.
No PUT, action, command, pulse guide, setter or connection-control member exists
in the observer transport. Each nonzero Alpaca error makes only that member
unavailable for ten minutes; other valid members continue. No error text,
inventory name/UUID or raw payload enters telemetry or logs. Read failures omit
values rather than inventing measurements. Tracking/slewing or an active camera
state opens/extends the shared session. Idle/error-only polls can sample an
existing session but cannot open it or extend its inactivity deadline.

Native instrument telemetry capabilities include the observer only when selected
and implemented outside demo mode. Demo and DWARF never open observer sockets.
The current selection chooses one device per type rather than merging several
instruments into indistinguishable metric values. Real broadcast delivery,
firmware support and session load impact remain **documented, not verified on
hardware**. Protocol reference: [ASCOM Alpaca API](https://ascom-standards.org/ASCOM%20Alpaca%20API%20Reference.pdf).

Isolated profiles use dedicated marked sockets and expiring permission sets,
with the same saved-target constraint. Discovered target/HTTP-port pairs expire
after 180 seconds and refresh per poll; broadcast/reply tuples expire after five
seconds and are cleared after discovery. Replies match the instrument source,
local destination and exact bound UDP socket, including ct-new replies. Native
unmarked services, DHCP, ICMP and default-drop forwarding retain their boundaries.
Opt-out, target/namespace changes and shutdown close owned sockets and revoke
sets; failed revocation leaves only bounded kernel expiry. No all-port/subnet
opening, default route or root fallback is added. Real nft/kernel and broadcast
behavior remain **documented, not verified on hardware**.


### Telemetry-informed replacement protection

`auto_policy.protect_running_operations` is an optional boolean, default `false`
for both new and existing configurations. PATCH `/settings` requires the actual
`If-Match` revision when changing it; enabling requires transient
`acknowledge_interruption:true`, like the other advanced flags. Omission preserves
its value. Explicit manual connection/disconnection keeps its existing guards.

When enabled, fresh `operation.state=running` prevents replacement even when
`force_first_profile` is enabled. A deferred connection event and the failed
connection operation use `OPERATION_RUNNING`. An enabled, supported operation
source with missing/stale state remains unknown and also prevents replacement,
including forced replacement: ageing never releases protection. Fresh non-running
telemetry supplies no quiet evidence and never overrides traffic, locks, leases,
manual intent or the normal replacement requirements. Disabled/unsupported
operation sources leave the existing traffic/force policy unchanged.

Sources: Vespera status (`vespera_status_v1`), Seestar events (`seestar_events_v1`,
only with an owner-provided maker key) and, since contract 1.2, DWARF capture
notifications. The bridge never opens its own DWARF session or sends a command:
`DwarfCapture.receive()` accepts a server-to-client payload that already passed
the bridge (or a bench capture), keeps only the operation state, and forgets it
when the link drops. DWARF state is read from notification 15208 (capture
state, module 9), the one-shot 16405 device state at connection (module 14) and
15211 (GoTo, kind `pointing`); 15209 stacking progress is not exposed yet. These
numbers are partially verified on a DWARF mini bench (02–03/11/2026); a JSON-by-JSON
record is still to come. An unrecognized packet is ignored, so DWARF then stays an
unsupported source rather than a quiet one.
Native instruments expose the same evidence as `current_operation`
(`{state, kind, since}` or null).

Only the Vespera observer currently reports normalized operation states. Its
latest source receipt is fresh for at most three source cadences: 1.5 seconds
while running, 20 seconds otherwise (inclusive boundary). The separate 30-second
storage sample cadence cannot refresh or expire that evidence. Sparse/malformed
operation state is unknown. Reads inspect bounded cached normalized state and
never wait for observer network work. Alpaca camera/mount metrics do not invent
an operation state. Bridge-owned observer traffic uses OUTPUT/INPUT, outside the
FORWARD classifier, so observation alone cannot keep user traffic active.
Source cadences and firmware behavior remain documented, not verified on hardware.

## Owner-supplied maker keys

The bridge ships no manufacturer key. A family has at most one owner-supplied
key, shared by every saved profile of that family. Currently only `seestar` is
supported. This service authenticates **only a fresh read-only connection**;
the verification runs a handshake and closes it, without requesting any instrument state.
The persistent listener and its explicit per-profile observation selection belong
to the Seestar session events service below. Importing a key does not enable any observation adapter.

All routes below require **admin** scope. Browser mirrors replace `/api/v1` with
`/api`; mutations retain same-origin and CSRF validation. GET responses have
`Cache-Control: no-store`. No key-read, fingerprint, modulus, size or export route
exists. The public instrument catalog exposes only `capabilities.maker_key`, one
of `absent`, `unverified`, `verified`, `refused`, `storage_unavailable`, or `null`
when the family has no key capability. `/info` and DNS-SD advertise `maker-keys`
when this service is available, even if platform encryption is unavailable.

| Method and resource | Behavior |
| --- | --- |
| `GET /api/v1/maker-keys` | `{keys:[{family,state,source,updated_seq,unlocks}],config_revision}`. Source is `maker_package`, `import` or null. `unlocks` names stacking, exposures, goto, temperature and battery groups; these describe the future authenticated listener, not active telemetry. |
| `POST /api/v1/maker-keys/{family}/fetch` | Empty JSON object; returns 202 with `{operation,config_revision}`. The fixed maker-package fetch is explicit and never scheduled. 409 `NO_INTERNET` without a cached home uplink (or in demo), 409 `FETCH_RUNNING`, 429 `FETCH_LIMITED` after three attempts per rolling hour in this process. A home uplink is not proof of successful WAN access; network failures become operation verdicts. |
| `GET /api/v1/maker-keys/operations/{id}` | Admin-only latest fetch job, with state `running`, `succeeded` or `failed`, a sanitized verdict and bounded steps. Use the returned `status_url`; this is distinct from instrument connection operations. Jobs are volatile and replaced by the next accepted fetch. |
| `PUT /api/v1/maker-keys/{family}` | `{"pem":"<owner-supplied RSA PEM>"}`; at most 8192 ASCII bytes. Accepts unencrypted PKCS#1 or PKCS#8 RSA, modulus 1024–4096 bits. All unusable content returns 422 `KEY_UNUSABLE` with the same generic message. No payload is echoed. |
| `DELETE /api/v1/maker-keys/{family}` | Requires the actual quoted current `If-Match` revision; no wildcard. Returns current metadata. Cancels old work and removes the shared ciphertext, so late fetches/verdicts cannot restore it. Missing precondition returns 428; stale revision returns 412. |

Import returns metadata immediately after encrypted persistence. If a saved,
enabled profile of that family is currently connected, a bounded background
worker uses its exact source interface/address or isolated socket permission to
open TCP 4700. It sends only `get_verify_str` with `params: "verify"`, signs the
challenge's unmodified UTF-8 bytes using blinded RSA CRT / PKCS#1 v1.5 SHA-1, sends
`verify_client` with `sign` and unchanged `data`, then `pi_is_verified` with
`params: "verify"`. Both verification responses must have integer code zero.
There is no control or state request. The connection closes within eight seconds.
An explicit refusal deletes ciphertext and sets `refused`; unreachable/malformed
responses remain `unverified`. Profile disable, movement, source changes, key
replacement/removal and shutdown invalidate late results. At most four workers
can be retiring concurrently; an import that cannot start immediate confirmation
remains unverified until the Seestar session events service confirms it on its first connection. Demo imports
never open instrument connections. Re-read metadata while the editor is open.

HTTPS fetches use system certificate and hostname verification, no HTTP proxy,
and the fixed maker download URL in `maker_keys/fetch.py`. Each of the five range
reads resolves the redirect chain again with `Accept-Encoding: identity`, HTTPS
only, five redirects maximum, the two approved maker hosts and at most one storage
hostname introduced by a redirect from `api.zwoastro.com`. Any 200 response is
closed at its headers; only an exact 206 Content-Range is accepted. Each request
has a 30-second deadline. The ZIP reader bounds the central directory/compressed
member and inflated member to 16 MiB, refuses ZIP64/encryption/unsupported methods,
checks local/central metadata, size and CRC-32, and accepts exactly one distinct
usable RSA modulus from the selected library member. No package/member/key/signed
URL is written to disk or logged. The in-memory journal allows only step names,
counts, HTTP status, CRC result, candidate count and verdict. Bytearray buffers
are overwritten on exit; Python/SSL/zlib may retain temporary immutable copies,
so this is best-effort memory clearing, not guaranteed secure erasure.

Storage follows the injected platform state directory, under `secrets/` mode
0700. A random 32-byte KEK is atomically written to `maker-keys.kek`; AES-256-GCM
ciphertext plus public metadata is atomically written to `maker-keys.bin`, both
mode 0600. Each encryption uses a new random 12-byte nonce and AAD
`astro-bridge-maker-key-v1|` plus the family. The implementation uses system
libcrypto EVP via ctypes and verifies GCM availability with a self-test. After
`find_library`, Linux may use only the platform-declared `libcrypto.so.3` soname
through the system loader; no vendor paths or bundled libraries are searched. Missing
libcrypto/GCM, unsafe files or authentication failure produce
`storage_unavailable`; writes return 503 `STORAGE_UNAVAILABLE`, with no plaintext
fallback. Revisions use only a monotonic metadata sequence, never secret-derived
values. A whole-memory-card copy contains both KEK and ciphertext and can recover
the key. Removal is not forensic erasure of old filesystem blocks.

These files and their atomic temporary names are forbidden artifact paths.
Configuration exports and migration/OTA configuration backups serialize only
configuration; they never recursively copy state. Current diagnostic resources
expose normalized public records and provide no state-directory bundle. The
artifact guard, OTA packager/unpacker and public-export audit reject secret-file
paths and private-key armour; filename constants in exclusion source are allowed.
Application packaging still has the existing subpackage delivery limitation:
this must be resolved before any OTA or public delivery. The publication home of the
maker-host/member fetch module is also undecided.
No real maker chain or Seestar verdict is verified here; both remain hardware checks.

## Seestar session events

`seestar_events_v1` is implemented, per-profile and owner-controlled through the
existing `PATCH /telemetry/settings` adapter selection. A successful owner key
import/fetch adds it only to Seestar profiles without an explicit selection.
Existing adapter lists (including empty opt-outs) and Alpaca cadence are preserved;
new unconfigured Seestar profiles receive the same conditional default while a
key is present. Removing/refusing the shared key stops listening, without erasing
selections. The Session data switch shows the persisted selection and may disable
it at any time; all writes require the current revision. No key is fetched by the
observer or on startup.

The adapter opens the saved profile's bound TCP 4700 path, performs only
`get_verify_str`, `verify_client`, `pi_is_verified`, then listens to newline JSON.
The send guard rejects every other method. The reviewed keep-alive allowlist is
empty; its optional method/cadence configuration cannot select an unreviewed
method. No `get_device_state`, `iscope_*`, `set_*`, `start_*`, `stop_*`, plans or
images/port 4800 are requested. The verification-only call still closes its
socket. Listener handoff cancels the authentication deadline and consumes any
already-buffered event bytes once.

Only these synthetic field shapes are accepted; firmware variants are not inferred:

| Event | Accepted normalization |
| --- | --- |
| PiStatus | `temp` → `device.temperature_c`; `battery_capacity` → `battery.percent`; boolean/0/1 `charger_status` → `battery.charging` |
| Stack | `stacked_frame` → `stack.frames_accepted`; `dropped_frame` → `stack.frames_rejected`; numeric 1–255 `frame_errcode` → bounded `stack.reject_reason.<code>` occurrence counters |
| Exposure | `exposure` or `duration` seconds → `exposure.seconds`; reviewed `state` → capture operation |
| AutoGoto | reviewed `state` → pointing operation; explicit failed state plus numeric 1–255 `code` → generic `error.code: pointing_failed` |
| AutoFocus | reviewed `state` → focusing operation |

Stack states map to stacking operations. Reviewed states are idle, pending,
running, paused, completed, failed and stopped. `battery_temp` and `is_overtemp`
are dropped because no corresponding reviewed metric exists. Unknown event
occurrences are counted in one saturating process-local counter; names and raw
fields are never retained. Unknown/sensitive fields never enter SQLite or events.
Each line is bounded to 64 KiB and the receive budget to 2 MiB per rolling minute,
shared across reconnect attempts. Reject-reason counters do not increase for
unchanged dropped-frame counts; these synthetic semantics still need review on hardware.

Reconnect waits are 2/5/10/30 seconds. Four authenticated peer closures within two
minutes yield for ten minutes, emitting `telemetry.adapter_yielded` with
`adapter_id: seestar_events_v1`, `retry_after: 600`. Local cancellation/permission
loss does not count as a device close. A refused handshake sets the maker-key
state to refused, stops observation and emits the additive
`telemetry.adapter_refused` event with that adapter ID. Native clients should
refresh key metadata after this event. Yield/refusal suppress effective telemetry
capability availability without rewriting owner selections.

Operation protection retains separate bounded evidence for Stack, Exposure,
AutoGoto and AutoFocus. Any fresh running operation wins; stale/unknown evidence
for another tracked operation remains unknown after running evidence expires.
An unrelated idle/completed event cannot prove a stack ended. PiStatus never
refreshes operation evidence. The constructor's configurable default receipt
cadence is a **synthetic 20 seconds**, with freshness at most three cadences;
it is not a measured firmware guarantee. Forced replacement stays vetoed under
telemetry-informed replacement protection when the aggregate is running or unknown.

Isolated profiles retain the existing single observer-grant owner: selected
Seestar listening with an available owner key has deterministic priority over
Alpaca. Alpaca is omitted from effective native telemetry capabilities while
suppressed; saved selections are retained. Non-isolated observers can coexist.
The single TCP tuple is renewed every 60 seconds with the existing 180-second
expiry, and revoked on cancellation. No namespace/root fallback is attempted.

Event delivery to a second authenticated client while the maker app runs a plan,
connection lifetime/keep-alive requirements, effect on the maker app, actual field
shapes/units and isolated routing are **documented, not verified on hardware**;
hardware measurement is still required. No instrument control or live manufacturer request is part
of the portable tests.

The authenticated telemetry response also has an optional, additive
`adapter_status` map from profile ID to adapter status rows. Rows always contain
`adapter_id`, `adapter_state`, `retry_after` seconds and `unavailable_reason`
(null, owner_disabled, demo, key_unavailable, disconnected, or isolated_priority).
Seestar worker rows add `event_counts_minute` for exactly PiStatus, Stack,
Exposure, AutoGoto, AutoFocus and aggregate unknown; `handshakes` counters
(verified/refused/unverified); authenticated `connection_closes`;
`connected_seconds`; and `last_connection_seconds`. Counters saturate at 2^53,
minute buckets at 60, profile records at 24. Counters are process-local, reset on
a new worker generation, and are retained for the last stopped generation until
replacement/profile removal. Rates expire even when the worker has stopped.
No event payload or unreviewed event name is exposed. These read-only diagnostics
are suitable for hardware observation; they do not establish hardware behavior.

The synthetic AutoGoto failure condition is an explicit `state: failed` plus an integer result `code` from 1 to 255. Repeated failure messages count once in the generic session `pointing_failures` summary; only an explicit reviewed non-failed AutoGoto state resets that transition. Unrelated events do not reset it. These state/code semantics require confirmation on hardware.

## Session cards

`GET /api/v1/telemetry` (and the cookie-authenticated `/api/telemetry`) adds
optional `card_data`, a map of at most24 configured profiles. Each card has:

- `readings`: at most32 allowlisted normalized metrics; each supplies `value`,
  `adapter_id`, source-receipt `utc`, first-observed unchanged-value `since`, `age_seconds`,
  `cadence_seconds` and `stale`. Stale means age strictly exceeds three cadences;
  this is independent of the30-second unchanged-value journal sampling interval.
- `recent`: the latest session from each of at most three observing nights,
  with its latest recorded frame counters (null when no such reading exists). Nights use the bridge clock's noon-to-noon
  UTC day. The UI labels this as the latest session per night.
- `tonight`: up to eight target lines from recent recorded samples, with
  `frames_accepted` and `frames_rejected`. These are a bounded card preview,
  not a complete session-analysis/export replacement.
- `history_partial`: true when older sessions or samples were omitted. The UI
  explicitly labels clipped previews. Clock status remains independently visible.

The card query reads at most501 combined records per selected session, includes
unflushed rows, and never forces a flush or performs an export. History projections
are cached per profile against selected session IDs, their latest sequence/end
and observing day. All sessions remain accessible through the existing paginated
session API. There is no new mutation or scope.

`adapter_status` also projects the actual Vespera lifecycle. `yielded` means the
observer paused itself, not that the instrument stopped. Vespera source cadence
is1.5 seconds when running and20 otherwise; Seestar uses the existing configurable
20-second operation cadence. PiStatus refreshes only its own metrics. These
cadence assumptions and field units remain unverified on hardware. Demo-only
normalized simulation never makes a socket or changes a manufacturer-key verdict.

Opening a profile never writes telemetry settings. Link quality stays on for
normal defaults; a persisted explicit link opt-out remains off with a visible
Enable action. Per-adapter switches preserve other selections and Alpaca cadence.
Deleted profile preferences are pruned on startup and collector reconciliation;
this never deletes historical sessions. Ten catalogs contain every new source
string; new translations use English placeholders pending translation.

### Retained session analysis

`GET /api/v1/telemetry/sessions/{id}/analysis` (cookie mirror
`/api/telemetry/sessions/{id}/analysis`) requires `telemetry` or `admin`, accepts
no query/body options, and returns the additive typed `TelemetryAnalysis`.
It is a derived read and **does not** record a paired raw-sample fetch.

- `points` covers all retained frame counters in at most 240 monotonic-time
  buckets per metric (480 points total). Each contains `metric`, `from_seq`,
  `to_seq`, `mono_ms`, representative `utc`, and `low`/`high` counter values.
  Min/max marks expose within-bucket variation; these are not per-frame events.
  `from_mono_ms` / `to_mono_ms` cover retained samples; `sample_count` counts them.
- `history_available` and up to 100 `gaps` qualify history. A 20-million SQLite
  VM-instruction budget bounds projection work; `limited:true` returns no chart,
  null sample count and unavailable exposure, never a partial chart labelled full.
  Memory holds at most 480 buckets, the existing pending batch, 100 gaps, 128
  fetched intervals and one streamed exposure row. No projection cache or read
  flush is introduced. SQL aggregation includes committed and pending records.
- `exposure.observed_seconds` sums only positive kept-counter increments with
  an unchanged known nonnegative exposure setting since the preceding counter.
  `observed_frames` counts those increments. Unknown initial frames, setting
  changes, resets, absent settings and the 50,000 relevant-row integration bound
  make `complete:false`. Known journal gaps make the total unavailable (`null`),
  since a missing exposure change cannot be excluded. `latest_seconds` is the
  last retained per-exposure length, not a total. This is observed integration,
  not a claim about physical shutter time or undocumented manufacturer behavior.
- `reader_coverage.fetched` is the exact union of at most 128 recorded raw fetch
  intervals, independent of app names. `unknown_before_seq` is exclusive: newer
  ranges outside that union have no recorded paired fetch; older coverage is
  unknown. `null` means all negative coverage is unknown (legacy/degraded audit).
  Existing min/max named `summary.readers` semantics remain unchanged.

New sessions start exact coverage tracking. Adjacent/overlapping fetch intervals
coalesce; separated holes never do. When 128 intervals are exceeded, oldest
intervals become unknown and the newest 128 remain. At most 128 sessions have
pending audit updates; overflow marks negative audit coverage unknown persistently.
Repeated already-recorded reads do not enqueue redundant audit writes. The audit
shares existing 30-second flushing and can lose the final pending batch on power
loss. Legacy sessions gain positive exact intervals on new reads but never gain
invented historical negative evidence. Session deletion/retention cascades audit
rows; browser views and analysis do not count as paired raw reads.

### Optional INDI camera service

The admin camera settings resource accepts the optional boolean `settings.indi`
(default false). PATCH sends the field `indi` in the existing settings patch,
with the current revision. `serve` remains the master switch. A compatible
attached camera uses one protocol at a time. An existing Alpaca connection or
unreaped ASI worker refuses the handoff with 409 `CAMERA_BUSY`. Disconnect the
camera in the owning app before retrying. Turning INDI off restores Alpaca on
the next confirmed USB scan. TCP 7624 is reserved while INDI is selected.

GET/PATCH `/api/v1/cameras/settings` include `indi: {"port":7624,"state":"off"}`
beside the persistent settings. Camera-scope GET `/api/v1/cameras` includes the
same optional object on selected rows, with `alpaca: null`. States are
`off`, `waiting` (awaiting one confirmed attachment or first health check),
`unavailable` (missing prerequisites, failed health, or inaccessible ingress)
and `ready`. Only `ready` identifies a usable endpoint on this request's
management address. Native endpoint discovery returns its actual host and
`indi_camera` TCP port. The primary DNS-SD service can include `indi=7624` once
all selected management listeners are healthy and camera discovery is enabled. The legacy service has no INDI
hint. See the platform requirements.

## Live USB and wired management

An owned, connected Ethernet interface is a `home` management path, using the
same authentication, discovery, endpoint and relay contracts as private Wi-Fi.
It is never listed as a Wi-Fi radio or offered for scanning, hotspot service or
instrument allocation. One confirmed wired path takes precedence while present;
its loss restores an eligible Wi-Fi management path. Saved Wi-Fi settings remain
unchanged. Ambiguous, disconnected, unowned and unroutable wired paths are refused.
Clients use `info().request_path` and the returned endpoint inventory as usual.
No client should infer a private address from an interface name or CPU architecture.

Live appliances report deployment `live-usb` through existing status capabilities.
Their root-owned OS declaration chooses exactly one of `linux-alpine-x86_64` and
`linux-alpine-aarch64` for v3 application-only OTA. They never fall back to an armhf
archive. `status.management.paths[].kind` is an optional additive field: `wifi` or
`ethernet`. Older bridges can omit it; absence does not identify Ethernet. No
native route or request schema changes in this image profile.

### Station instruments (contract 1.2)

Seestar and DWARF have a manufacturer station mode: the instrument joins the
bridge hotspot or the private LAN instead of exposing its own access point. A
station instrument is declared by address only (`/stations`, or **Station
instruments** in the web interface, where hotspot DHCP clients are offered as
addresses). It has no radio, SSID, password, isolation or owner lock, and never
changes routing. The bridge checks every 15 seconds that `target_ip` completes a
TCP handshake on a read-only port (Seestar 4700, DWARF 8082 so its single control
session is never occupied) and sends nothing on it; demonstration mode simulates
the answer. IDs are unique across radio profiles and stations. Validation accepts
only private, non-loopback IPv4 addresses and at most 16 stations.

Natively, a station is listed with `access: station`, `simultaneous: true`,
`link` `connected` / `unreachable` (`saved` before the first check), and
`service` `ready` / `not_answering`. `connect` creates the ordinary lease (or a
night reservation) and returns `mode: direct` with the instrument's own address
and plugin ports; it answers `409 INSTRUMENT_UNREACHABLE` while the instrument
does not answer. Station leases never set radio protection (`protected()`), so
they never block network or adapter changes. While reserved and answering, the
handshake time is recorded as `link.ping_ms` in a `station_link_v1` telemetry
session; a lost answer adds `link.disconnects`.

### Night reservation owner switch

`auto_policy.night_reservations` (default `true`) lets the owner refuse night
reservations: when `false`, `reserve_seconds` on `connect` or `lease` answers
`403 RESERVATIONS_DISABLED`; ordinary leases are unchanged. It needs no
interruption acknowledgement. An older application that does not know this field
refuses to load the configuration, as for other new policy fields.
