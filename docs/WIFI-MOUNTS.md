# ZWO and SynScan Wi-Fi mounts

This is the application-side implementation and physical acceptance plan for
0.6.30–0.6.32. A physical AM5N has now passed Wi-Fi association, read-only model
identification and TCP4030 relay through the private-network Pi address. Native-app
control, guiding and automatic replacement are still untested. SynScan physical
acceptance is pending. These results do not establish every model or application.

## Connect a mount

1. Add an instrument and choose **ZWO mount** or **SynScan mount**. Select its
   actual Wi-Fi network and enter its current password. `AMH_…` and `SynScan_…`
   names suggest the family only. Since0.6.34 the scan assistant can insert the documented
   AMH factory password; change it if the owner changed the mount password. Manual
   profiles and saved credentials are not overwritten.
2. Check the target address. Both documented direct Wi-Fi transports commonly
   use `192.168.4.1`. Two overlapping instrument networks cannot be active
   simultaneously in the current routing design; use profile switching.
3. Connect the profile, then use the bridge address displayed on its card in the
   mount application: TCP **4030** for ZWO or UDP **11880** for SynScan. The address
   depends on whether the phone uses the hotspot or private Wi-Fi. Using the Pi
   address avoids a router route for these supported proxy ports. Direct access
   to the mount's original address from private Wi-Fi still requires a route.
4. For automatic switching, include the family in Advanced switching settings
   and allow automatic connection requests for the saved profile. An existing
   installation keeps newly introduced family triggers off until explicitly
   enabled; fresh settings include all supported families, with the master
   automation switch initially off. Multiple eligible matching profiles require
   explicit selection.

Opening an application is not by itself a detectable event. It must send a
supported connection attempt or search on the bridge's reachable network.
Whether each manufacturer's app offers a custom address or repeats a search
after connection is a physical acceptance item. There is no generic beacon or
embedded Alpaca-server claim here.

## Implemented transports and identification

| Family | Native path | Explicit identification | Evidence boundary |
| --- | --- | --- | --- |
| ZWO AM-series | Pi-address TCP 4030 proxy; initial TCP SYN can request guarded selection | `:GVP#`, bounded ASCII AM-series model reply ending in `#` | Manufacturer v2.1 transport. Actual AM5N association, model reply and private-network proxy passed on 0.6.30; native-app/control/guiding/switching pending. |
| SynScan Wi-Fi motor controller/module | Pi-address UDP 11880 proxy; version searches can request guarded selection | `:e1\r`, reply `=` followed by six hexadecimal characters and CR | Manufacturer motor-controller protocol. Exact owner's mount/module and firmware remain to be tested. |

The explicit **Reachable devices** action probes only enabled, associated saved
profiles, never an address sweep. Each mount query binds the selected radio and
has a 2.5-second total deadline. Reconciliation does not send protocol probes.
The SynScan discovery relay accepts only version reads (`:e1\r` / `:e2\r`),
forwards them to an already selected mount and returns a valid reply from that
exact address/port. It does not fabricate identity, station data or Wi-Fi settings.
Selection requests received during reconciliation use bounded, expiring intents.

Protocol replies remain unauthenticated observations. The SynScan raw board
version is retained without inventing a model, serial or firmware component from
its mount-code byte. Wi-Fi BSSID binding remains a separate owner-confirmed or
observed physical association.

SynScan **11880** is the motor-controller transport. It is distinct from the
**11881** UDP/TCP server exposed by the SynScan application and from hand-controller
interfaces. Supporting one does not establish support for the others.

## Protect control and guiding

Unlike bounded background exchanges from a smart-telescope home screen, **all
observed mount forwarding traffic protects the connection**, including small
guiding or control packets. Unknown activity remains protected. Quiet traffic
does not establish that tracking or an autonomous operation ended. Keep a manual
owner lock when the mount must remain connected without visible traffic.

Discovery/identification sends no slew, tracking, guide, park, alignment,
initialization or settings command. Normal client commands still pass through
the selected proxy; Astro Bridge does not replace the mount driver.

