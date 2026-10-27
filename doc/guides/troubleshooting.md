# Troubleshooting

[Documentation](../README.md) / Troubleshooting

| Symptom | Check |
| --- | --- |
| Hotspot is not visible | Wait for startup; check its enabled mode and assigned AP-capable adapter in Networks |
| Phone abandons an isolated hotspot | Confirm staying connected to Wi-Fi without Internet; verify the phone received a 192.168.77.x address |
| Wi-Fi scan is unavailable | Read the assistant's top status; keep a compatible adapter idle or use manual entry |
| Vespera works on hotspot, not home Wi-Fi | Add the displayed telescope-subnet route on the home router |
| DWARF is not listed initially | Use the bridge IP once in Connect by IP Address; passive search demand does not synthesize first-time discovery |
| An app search does not switch | Check master/family policy, saved Wi-Fi availability, routes, protection and cooldown |
| Instrument stays protected after closing its app | Check the quiet interval and whether coverage is known; inspect locks/reservations before changing policy |
| Two devices share a factory subnet | Select one at a time; a JSON plugin does not create network namespaces |
| Internet sharing is ready but a website fails | Check the private uplink, DNS and the phone's DHCP lease; readiness is not an end-to-end WAN test |
| PWA notifications are disabled | Follow [HTTPS and home-screen setup](pwa-notifications.md); check Internet and device permission |
| An update is unavailable on Docker/native Linux | Use the deployment's verified image/wheel workflow; appliance OTA is platform-specific |

For an issue report, include the release/build ID, deployment, adapter chipset, equipment/app versions and a short reproduction. Remove passwords, tokens, private certificates, personal network names and packet payloads.

Connection protection should not be disabled merely to hide a diagnostic. An unknown or stale observation is not evidence that a connection is free.
