# Connection modes and consultation activity

Owner design approved for implementation, 2026-10-23. Implementation and validation
are in progress; this document does not claim installed sharing or physical
consultation calibration. Installed evidence is tracked in IMPLEMENTATION-STATUS.md.

## Three clear access choices

| Choice | Phone connects to | Internet for phone | External router route for telescope |
| --- | --- | --- | --- |
| Private Wi-Fi bridge | Existing private Wi-Fi | Provided by existing router | May be needed; Vespera/Singularity direct target access requires it |
| Hotspot without Internet | Astro Bridge hotspot | No | No external router configuration; the Pi supplies telescope routing |
| Hotspot with Internet | Astro Bridge hotspot | Via the Pi's private Wi-Fi uplink | No external router configuration for hotspot clients |

The first two client paths exist today. The current dual-management configuration
keeps home and hotspot access to instruments, but deliberately blocks AP/home
forwarding. It is not Internet sharing. Do not reinterpret existing saved dual
management as consent to new forwarding. Preserve old intent during migration.

For isolated hotspot, isolation describes hotspot clients: the Pi may retain its
private Wi-Fi for administration without forwarding clients through it. This avoids
silently dropping existing management or changing the user's radio allocation.
The current allocator needs a role for home, hotspot and each active telescope;
sharing with one instrument needs three available Wi-Fi roles on this hardware.
Do not take a protected radio to satisfy the new mode.

### Shared hotspot implementation proposal

Use explicit opt-in policy with omission-preserved updates and a current revision.
Permit only the validated hotspot source subnet through the confirmed home uplink,
with stateful return traffic and source NAT. Keep instrument routes/proxies ahead
of Internet forwarding and preserve all unrelated owned firewall restrictions.
Publish owned nftables atomically; never flush the host ruleset or conntrack.

Existing dnsmasq is DHCP-only (port=0, empty DNS option). Internet sharing requires
DNS listening only on the AP address, with validated home DNS upstreams and DHCP
DNS advertisement. Retain target /32 routes and default gateway in option121.
A phone lease renewal may be needed after a DNS/DHCP policy change.

If uplink fails, keep hotspot/telescope access, withdraw sharing and show the
failure. A Wi-Fi association/default route alone does not prove Internet access.
Actual WAN/DNS validation is a separate physical acceptance step.

Hotspot-to-private-LAN connections can use NAT without a route on the home router.
Whether those connections should be allowed is a separate explicit option;
recommended default is Internet only, with no general private-LAN access. DNS to
the chosen home resolver is a limited exception, not general LAN forwarding.
The reverse direction, private LAN to hotspot devices, needs both a router route
and an explicit ingress policy; outgoing NAT does not grant that access.
Automatic multicast/device discovery between the two LANs is not promised.

## Consultation rather than network presence