Because both families can use the same original address, retries aimed at an
unselected family may conservatively count as traffic on the selected mount's
direct route. The safe fallback is an explicit selection after stopping the old
client. Do not exempt all small mount packets to make switching appear faster.

## Physical test sequence

On 2026-10-27 the owner's AM5N was selected by its observed `AMH_…` Wi-Fi and
explicitly supplied credentials. The Pi's built-in radio received `192.168.4.4/24`;
the model query returned `AM5N`. An external private-network client sent the same
read-only query through the Pi's TCP4030 proxy and received `AM5N` in 0.039s. The
hotspot remained on its original USB adapter, with Internet-sharing settings and
quiet40s/cooldown60s preserved. This was not a native-app, movement, guiding or
automatic-switch acceptance test. The profile starts without automatic eligibility
or automatic replacement; existing family demand settings were preserved.

Record exact model, controller/module, firmware, app/OS and radio. Establish a
direct Wi-Fi reference first. Then test bridge identification, Pi-IP connection,
read-only status, reconnect, app closed/open, and release after traffic stops.
Verify automatic switching in both directions, ambiguity, locks and unavailable
targets while retaining management access. A competing request must not replace
active control/guiding. Movement or guiding checks need a separate explicit,
safe hardware session; identification does not authorize them.

Record operation duration, provider acceptance, actual phone notification receipt
and click destination separately. See [Adding hardware](ADDING-HARDWARE.md) for
the full evidence matrix and PR checklist.

## SynScan preparation and first hardware session

The motor-controller transport and discovery adapter were included in0.6.30/0.6.31.
Version0.6.32 completes the broadcast-search-to-unicast connection path described
below. Physical SynScan support remains unvalidated; the following session still
needs the exact mount and Wi-Fi module details.

The official adapter manual uses `SynScan_WiFi_####`; the AZ-GTi manual also uses
`SynScan_xxxx`. Both match the existing `SynScan_` family hint. Indexed search
excerpts sometimes omit the underscores: consult the PDF itself for exact names.
Factory settings can be changed, so the bridge must use the observed network and
its current authentication instead of assuming a factory password or open Wi-Fi.

| Stage | Action | Required evidence |
| --- | --- | --- |
| Hardware reference | Record exact mount, built-in Wi-Fi or external module, firmware, SynScan/SynScan Pro version and phone OS. Establish a working direct connection first. | Actual model/controller and direct connection, not an SSID-derived model guess. |
| Bridge profile | With client apps closed, scan the mount Wi-Fi, select SynScan, check `192.168.4.1` and bind the observed access point. | Correct BSSID, security and association on the instrument radio; home/hotspot retained. |
| Read-only identity | Use Reachable devices while the selected mount is associated. | A real motor-board version reply to `:e1\r`; retain the raw code, do not invent a serial or model. |
| Fixed IP | In SynScan Pro, Settings → Connect Settings → Network, turn Find Device off and enter the bridge IP in Fixed IP. Then use Connect. | SynScan connects through the displayed UDP11880 proxy. Start with `192.168.77.1` on the AstroBridge hotspot, or the bridge address shown on the private network. |
| Native discovery | Turn Find Device on and use Connect again. | A real search reaches the bridge; replies identify the bridge address and the native app accepts it. Test each management network separately. |
| Protection | Observe ordinary app status, then close the app. | Every mount-control/guiding packet protects the connection. After traffic stops, require fresh measured quiet and the configured release interval; silence is not proof tracking ended. |
| Automatic selection | Enable SynScan searches and this profile's eligibility, with an explicitly replaceable quiet previous instrument and no lock. Use Connect/Find Device. | Genuine SYNSCAN_DISCOVERY operation, readiness, measured duration and phone notifications; no forced replacement. |
| Recovery | Disconnect/reconnect the client, restart the mount when safe, and repeat both directions of switching. | No stale relay, repeated warning flood or management loss. |

