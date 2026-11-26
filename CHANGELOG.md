# Changelog

All notable changes to Astro Bridge Core. Versions follow [Semantic Versioning](https://semver.org/).

## 1.2.0 — 2026-11-27

### Added

- One bridge password for the web interface, the hotspot and the Linux account (`root` on the Raspberry Pi 5 image, `bridge` on the Pi Zero W image). One change sets all three or none: the Linux account first, then the web hash and the hotspot key in a single save. Rule: 8 to 63 printable ASCII characters without spaces, not the initial 12345678. Earlier Setup releases that change the web or hotspot password change all three. Renaming the hotspot after a personal password asks for that password, since the hotspot key is made from it (`current_password` in `PATCH /hotspot`).

- No clear password is kept: the hotspot stores only its WPA key bound to its name, and an existing hotspot password is converted at start. The Pi 5 welcome screen draws the Wi-Fi code only while the initial public password is in use.

- SSH follows one rule on every appliance image: closed while the bridge password is the initial one, open once the owner has chosen theirs. `/status` reports the state SSH really applies (`ssh.password_login`, `ssh.account`).

- A camera driver catalog (`wifi_bridge/static/drivers-catalog.json`) lists ZWO, QHYCCD, Player One, Atik and Moravian, none of them verified on hardware as delivered (ZWO was served once with SDK 1.41 supplied in an image, not with the SDK 1.42 it now downloads), every one at its official download address. No manufacturer file is part of any image, ZWO included. A build becomes installable only once its archive, library and licence fingerprints are recorded. Acceptance is recorded per manufacturer with the exact library, the terms page, the catalog version and where it was given (`setup`, `web`).

- Configuration, Cameras: a driver whose fingerprints are known can be installed from the bridge. After the owner accepts the manufacturer's terms, the bridge downloads the archive itself (HTTPS, verified certificate, the catalog host and its listed redirect hosts only, bounded size, SHA-256 checked while receiving) in the background, then installs only the listed files. Without Internet, the page says to connect the bridge to a Wi-Fi network with Internet, or to write the card again with Astro Bridge Setup and tick the driver.

- Camera adapters for Player One, QHYCCD, Atik and Moravian, written from each manufacturer's official SDK header and documentation, **documented, not verified on hardware**. Each manufacturer library runs only in the child process of an opened camera and is loaded only once its driver is installed and accepted. A missing library, or one that does not load (glibc libraries on Alpine rely on `gcompat`), leaves the camera "driver missing" and never stops the bridge. Served: sensor and pixel size, bit depth, Bayer pattern where established, gain and exposure ranges, gain, exposure, sub-frame, one image at a time, abort where the SDK allows it. Not served yet, with the reason in `docs/CAMERA-DRIVERS.md`: binning except Player One monochrome cameras, colour Moravian cameras, QHYCCD cameras that load firmware at plug-in, Atik CCD gain, setting a QHYCCD temperature. A camera table may now match every product of its manufacturer's USB vendor except listed products (`vendor_id` with `except_product_ids`, root-only udev rule). Configuration, Cameras offers a manufacturer only when its adapter exists, and a camera card of such a driver says "Driver not verified on hardware. Tell us if it works." in the ten languages, and `GET /api/cameras` carries `verified_on_hardware` for manufacturer drivers.

- Configuration, Cameras says in plain words what the first boot did with each driver ticked in Astro Bridge Setup (installed, refused file, incomplete file, unknown version, other hardware, licence changed), from `camera-drivers/setup-results.json`.

- Paired apps list the telescopes and mounts the bridge hears over Wi-Fi, before any setup, and link one in one request (`GET /api/v1/instruments/nearby`, `POST /api/v1/instruments/nearby/{id}/link`, `DELETE /api/v1/instruments/{id}/link`, `docs/NEARBY-DEVICES.md`). Linking is allowed to apps paired with `connect`, without the administrator scope, and only creates a disabled profile of a recognized type at its factory address. The list stays available while another profile is active.

- A paired app's request for an instrument is no longer blocked by another one that is switched off or out of range: the absent instrument gives up the Wi-Fi adapter, and the answer and the catalog say which one and why (`displaced`, `released`). The same holds for an instrument left connected after its app released it. An instrument in use is never replaced silently: `409 NO_FREE_RADIO` now names it in `error.holders`, and `force_replace` replaces it after the user's confirmation. `disconnect` accepts `reason: "switched_off"`, reported as switched off on request instead of a lost link. See `docs/NATIVE-CLIENT.md`, Radio release.

- A SynScan mount left without a client may switch its own Wi-Fi off (seen on an AZ-GTiX: about 11 minutes after power-on, status light steady, back after switching it off and on). The bridge keeps a linked mount joined, with its read-only version query every 30 s, until it is disconnected explicitly. A refusal that names such a mount in `error.holders`, and a confirmed replacement in `displaced`, carry `wifi_sleep`. The catalog entry and a failed `NETWORK_UNOBSERVED` operation carry `silence` (since when it is no longer heard, probable cause, never established). See `docs/WIFI-MOUNTS.md`.

- The web interface flags a USB Wi-Fi adapter plugged through a hub (`usb_hub` in the adapter list), on its row and in the adapter notice, in the ten languages, and asks to plug Wi-Fi adapters directly into the board when it has enough ports. Read from the USB topology; the board hubs of a Pi 3 or Pi 4 are skipped.

- The administration page says when the bridge clock was last set, in plain words: by network time, from an approximate hint of a signed-in browser, or never. `/status` carries the same information (`last_sync`), kept across restarts in `clock-sync.json`.

- The interface uses one word for the appliance in every language (pont in French, ponte in Brazilian Portuguese, 桥接器 and 橋接器 in Chinese), and addresses the user in one register per catalog (du, tú and tu in German, Spanish and Italian).

### Fixed

- With Internet sharing on, the hotspot's DHCP service is no longer restarted about 19 times a minute. A route update wrote its configuration without the sharing and the next pass wrote it back. 1.1.0 had the same fault.

- Configuration, Cameras: the ZWO ASI driver is installed like the other manufacturers' drivers (Install, or ticked in Astro Bridge Setup), downloaded from ZWO's site on request. The licence dialog for a library supplied with the image, the Activate button on a camera card and the `/api/cameras/sdk` routes are gone, since no library is supplied any more. Deactivate works as for the others: the driver stays installed and stops serving cameras, and the line then offers Reactivate (the manufacturer's terms are asked again, nothing is downloaded) or, when the files are no longer exactly the checked ones, Install. This holds for every manufacturer.

- When the hotspot does not start, stops, or stays unconfirmed, the service log says the state of its radio at that moment (radio type, whether the station manager still holds it, NetworkManager and hostapd states, what hostapd logged since its start, driver errors), twice per radio at most, without network names or passwords.
- Pi 5 image: an `apk add` at the console no longer removes the bridge runtime installed at boot. A wrapper in `/usr/local/sbin/apk` keeps the installed runtime packages in the world for the time of each change and restores the base world afterwards; the boot scripts call `/sbin/apk` directly. `docs/TROUBLESHOOTING.md` gives the safe way on earlier images.
- Pi 5 welcome screen: the codes no longer vanish to a black screen a few seconds after they appear. Any failed check used to clear them at once, including the short failures while the hotspot restarts. Codes that are still right now stay on screen with a line saying the bridge Wi-Fi is restarting, wrong ones are replaced at once by the reason in plain words, and a screen is never left blank. The Wi-Fi code and the web code are drawn at the same QR version with the same quiet zone, so they have the same size, and the layout shrinks (narrower quiet zone, then half-height blocks) to fit smaller screens. The console blanking timer is turned off on that screen.
- A paired app that asks again for an instrument within two minutes of a failed attempt is served: the pause after a failure (`409 TARGET_BACKOFF`) now paces the bridge's automatic attempts only. Apps showed that refusal as a bridge that does not answer.
- A link that cannot be made says which step failed, beside its unchanged reason: `failure_stage` on the operation and on the catalog entry is `NETWORK_NOT_FOUND`, `NOT_ASSOCIATED` or `ADDRESS_NOT_OBTAINED`, so an app can say to restart an instrument that no longer gives addresses.
- An instrument that is linked but has lost its address is waited for like any lost link. Reading its empty address ended the whole reconcile with an internal error, which also reported every other instrument as failed. The internal error line of the service log now says where it happened.
- A SynScan mount that accepts the Wi-Fi link and gives no address is linked anyway: the bridge takes a free address of the mount's fixed network (probed first, kept only if the mount answers) and the catalog says so (`address_fallback`). Owner switch `auto_policy.address_fallback`, on unless set to false. Not yet verified on hardware.
- The bridge keeps its DNS-SD announcement alive: it starts the announcement daemon again when it finds it stopped, and a reload that fails becomes a start. The announcement carries `boot`, which changes at every start of the bridge, so a client knows it must check the bridge again.
- A client that wrote to a relayed UDP port (SynScan, 11880) before the relay existed, and keeps the same socket, is relayed once the relay is installed: the bridge forgets the untranslated flows to its own relay port each time it installs a changed plan.
- A lost instrument is retried with a growing wait (15 s doubling up to five minutes) instead of every 15 s forever, which kept the adapter and the bridge's network busy. The catalog reports the attempts (`retry`).
- ZWO ASI cameras can be served again. The driver finds the SDK library by its base name and reads its version instead of requiring SDK 1.41 in a fixed folder. A version not yet tested with this release (SDK 1.42, served by ZWO today) loads with one warning in the service log.
- A bridge whose clock is earlier than this release (a board without a battery clock and without network time) takes the time of the signed-in browser, and the interface shows the wrong bridge time while it lasts. The ten-year bound of time hints no longer rejects such a clock.
- The web interface loads much less over a weak hotspot: each language is a separate file loaded only when chosen, the fixed files are sent compressed (gzip, prepared once per process) and versioned files are cached by the browser. The first page drops from about 2 MB to under 200 KB.
- The web interface names the bridge adapter that carries a link "Wi-Fi adapter B" instead of "USB connection B", which read as an instrument plugged by USB, in the ten languages.
- The private network form no longer asks for the Wi-Fi adapter when one is already reserved for the private network in Wi-Fi adapters.
- A hotspot that stopped on its own no longer leaves its radio unmanaged forever. The bridge hands the radio back once both AP services are confirmed stopped, stops and restarts a recorded hotspot that stays unconfirmed for 20 seconds, and after two failures on one radio tries another free Wi-Fi adapter first, without changing the saved adapter choice.
- Before starting the hotspot on a radio it has just taken from NetworkManager, the bridge waits a few seconds for the station manager to really let go of it (its peer-to-peer device disappears). Starting earlier let the access point be refused its keys, or start and then lose its radio while still reported enabled.
- A newly started hotspot must keep its radio in access-point mode for 8 seconds. When the radio's previous station manager hands it back as a station after hostapd started, the AP service is restarted once, and a second loss counts as a start failure.
- Hotspot starts, stops and failures are written to the service log in plain words, without network names or passwords.
- A Wi-Fi mount whose own DHCP server is slow (an AZ-GTiX answers 27 to 31 s after the link) gets its usual address again. The bridge waited 35 s for the whole link, gave up a quarter of a second before the address arrived and took its fallback address instead. An instrument link now waits 65 s, which covers the address timeout of NetworkManager. The home Wi-Fi keeps its 35 s.
- Relayed instrument ports answer when the bridge is on one network by cable and by Wi-Fi together. The bridge now accepts relayed replies whichever of its interfaces routes to the client.
- A restart or a shutdown of the bridge takes a few seconds again. It used to wait for a link attempt in progress, up to about two minutes, and no new home or instrument attempt starts once a stop is asked.
- The service log has one line each time the reason for the next home Wi-Fi attempt changes (`Astro Bridge home Wi-Fi:`), without network names or passwords.
- The web interface names the list of saved equipment "Instruments" in the ten languages. Some error messages of the API still say "telescope", since they are part of its contract.
- The Raspberry Pi 5 appliance image announces itself over DNS-SD and may use its wired uplink for the onboard radio, through a root-owned image marker instead of the private camera opt-in only.

## 1.1.0 — 2026-11-17

### Added

- Native client contract 1.2 (`docs/NATIVE-CLIENT.md`, quick start, `contracts/native-client/v1/` replay scenarios and `tools/contract-mock.py` loopback mock): scoped pairing, endpoint inventory, leases and night reservations, retained events with catch-up, telemetry sessions and operation state.
- Owner-enabled, read-only instrument telemetry adapters (Seestar events, Vespera status, Alpaca) with retained sessions, export and deletion.
- Physically attached cameras served over ASCOM Alpaca (ZWO ASI, UVC, gPhoto, INDI bridge, loopback simulator), camera descriptor tables and conformance fixtures. No vendor SDK is bundled.
- Celestron NexStar hand controls over USB serial, relayed over TCP (one client per port, stop on lost link), and the proposed contract 1.3 serial extension (`docs/SERIAL-MOUNTS.md`).
- Station profiles, wired (Ethernet) management, readiness reporting and maker-key import with encrypted storage (no key is distributed).
- Seestar `scan_iscope` probe sent as compact JSON ending in CRLF.
- `SECURITY.md` and this changelog.

### Changed

- Version 1.1.0 of the wheel, installer and OpenAPI document; the wheel now packages nested application modules and camera tables.
- README: equipment table with separate "implemented", "tested in simulation" and "verified on hardware" columns, and a section on Astro Bridge Setup.

### Known limits

- Most families are implemented and simulated but only partly verified on hardware; see the README table. Documented is not verified.

## 1.0.1 — 2026-10-28

- Compact header with activity and account menus; installer and documentation fixes. See the [1.0.1 release](https://github.com/Sqound/astro-bridge-core/releases/tag/v1.0.1).

## 1.0.0 — 2026-10-28

- First public release.