The 0.6.26 collector also meters payload-free Singularity connection attempts as
bounded background when a Seestar or DWARF owns the same target address. Only
initial outgoing SYN and incoming RST+ACK on original TCP ports8082/8083 qualify:
IPv4 without options/fragments and exact IP/TCP header lengths proving no payload.
TCP options are permitted up to the protocol header limit. These packets still
count in aggregate and background counters and share the existing1024B/s and
12packets/s limits. Successful foreign-port connections, payload-bearing SYN,
unknown traffic and real content remain replacement-blocking. Rules use the
[nftables header fields](https://netfilter.org/projects/nftables/manpage.html);
namespace tests validate actual packet attribution, not just generated text.
The first SYN is classified only after its previously unmarked flow obtains an
owned mark. Coverage/freshness/quiet thresholds and activity guards are unchanged.

Owner clarifies that the telescopes can operate autonomously. The desired busy
signal is live viewing, browsing results and transfers, not autonomous acquisition,
presence announcements, control keepalives or merely an open socket.

Current activity.py conservatively treats increments in owned forwarded packet/
byte totals OR an established TCP session as activity. This is network occupancy,
not a media/consultation detector. Discovery requests are separate events that can
ask for an automatic connection; they do not prove viewing. Local Pi probes and
observers do not count as forwarded client activity. Some client keepalives do.

Do not relabel the current metric as proven viewing. A future classifier should
separate discovery, background/control, probable content transfer and unknown.
Use bounded interval deltas by profile, direction, protocol and port, no payload,
images, tokens or application command capture. A silent status/control socket is
not proof of viewing; a mixed channel cannot be assumed unused because it is slow.
Retain manual reservations/owner locks and conservative unknown/stale behavior.
A cached image on the phone generates no traffic: human attention is unobservable
from the Pi, so a delay/manual protection remains useful after the last transfer.

### Validation before adopting thresholds

For each actual model/firmware and private/hotspot path, record only aggregate
metadata with app closed, Home/status, live camera, stored-image list, image load,
download, app background and paused stream. Do not start acquisitions or motion.
Compare traffic bands and keepalive patterns; then propose thresholds, hysteresis
and release delay. No arbitrary universal byte threshold is accepted from code
inspection alone. Test that unrelated Internet traffic never marks a telescope busy.

Network acceptance should also cover preserved legacy settings, validation/current
revision, protection refusal, DNS/DHCP changes and rollback, allowed/blocked flow
directions, overlap handling, uplink loss/recovery, AP clients, native apps, existing
session marks and actual phone behavior. Namespace tests do not prove hardware.

References: [nftables NAT](https://wiki.nftables.org/wiki-nftables/index.php/Performing_Network_Address_Translation_(NAT))
and [NetworkManager IPv4 settings](https://networkmanager.dev/docs/api/latest/settings-ipv4.html).

## Implemented classifier contract (candidate, not hardware calibration)

`connection_activity.profiles` retains raw cumulative packet/byte totals and
established TCP count. `raw_active` retains the previous occupancy observation.
Separate `consultation` reports none, background_control, probable_content,
unclassified or unknown, with interval_seconds/packets/bytes. It reads named nft
counters on instrument forwarding paths only; Internet sharing does not enter them.
No payloads, commands, image bytes or URLs are collected.

A silent established socket no longer resets the quiet interval. Status/control
ports Vespera8083, DWARF9900 and Seestar4700 get separate direction counters.
Intervals containing only these small exchanges may advance the normal quiet
window: at most1024 bytes and12 packets per second across both directions. These
are conservative anomaly ceilings on known control channels, not measured media
thresholds. Any mixed/unknown-channel transfer stays replacement-blocking, however
small. Bursts over32768bytes and4096bytes/s are labelled probable content; the
label does not weaken protection for smaller transfers. `calibrated:false`
explicitly records that real native-app/firmware calibration is still outstanding.

Existing quiet delay, cooldown, reservations, owner locks, mark/catalog/bootstrap
coverage, stale-data and collection-failure protections continue to apply. Unknown
coverage never grants replacement. The classifier must not be described as a
human-presence, autonomous-acquisition or cached-image detector. Hardware acceptance
still needs app status, live view and stored-content tests for each actual model.

## Access configuration and evidence

Public config/settings add `connection_mode` (private, hotspot_isolated,
hotspot_shared) and `hotspot_private_lan` (false by default). Omitted fields preserve
the previous choice. Existing dual management migrates to private without new
forwarding. Changing access policy requires the real current revision and respects
reservations, owner locks and pending work. Selecting a hotspot mode requests an
available AP role; it never promises to take a protected adapter.

`network_access` reports configured mode, sharing availability/reason and
`internet_verified:false`. Availability means the owned home connection, matching
unique default route, valid upstream DNS and installed owned forwarding plan were
observed, not that the ISP is online. Multiple default routes remain conservatively
unavailable. IPv6 Internet sharing and reverse LAN ingress are not implemented.
Private-LAN permission covers only outgoing access to the directly attached home
network; device discovery between networks is not relayed.

Linux namespace validation in a new disposable no-network container proves public
NAT, real DNS via the AP, rejection from other interfaces, private/reverse/spoofed
flows blocked, Internet bytes excluded from telescope counters, permission withdrawal
on existing TCP and isolated/uplink-loss telescope continuity. The multi-instrument
runner retains owned marks, silent old sessions, counter history, foreign rules,
conflict behavior and maximum shape. Browser uses actual code with simulated
networks: three choices, guarded saves, draft persistence, ten languages and320px.
These tests do not establish actual Wi-Fi, WAN or native-app consultation acceptance.
Evidence: ignored artifacts/network-0622. First failed route observation and catalog
build races are retained, with separate accepted runs after fixes.