The SynScan app manual documents searching **when Connect is used**. Merely
opening its home screen is not a documented discovery trigger. Automatic switching
can react only to a supported search that actually reaches the bridge. If several
eligible SynScan profiles exist, the bridge requires explicit selection.

ZWO and SynScan commonly share the direct address192.168.4.1, so they cannot both
own that subnet simultaneously in the current design. The owner's AM5N profile
currently disallows automatic replacement; do not change that protection or move
its radio just to prepare this test. Station mode is a separate future acceptance
path; do not reconfigure the mount's AP/STA settings during direct-AP discovery.

This profile connects directly to the motor controller over UDP11880. A third-party
driver that instead talks to **SynScan App** must use the app's documented service
on the device running that app (for example TCP11882 for the serial-emulation
interface). The Pi does not impersonate SynScan App or provide its alignment model.

The expanded isolated Linux packet test covers actual broadcast version searches
on home and hotspot, both axes' exact replies, unchanged unicast status/position
datagrams, rejection of non-version broadcasts, and an unselected-profile demand
without a fabricated reply. Those tests use synthetic endpoints and establish no
physical model, native-app acceptance, guiding or movement capability.

### Broadcast reply and subsequent unicast on the same UDP socket

The expanded test initially found that version discovery succeeded but subsequent
status/position requests timed out when the client reused its UDP socket. A local
reply from Pi:11880 created a reverse conntrack tuple; the next client unicast was
treated as that local flow's reply instead of a new DNAT connection to the mount.

Version0.6.32 adds an owned raw OUTPUT chain only while a SynScan proxy is selected.
It leaves locally generated UDP11880 replies to the confirmed home/AP client prefix
untracked, constrained by output interface and exact Pi source address. Forwarded
mount traffic does not traverse OUTPUT, continues to use DNAT/SNAT and remains
tracked for control/guiding protection. There is no blanket UDP notrack rule and
no change for other instrument families. The chain is withdrawn with the proxy.

The regression uses the **same actual client socket** for both-axis broadcast
queries followed by unicast status/position. It runs on both management interfaces,
checks the upstream datagrams to prove non-version broadcasts were not forwarded,
and tests selection requests with no selected mount or invented reply. Both reply
correlation and routing are real Linux behavior against a synthetic mount.

### Datagrams sent before the relay exists

Address translation is decided once per flow. A client that writes to the
bridge's UDP 11880 while the relay rule is absent (the mount is not linked yet,
or its link dropped) leaves a flow delivered to the bridge itself. As long as it
keeps writing from the same port, its datagrams follow that flow and never reach
the mount, even after the relay is back.

Each time the bridge installs a changed plan with a UDP relay, it now forgets
the untranslated flows addressed to its own address and relay port
(`conntrack -D -p udp --orig-dst <bridge> --orig-port-dst 11880 --reply-src <bridge>`).
Relayed flows answer from the mount's address and are not touched. A missing
`conntrack` tool changes nothing else. Checked with a patched command runner
only; the Linux behavior remains to be verified.

### One network on two interfaces

The relay rules accept a client on the management interface whose address the
bridge gives it. A bridge linked to the same network by a cable and by Wi-Fi
answered address resolution for the Wi-Fi address through the cable too, so a
client could enter by the cable: the interface and its API answered, and no
relayed port did. The appliance images set `net.ipv4.conf.all.arp_ignore=1`:
each interface answers for its own address only. Not yet verified on hardware.

A client needs nothing else: no reservation, token or declared source address
is checked by the relay, only the client's network and the bridge's address.

## SynScan mount: Wi-Fi switched off when left without a client

What was seen on the bench, on one AZ-GTiX, with the bridge on a Raspberry Pi 5:

| Situation | Result | Status |
| --- | --- | --- |
| Powered on, no client joined, no movement | Its Wi-Fi stops 11 to 12 minutes after power-on. Its status light turns steady each time. | Observed on the AZ-GTiX |
| A client joined the whole time | No stop in 28 minutes | Observed once |
| Moved through the app | No stop in 55 minutes | Observed once |
| 15 links one after the other | No stop | Observed once |
| A movement, then no client at all | Not tried | Unknown |
| Other SynScan models, other firmware | Not tried | Unknown |

