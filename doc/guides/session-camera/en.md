## Sessions and local history

Connected instrument cards show enabled read-only session data: operation, target,
values with units and their ages. After three source intervals a value is marked
as the last reading; Paused means the observer yielded, not that the instrument
stopped. Missing values stay unknown. Choose session data in each telescope
profile. Seestar stacking requires an owner-supplied key; getting one needs an
explicit action and Internet on the bridge. Saved listening choices survive key
replacement. DWARF remains link-only with one app at a time.

Open session shows retained charts, frame maxima, rejection reasons and environment.
Observed exposure counts only witnessed kept-frame increments at an unchanged
exposure length. Missing baselines, changes and resets can make the total partial;
the latest single-frame duration is separate. Hatched chart ranges mean no recorded
raw-data download by a paired app, not that an app was closed. Old or aggregated
audit coverage stays unknown. Unverified clocks show approximate times; sequence
ordering remains valid. Recent events are limited to 500 readings and lifecycle
records; CSV/JSON exports contain all retained readings, with events and reader
names also in JSON. Configuration → Sessions controls nights and MiB retention;
the first limit reached applies. Lowering limits can remove old data. Deletion is
irreversible: export first, then explicitly confirm. Only paired apps with telemetry
or administrator permission may read sessions; these permissions never control an
instrument. History stays local even without a reading app.

## Attached cameras and their limits

Pi Zero W and Zero 2 W are for connections and telemetry only. Camera service is
for the approved Pi 3/4 tier; start physical camera acceptance on a Pi 3B bench.
In Configuration → Cameras, enable serving and discovery deliberately. Demo starts
empty; its plug button adds only a loopback simulator, never a physical camera.
Clients discover the bridge and ask it for actual per-connection endpoints; users
need not type an instrument address. Alpaca discovery answers only on confirmed
management networks, and an address appears only after its listener binds.

Only physically attached supported cameras are served. UVC needs supported capture
formats and owner pixel-pitch calibration via camera-calibration.json; no calibration
editor is provided. Follow the owner procedure, preserving other
entries, then safely reattach or restart. Port-based attachment IDs do not prove
physical identity. UVC captures bounded luminance frames (GREY/Y16/YUYV), at most
1280×960 with binning 1. Longer requests average frames; this is not a true continuous
long exposure. Without exposure controls, frame duration is nominal. A shutterless
camera does not manufacture dark frames. No manufacturer SDK is bundled; SDK
cameras require an installed supported driver. Large transfers over 2.4 GHz may be
slow; supported binning 2 is only an initial default, and apps retain their choices.

One Alpaca client owns a camera at a time. ClientID is a protocol number, not a
paired-app identity; DHCP names appear only on a current exact address match.
Let this app use it requires explicit release consent for the displayed current
owner and can interrupt an exposure. Changed ownership invalidates old consent;
the other app must reconnect after release. This does not automatically grant a
new owner. Repeated disconnects suggest checking cable/powered hub, not a proven
voltage fault. Real camera/phone behavior, Pi performance and ConformU acceptance
remain **documented, not verified on hardware**; see USB limits.
