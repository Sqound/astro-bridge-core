# Attached camera service and Alpaca Camera V3

This document describes a separate, local Alpaca service for physically attached cameras.
The foundation takes explicitly registered reviewed drivers. It performs no USB
probing and does not invent detected cameras; physical discovery is separate (see USB discovery).
A simulator is injected only by tests or the explicit loopback-only
[conformance demo harness](CAMERA-CONFORMANCE.md). Pi Zero W remains the connection/telemetry
tier; served cameras target Pi 3/4. No hardware acceptance is claimed.

## Transport and owner boundary

The Alpaca listener uses TCP 11111 by default, configurable from 1024 to 65535.
It never shares the bridge HTTP listener. Camera paths are
`/api/v1/camera/{device_number}/{member}` on this separate listener. The bridge's
`/api/v1` resources remain on its original listener with Bearer/cookie protection.
Alpaca has no Bearer requirement, consistent with existing Alpaca clients.

Each TCP listener binds an explicit confirmed home/hotspot IPv4 address. Host
headers must match an allowed exact management host and the listener's actual
port; malformed/duplicate Host headers fail closed. Peer addresses must be in
that listener's management subnet. Optional allowed subnets narrow this boundary.
There is no wildcard TCP, instrument-facing listener or arbitrary outbound access.
A small runtime reconciles at most two paths, removes obsolete listeners and
retries failed binds no faster than every five seconds. A failed TCP or UDP bind
rolls back that path and publishes no endpoint for it.
The owner `serve` switch defaults true, but no listener is useful or advertised
without a registered camera. An explicit off decision persists across restarts.

UDP discovery answers exactly `alpacadiscovery1` with `{"AlpacaPort":11111}` using
the successfully bound listener port (which can briefly differ from a newly
configured port until rebinding completes). On Linux broadcast reception binds the UDP socket to the
explicit management interface with `SO_BINDTODEVICE`, then binds UDP 32227;
it cannot receive on other interfaces. Non-interface test sockets bind a specific
loopback address. Replies are limited to one per source per second and eight per
second per management responder; at most 64 source timestamps are kept. Disabled serving suppresses
both discovery replies and device operations. Actual LAN/hotspot broadcast delivery
is **documented, not verified on hardware**.

All driver calls run on one owning worker, including inventory polling. The queue
holds at most 16 calls and TCP admits at most eight handlers with five-second socket
timeouts. Driver-queue requests wait at most one second; timed-out queued
work is canceled. Settings/enabled reads use independent bounded metadata, and
shutdown waits at most 0.5 seconds. A stalled SDK worker is never replaced; it
closes devices when it eventually returns. Form bodies are limited to 4096 bytes, URLs to 2048 and parameters to 24.
Parameter names are case insensitive; duplicate names after folding are rejected.
URLs/member names stay case sensitive as required. Client and transaction IDs are
unsigned 32-bit values; omitted IDs use zero. Server transaction IDs increase and
wrap without returning zero. No raw requests, images or client addresses are logged.

StartExposure reserves a camera for `{client_id,address,since}`. Other identities
cannot expose, alter settings, abort or disconnect its camera and receive 0x40B
with `camera in use by another client`. Matching ClientID from another source
address is also refused. The lease expires after 120 seconds continuously idle;
active exposure/readout never expires. Background state polling detects completion,
so expiry does not require a client request. The administrative service `release`
action closes the device, discards its frame and releases the owner; the bridge also provides
the owner-facing “Let this app use it” interface. Connected is global for this V3
service; this does not claim V4 multi-client connection semantics.

## Images

ImageArray honors `Accept: application/imagebytes` (positive quality) with a
44-byte little-endian metadata version 1 header. ImageElementType is Int32 (2),
TransmissionElementType is UInt16 (8) or Byte (6), Rank is 2, and dimensions are
width, height, zero. Data starts at byte 44, with x outermost and y varying fastest.
Errors negotiated as ImageBytes contain a nonzero error header and UTF-8 message.
JSON has `Type:2`, `Rank:2` and nested `[x][y]` arrays. Images over 1,000,000 pixels
return 0x500 with an ImageBytes suggestion. Both forms stream in bounded chunks
from the immutable packed frame, without allocating a transposed frame or full
JSON document. A camera retains only its last frame; no image is written to disk.
The last-image inventory reports bytes and exposure seconds once retrieved.
The Alpaca endpoint is null until a management listener is successfully bound;
the configured port alone is not a readiness claim. Bridge HTTP inventory
projects availability for the accepted socket’s local address, so a working home
listener cannot imply that a failed hotspot listener is reachable.