No Sky-Watcher document found describes this stop or the steady light. Only
forum reports mention it. Treat it as observed behaviour of one mount, not as
a documented feature.

**Recovery:** switch the mount off, then on again. If its status light is steady,
its Wi-Fi has stopped. Nothing the bridge or an app sends can wake it.

What the bridge does about it:

- While a SynScan mount is linked, the bridge stays joined to its Wi-Fi and sends
  the read-only version query (`:e1`) of its service check every 30 seconds, skipped
  while relayed traffic is already flowing. Nothing else is sent: no movement, no
  setting. The bridge stays joined until the user disconnects it explicitly
  (`POST /api/v1/instruments/{id}/disconnect`), says it was switched off, or the
  mount itself goes away. Releasing an app reservation (`release`) keeps it joined.
- A linked mount never gives its radio away silently. It is always `in_service`:
  another request is refused with `409 NO_FREE_RADIO`, and the refusal names it.
  Each holder entry of a sleep-prone mount carries `wifi_sleep` (the observed delay,
  the steady light, the recovery). The refusal message says the mount may switch
  its Wi-Fi off after about 11 minutes once left without a client. A confirmed
  `force_replace` reports the same `wifi_sleep` in `displaced`.
- The instrument entry carries `silence` once the mount has not been heard for a
  minute, by its link or by the bridge scans: `since_seconds`, `last_heard`
  (`link` or `scan`), `probable_cause`, `cause_established: false`, `sign` and
  `recovery`. `probable_cause` is `MOUNT_WIFI_SLEEP` when the bridge was not joined
  to the mount when it was last heard, and `UNKNOWN` when it was (with a client
  joined, the stop was not seen). A failed connection whose reason is
  `NETWORK_UNOBSERVED` carries the same `silence` in its operation.

The usual case: the mount is switched on, the bridge only hears it in its scans,
and the app asks for it a quarter of an hour later. The mount has stopped its Wi-Fi,
the bridge reports `NETWORK_UNOBSERVED` with `silence.probable_cause`
`MOUNT_WIFI_SLEEP`. The bridge does not join a mount nobody asked for, so it
cannot prevent this case. Ask for the mount soon after switching it on.

## Primary references

- [ZWO Mount communication protocol v2.1, manufacturer document mirrored by INDIGO](https://raw.githubusercontent.com/indigo-astronomy/indigo/master/indigo_drivers/mount_asi/docs/ZWO_Mount_communication_protocol_v2.1.pdf): Wi-Fi address/port and LX200-compatible command definitions; the history changes the model query to `:GVP#`.
- [ZWO guides and manuals](https://www.zwoastro.com/guides-and-manuals/).
- [Sky-Watcher Motor Controller Command Set](https://inter-static.skywatcher.com/downloads/skywatcher_motor_controller_command_set.pdf): UDP 11880, one command/reply per datagram and motor-board version framing.
- [Sky-Watcher developer manuals](https://www.skywatcher.com/download/manual/application-development/).
- [SynScan Wi-Fi adapter manual](https://inter-static.skywatcher.com/downloads/synscanwifi_102.pdf): factory Wi-Fi name, AP address and UDP port; AP/STA settings are separate.
- [AZ-GTi manual](https://inter-static.skywatcher.com/downloads/azgti_manual_v102.pdf): built-in `SynScan_xxxx` Wi-Fi variant, AP address and UDP11880 transport.
- [SynScan App manual, 2023-05-25](https://inter-static.skywatcher.com/downloads/synscan_app_manual_en_20230525.pdf): Connect Settings, Find Device/Fixed IP, and the distinction between direct motor transport and third-party app server interfaces.
- [Discovery study](DEVICE-DISCOVERY-STUDY.md): distinguishes announcements, active queries, SSID hints and connection-demand detection.
