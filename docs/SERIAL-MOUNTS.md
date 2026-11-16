# Serial (USB) mounts: Celestron NexStar hand control

[README](../README.md) · [Adding hardware](ADDING-HARDWARE.md) · [Wi-Fi mounts](WIFI-MOUNTS.md) · [Native client](NATIVE-CLIENT.md) · [API contract proposal](SERIAL-MOUNTS-CONTRACT-PROPOSAL.md)

A NexStar mount has no Wi-Fi of its own: its hand control has a serial port. The
bridge opens a USB serial adapter plugged into the Pi and offers it to the
telescope app as **one TCP access point per mount**. The app uses its existing
"serial Wi-Fi bridge" connection (host and port, no driver change). Bytes are
relayed untouched in both directions; the bridge invents none.

## What is documented and what was seen on a bench

| Claim | Status |
| --- | --- |
| Hand control serial port: 9600 baud, 8 data bits, no parity, 1 stop bit, no flow control | Documented by the manufacturer; seen on a NexStar 4SE (hand control 4.15, motor boards 5.14) |
| Commands `K` (echo), `V` (version), `m` (model), `M` (cancel GoTo), `P` (pass-through to the motor boards) | Documented; all seen on the 4SE |
| Stop = `M`, then rate 0 on both axes (fixed and variable rate, as the telescope app does for STOP) | Documented commands; the sequence of `M` plus rate 0 on the positive side was seen to stop the mount |
| Rate 0 on the **negative** side of each axis (part of the bridge stop sequence) | Documented message, **not yet seen on hardware** |
| The bridge sending the stop itself, and the single-client rule, on a real cable | **Not yet verified**: tested only on pseudo-terminals |
| Per-unit serial number in the hand control or motor boards | **None exists in the documented protocol** (see Identification) |
| USB-to-RS-232 converter with an FTDI chip and a DB9 side | Worked on the bench |
| 3.3 V / 5 V "TTL" adapters, Prolific PL2303 cables, other chips | Not verified; the hand control port uses RS-232 levels |

## Cable

The hand control serial jack takes a 4-contact (4P4C) handset-type cord. On the
bench the two **middle** contacts were ground and the two **outer** contacts were
the data lines. Several off-the-shelf cables were wired wrongly: check
continuity before you plug anything in.

```
 Hand control jack (4P4C, looking at the plug)      USB-RS232 converter (DB9)
   contact 1  (outer)  ──── data ───────────────────  pin 3  (PC transmit)
   contact 2  (middle) ──── ground ┐
   contact 3  (middle) ──── ground ┴────────────────  pin 5  (ground)
   contact 4  (outer)  ──── data ───────────────────  pin 2  (PC receive)
```

Bench cord (contact colours yellow, green, red, black): PC transmit went to the
yellow contact, PC receive to the black one, ground to green (red left free). If
`Kx` does not answer `x#`, swap the two outer wires. Contact 1 versus 4 depends on
how the cord was crimped, so test it rather than trusting colours.

The adapter must output **RS-232 levels** (a DB9 converter). Plug the hand control
in and switch the mount on before the adapter is read; the bridge never powers or
resets anything.

## Declare a mount

1. Plug the adapter into the Pi. A powered hub is advisable on a Pi Zero W.
2. Open the bridge home page. **Detected devices** lists the adapter with what
   the bridge knows: maker, product, serial number of the **adapter** and, when
   the adapter is a common cable chip (FTDI FT232, Prolific PL2303), the result
   of a read-only identification of the hand control.
3. **Add as mount…** opens a prefilled form (name such as "NexStar 4/5 SE", 9600
   8N1, the assigned TCP port, an idle timeout). Confirm. **Nothing is relayed
   before you confirm.**
4. The mount appears in **Declared devices** (next to Wi-Fi mounts, with its link
   nature "Serial (USB)") and under Configuration → Telescopes → Serial mounts,
   where it can be renamed or removed.

An app can do all of this itself with the [API](SERIAL-MOUNTS-CONTRACT-PROPOSAL.md)
and never needs the web interface.

## What the app must enter

* Connection type: the app's NexStar **serial Wi-Fi bridge** preset (TCP).
* Address: the bridge address on the network the phone uses (hotspot
  `192.168.77.1`, or the private-network address shown on the home page).
* Port: shown on the mount in Declared devices and returned by the API as
  `tcp_port`. The rule is simple: the first mount gets **2000**, the next **2001**,
  and so on up to 2099. A port is kept once assigned and can be chosen by the
  owner (2000 to 2099, no collision with other relays, which use 4030, 11880,
  the telescope ports and 10000 to 49151).
* Nothing else: serial speed and format are set in the bridge (9600 8N1).

## Rule 1: one client per port

While a client holds the link, any other connection to that port is **closed at
once**, with no byte exchanged, not queued, and it never takes the link. The
refusal is counted and journaled (`refused_clients`, `last_refused`, `journal`).
The owner is not disturbed. Why: a second program connecting while the first
moves the mount used to make the first reconnect and leave the mount turning
unattended. Only private, loopback and link-local peers may connect.

Several mounts at once is the normal case: each adapter has its own relay, port,
client, idle timeout and stop sequence. Unplugging or stopping one never touches
the others, and plugging in a second mount while the first is in use does not
interrupt it.

## Rule 2: the bridge stops the mount when the link is lost

When the client disappears (connection closed, network lost, idle timeout,
adapter unplugged, the bridge itself stopping) the bridge sends the profile's
stop sequence **once**, then frees the port. It is never sent while a client
holds the link. For NexStar, in this order:

| # | Bytes (hex) | Meaning (documented) |
| --- | --- | --- |
| 1 | `4D` | `M` cancel GoTo |
| 2 to 5 | `50 02 10 24/25 00 00 00 00`, `50 03 10 06/07 00 00 00 00` | azimuth board: fixed rate 0 (positive, negative), variable rate 0 (positive, negative) |
| 6 to 9 | same with board `11` | altitude board |

Each frame is answered `#`; the bridge waits for it (0.5 s) before the next one.
It reads no position and changes no setting, so tracking is left as it was.

* **Idle timeout**: default 120 s, adjustable 5 to 3600, 0 turns it off. A client
  that sends nothing for that long is dropped and the stop is sent. TCP keepalive
  also detects a dead phone in about 25 s.
* **A client that never wrote a byte** cannot have moved the mount: nothing is
  sent for it (a port scan, a health check).
* **Adapter unplugged while a client held the mount**: the bridge closes the
  client and keeps the stop **pending**; when the adapter is back (up to 10 minutes
  later) it is sent first, before any client is served.
* **Limits**: if the cable is cut or the bridge loses power the stop cannot be
  sent; the mount may keep moving until its own limits or the hand control. The
  app must keep its own stop-on-disconnect behaviour. Worst-case duration of the
  sequence with a silent hand control is 9 frames by 0.5 s.
* The sequence belongs to the **device profile** (`wifi_bridge/serial_profile_data.py`):
  another serial device gets its own sequence or none. With none, nothing is sent.

## Identification: what the hand control can tell

Read from the documented protocol: hand control version (`V`), model code (`m`),
motor board versions (`P` pass-through, "get version" on the azimuth and altitude
boards). The 4SE answered 4.15, model 11 ("4/5 SE") and 5.14 on both boards.
**There is no serial number or per-unit identifier in the documented protocol**,
and the bridge reads none (not verified on hardware beyond these commands).

So the bridge uses the reading to (a) confirm a NexStar hand control answers
(`identification`: `identified` / `not_identified`), (b) name the mount by default
("NexStar 4/5 SE"), (c) tell two **different** models apart whatever the plug order
and warn (`identity_mismatch`) if another model now answers on the adapter a mount
was declared on. Two strictly identical mounts are told apart **by their adapter
or their USB socket only**.

Safety of the reading: read-only commands only (`K`, `V`, `m`, `P` get-version),
never a move, a GoTo, a set or a stop; only while no client holds the link
(at plug time, or on demand with `POST /serial-mounts/{id}/identify` when the port
is free); 0.6 s per frame. A silent hand control leaves the relay usable and the
state says "not identified". Automatic reading at plug time is limited to adapters
whose USB id is a known cable chip, never to arbitrary serial devices; other
adapters are read only when the owner asks.

## Telling adapters apart

The configuration stores a **stable link**, never `/dev/ttyUSB0`:

* `/dev/serial/by-id/usb-<maker>_<product>_<serial>-if00-port0` when the adapter
  has a USB serial number (unique, follows the adapter to any socket);
* otherwise `/dev/serial/by-path/...`, tied to the **USB socket**.

Two identical adapters **without** a serial number have the same by-id name, so the
bridge offers the by-path link: each mount is then tied to a socket, and the user
must always plug the same cable into the same socket (label the sockets). Swapping
two such cables swaps the mounts; the identification warns when the models differ.
FTDI chips can have their serial number rewritten with the maker's tool; most
Prolific and CH340 cables have none.

## Limits

* At most **8** serial mounts, TCP ports 2000 to 2099.
* Pi Zero W: each relay is one thread waking five times a second and moving at
  most about 960 bytes per second at 9600 baud; the adapter scan lists two
  directories every 3 s. Estimated at well under 1 % CPU and about 1 MiB of memory
  per relay, so two or three mounts are expected to be comfortable and eight
  untested. **Estimated, not measured**; the USB port and hub power are the real
  limit on a Zero W.
* The relay has no authentication: any private-network device can use a free port.
  Use the hotspot or a trusted network; the single-client rule limits, not removes,
  that exposure.
* Wi-Fi switching protection, leases and the native instrument catalog do not
  cover serial mounts yet; they use the management API described in the proposal.
* Only the NexStar profile exists. Other serial devices need their own profile
  and bench evidence first ([adding hardware](ADDING-HARDWARE.md)).

## Test hooks

Tests and screenshots use pseudo-terminals, never a real port:
`ASTRO_BRIDGE_SERIAL_DEV_ROOT`, `ASTRO_BRIDGE_SERIAL_SYS_ROOT` and
`ASTRO_BRIDGE_SERIAL_NODE_PATTERN` replace `/dev/serial`, `/sys` and
`tty(USB|ACM)[0-9]+`. In demo mode the relays stay off unless the dev root is set.

## What the bench must check on the real cable

1. `Kx` answers `x#`; `V` answers `04 0F 23`; `m` answers `0B 23`; both boards answer `05 0E 23` to the pass-through version request, **through the bridge** (not the bench relay).
2. Identification at plug time, with the app absent: the home page shows "NexStar hand control 4/5 SE, version 4.15" and the default name.
3. One client: connect the app, then a second program to the same port: the second is closed at once; the app keeps working; the refusal shows in the state.
4. Stop on loss with a slow move running (fixed rate 3, **no tube**): kill the app; the mount must stop within about 5 s. Repeat for each direction (up, down, left, right), because the **negative** frames are not yet proven; repeat with the Wi-Fi switched off on the phone, with the idle timeout, and with the adapter unplugged then re-plugged (the stop must arrive first after re-plug).
5. Stop during a GoTo: the GoTo is cancelled, tracking is unchanged.
6. Two mounts on two adapters at once, one app each; cut one: the other is not stopped.
7. Reboot and re-plug in the other order: each mount keeps its name, port and adapter.
8. Timing of the 9-frame stop with the real hand control (expected well under 1 s).