## Member coverage

All Camera V3 and its common device members have routes. Current driver contracts
expose symmetric binning, ROI, numeric gain, exposure/abort and packed images.
Unsupported features return NotImplemented (0x400), never invented measurements.
Platform 7 / Camera V4 additions are not advertised (`InterfaceVersion=3`).

| Members | Behavior |
| --- | --- |
| connected (GET/PUT), description, driverinfo, driverversion, interfaceversion, name, supportedactions | Connection and fixed service/driver metadata; empty supported actions |
| action, commandblind, commandbool, commandstring (PUT) | 0x400; no arbitrary device command passthrough |
| binx, biny, numx, numy, startx, starty, gain (GET/PUT) | Validated settings; Alpaca binned ROI coordinates converted to driver unbinned coordinates at exposure |
| camerastate, cameraxsize, cameraysize, maxadu, maxbinx, maxbiny, gainmin, gainmax, pixelsizex, pixelsizey, sensorname, sensortype | Normalized driver metadata/state |
| bayeroffsetx, bayeroffsety | 0x400 for monochrome cameras; colour offsets remain relative to the sensor origin, independent of subframe settings |
| canabortexposure | Driver capability |
| percentcompleted | Integer 0 to 99 from the requested duration while exposing (0 while waiting), 100 while reading or when the image waits; 0x40B (invalid operation) otherwise, as the standard requires |
| canasymmetricbin, canfastreadout, cangetcoolerpower, canpulseguide, cansetccdtemperature, canstopexposure, hasshutter, ispulseguiding | False with the current driver contract |
| exposuremin, exposuremax, lastexposureduration, lastexposurestarttime, imageready, imagearray | Exposure metadata/readiness/image; last exposure fields require a completed exposure |
| startexposure, abortexposure (PUT) | Driver operations, with ownership and state checks; HasShutter=False means a valid Light=False is ignored and normal incoming light is captured, without synthetic dark pixels |
| readoutmode (GET/PUT), readoutmodes | One default mode (0) |
| ccdtemperature, coolerpower, gains, heatsinktemperature, imagearrayvariant, offsetmin, offsetmax, offsets; exposureresolution, electronsperadu, fullwellcapacity when the driver does not report them | 0x400 |
| cooleron, fastreadout, offset, setccdtemperature, subexposureduration (GET/PUT) | 0x400 |
| pulseguide, stopexposure (PUT) | 0x400 |

Management JSON routes are `/management/apiversions`,
`/management/v1/description`, `/management/v1/configureddevices`, plus the brief's
`/configureddevices` alias. Camera UniqueID derives from a random per-bridge
identity persisted in a private file and the stable camera ID.

## Sources and validation

The implementation was checked against the official
[Device API](https://ascom-standards.org/api/AlpacaDeviceAPI_v1.yaml),
[Management API](https://ascom-standards.org/api/AlpacaManagementAPI_v1.yaml), and
[Alpaca API Reference / ImageBytes](https://ascom-standards.org/AlpacaDeveloper/ASCOMAlpacaAPIReference.html).
The independent member table is in `tests/test_alpaca.py`. Its HTTP/UDP tests use
real disposable loopback sockets and an injected clock/simulator. Linux interface
binding is checked with a socket mock, not a changed host network.

After sourcing the workspace `env.sh`:

```sh
python3 -m unittest discover -s tests -p test_alpaca.py -v
python3 -m unittest discover -s tests -p 'test_camera*.py' -v
python3 -m compileall -q wifi_bridge
python3 tools/build-i18n.py
python3 tools/build-i18n.py --check
```

New protocol/display text uses
English placeholders in all non-English catalogs pending translation. Camera/phone
client interoperability, USB behavior, Linux broadcast reachability, Pi resources
and sustained exposures remain **documented, not verified on hardware**.
App-only/OTA packagers currently copy flat modules; nested camera-package delivery
is a prerequisite for packaging this service.
