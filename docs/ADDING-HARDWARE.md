# Add support for your astronomy hardware

[README](../README.md#propose-support-for-your-hardware) · [Contributing](../CONTRIBUTING.md) · Test commands

Astro Bridge support means more than recognizing a Wi-Fi name. The bridge must
connect reliably, provide a usable path from the manufacturer's app, protect an
instrument while it is being used, recover after interruption, and explain its
behavior clearly. Each claim belongs to a specific model, firmware, app and
network topology. Hardware in the same brand can require a different protocol.

You can contribute without owning every supported device. Open a hardware-support
issue with the information below, share sanitized results, or submit a **draft
pull request** with the implementation and an honest list of untested steps.
See the repository's [issues](https://github.com/Sqound/astro-bridge-core/issues) and
[pull requests](https://github.com/Sqound/astro-bridge-core/pulls). Repository access may
be required while the prototype is private. Do not claim that untested hardware
is compatible simply because its SSID resembles a supported family.

### Serial (USB) devices

A mount with a serial port (not Wi-Fi) is added as a **serial profile**, not a Wi-Fi
plugin. The profile is data in `wifi_bridge/serial_profile_data.py`: serial settings,
the read-only identification frames and, if the device has one, the stop sequence the
bridge sends when the client is lost (none means nothing is sent). The relay, the
single-client rule, detection and the API are shared; see
[Serial mounts](SERIAL-MOUNTS.md). Bench evidence must say which frames are only
documented and which were seen on hardware, and the stop sequence needs a physical
test of every direction before a profile ships.

## 1. Describe the hardware and the intended connection

Record:

- Exact telescope or mount model, firmware, manufacturer's app version and phone OS.
- Pi model, OS/kernel, bridge version/build, adapter chipset/USB ID and driver.
- Direct telescope Wi-Fi versus station mode, supported bands and authentication.
- The topology to validate: private Wi-Fi bridge, isolated hotspot, or hotspot
  with Internet from private Wi-Fi. Note client isolation and routing constraints.
- Public manufacturer documentation, protocol references and licensing constraints.

Keep credentials, serial numbers, personal SSIDs/MAC addresses, location, tokens,
subscriptions, CA/private keys and raw private configuration out of issues and PRs.
Use fictional network names and addresses in fixtures. Keep personal test logs,
packet traces, screenshots and release artifacts outside committed files.

## 2. Establish a direct-connection reference

Connect the phone directly to the instrument using its normal app. Confirm that
ordinary controls and a camera preview work before debugging the bridge. Record
addressing, discovery behavior and latency. Do not start an acquisition, replace
an observing plan, move a mount or interrupt guiding just to test discovery.
Such actions require the owner's explicit authorization and appropriate setup.

Separate observed facts from hypotheses: an open TCP port proves reachability,
not that the app authenticates, displays an image, or controls the right device.

## 3. Discover and identify a candidate safely

Identify how the instrument is found: Wi-Fi scan, UDP broadcast, multicast/mDNS,
fixed address, service endpoint or app-specific request. Work with the existing
candidate, binding and manufacturer-demand modules rather than adding a second
unbounded scanning loop.

An SSID/MAC suffix is a useful hint, not authenticated identity. Prefer a real
manufacturer identity endpoint where available, and handle several devices of
one family, duplicate names and shared factory addresses. Never invent serials,
station addresses, SSIDs or credentials to make a device appear supported.
If a request cannot identify one instrument, retain ambiguity and ask the user
to select it. Do not silently choose another instrument.

## 4. Implement connection and native-app access

Keep OS commands in the network backend and use the normal revision-checked
connection API. Preserve private-network and hotspot adapter reservations,
leases, owner locks and saved profiles. Each active direct-Wi-Fi instrument needs
an available compatible radio; overlapping subnets are a separate limitation.

Determine which addressing mechanism the app actually accepts:

- Routed access to the real instrument address, with a private-router route when
  needed (for example the tested Vespera private-Wi-Fi path).
- A family-specific relay/proxy advertised at the bridge's management address.
- Manual IP entry and remembered reconnection, when automatic discovery is not
  validated. State this limitation rather than advertising first-use discovery.

On the Astro Bridge hotspot, use the displayed hotspot endpoint and its supplied
routes. In hotspot-with-Internet mode, private Wi-Fi is the Internet uplink and
the phone joins the hotspot. Do not accidentally reconfigure the phone's private
Wi-Fi credentials through synthesized discovery responses. Validate any proposed
station-mode behavior separately; never switch a telescope into station mode
without the owner's agreement.

Start with a [declarative plugin](PLUGINS.md): add or extend a family manifest,
rebuild the bundle/contract, and supply fixtures plus validation evidence. Existing
adapters cover native TCP/UDP relays and approved discovery/identification formats.
Only a genuinely new protocol needs a reviewed adapter implementation. UI presets,
plugin inventory, policy choices and profile types derive from the registry.

## 5. Measure real activity with a controlled phase sequence

This is the sequence used with the physical DWARF mini and Seestar S50 Pro, and
partly with Vespera. Keep the network and active instrument unchanged between
phases. The owner confirms the exact app screen before each observation. Observe
bounded metadata from owned paths; do not inspect image or authentication payloads.

| Phase | Owner action | What to record |
| --- | --- | --- |
| App closed | Leave instrument powered, fully close its app | Counters, existing sessions, connection and relay readiness |
| App home | Open app on the bridge network and stay on home | Discovery, small periodic exchanges, open native channels |
| Preview | Display a real camera image without acquisition | Transfer rate, protocol/port metadata, activity protection |
| Return home | Stop preview but leave app open | Whether bulk traffic stops and which background channels remain |
| Fully close again | Keep instrument powered and close app | Silence, fresh samples and the actual quiet/release threshold |
| Stored content | Browse content actually stored on instrument | Download behavior; phone-cached images do not exercise the bridge |
| Autonomous work | Only with explicit owner authorization | Commands, guiding/observing autonomy and safe interruption limits |

Use comparable windows (we used about 30 seconds per app phase) and retain
individual samples, counter/table/association continuity, direction, packet/byte
deltas, established channels and production classifier output. A classifier
replayed offline is not proof that the installed collector produced every sample
successfully. Do not equate an open silent socket with image viewing.

For the tested S50 Pro, home used about 9–10 kB per 30 seconds, preview about
12 MB, return-home about 9 kB, and app closure no forwarded traffic. These are
reference observations, **not universal calibration thresholds**. Vespera's
initialization failure in daylight did not validate an observing workflow or an
image download. Keep such missing phases explicitly pending.

Background exceptions must be bounded to known native service channels, small
rates and packets. A service port can also carry images: large transfers remain
protected. Mixed/unknown traffic, lost coverage, counter resets, stale samples,
missing identity, reservations and owner locks must never become idle evidence.
Keep the configured quiet/cooldown periods and test just before/after their
boundaries. If the exact app-close time was not recorded, report the device's
observed quiet duration instead of claiming a precisely timed UI-close delay.

Mounts need additional validation of guiding, slewing and command traffic.
A camera-viewing-only policy is insufficient to declare a mount safe to replace.

## 6. Validate switching in both directions

Test app demand from an already connected instrument to the new family and back:

1. Confirm apps/acquisitions and protection state with the owner.
2. Verify the actual app screen that emits demand (home or device search).
3. Confirm demand reception, selected physical profile, adapter allocation and
   reasons for any refusal. A family request may not identify one physical unit.
4. Check that activity, locks, reservations, incompatible bands, ambiguity and
   overlapping networks prevent unsafe replacement. Do not force a test through.
5. After the normal quiet/cooldown periods, check actual association, relay,
   native-app control and visible camera data. Measure observed switch duration;
   do not invent an estimate from a timeout or from another model.
6. Repeat with app reopen, instrument power-off/restart, loss of private Wi-Fi
   and each supported topology. Protect management access throughout.

A manual connection is not proof of automatic switching. Receiving a demand is
not proof the native app subsequently accepted the instrument. If one direction,
first-use discovery or the home screen is untested, say so.

## 7. Integrate the user interface and notifications

Use a clear family/model name, connection state, current endpoint and readable
routing guidance. Put technical details in an information dialog and advanced
settings in collapsible sections. Translate new user-visible text in all ten
catalogs and regenerate `static/i18n.js` with `python3 tools/build-i18n.py`.

Check protection/active-use state on the dashboard. Verify supported notification
categories and internal click destinations. Provider acceptance alone does not
prove notification delivery: test receipt and navigation on the real installed
PWA/phone. Preserve consent and subscriptions; never use global logout as test
cleanup or silently enable new categories.

## 8. Verify software, then deploy to consenting hardware

Run the relevant Python, browser and isolated Linux packet suites from
TESTS.md. Add fixtures for valid/invalid discovery, ambiguity, identity
changes, service/background/content traffic, silent sockets, stale/error states
and protected replacement. Namespace/demo results prove software behavior, not
physical radio or manufacturer-app compatibility.

For physical OTA: confirm no acquisition, take a private snapshot of roles and
settings, use the normal protected maintenance gate and an identified/checksummed
application archive. Verify the **actually running version and build**, completed
transaction, original configuration, radio roles, HTTPS trust, SSH identity and
push consent before resuming tests. Do not bypass a busy/unknown gate or claim
installation merely because a transfer or queue command succeeded. Rejoin the
hotspot if needed and verify native app behavior again.

## 9. Submit a focused pull request

Create a branch from the current repository, keep fixtures sanitized and open a
**draft PR** if hardware validation is incomplete. Include:

- Model/firmware/app/OS/topology matrix and references for protocol decisions.
- What the adapter now does, the actual trigger, endpoint and known limitations.
- Automated checks and separate physical results for every phase above.
- Evidence of activity protection, quiet release, both switch directions,
  reconnection, management preservation and relevant PWA behavior.
- Explicit pending models, topologies, acquisition/night or station-mode tests.

Link an issue if available. Ask maintainers to review before merging; repository
access and review requirements still apply. Never include credentials or private
runtime artifacts. The PR should make it easy to distinguish implementation,
simulation, observed hardware compatibility and future work.
