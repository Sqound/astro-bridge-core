# Discovery and manufacturer-app connections

[Documentation](../README.md) / Discovery

Discovery has four separate stages: **recognize Wi-Fi**, **identify the device**, **observe an app request**, and **connect the native app**. A family may support some stages without supporting all of them.

| Family | Device/search mechanism | Switching signal | Important limit |
| --- | --- | --- | --- |
| Seestar | Active `scan_iscope` request/reply; Wi-Fi family hints | Supported Seestar searches | Reply validation and native TCP services must be reachable |
| Vespera | Wi-Fi hints plus TCP service readiness | Connection attempts to original target TCP 8082/8083 | Private-Wi-Fi route is required; target IP alone does not distinguish identical-address devices |
| DWARF | Wi-Fi hints and read-only device handling; passive app-search observation | Recognized DWARF search traffic | No fabricated discovery replies or Wi-Fi credentials; initial IP entry may be necessary |
| ZWO mounts | `AMH_` hints and bounded model query | Native proxy TCP SYN | Specific model/app and guiding still need validation |
| SynScan | Wi-Fi hints and exact version request/reply | Scoped version search on UDP 11880 | A protocol search is not an unsolicited beacon |

![Installed protocol families](../images/plugins.png)

## Why a first search can take time

1. The app searches before the bridge is connected to the requested device.
2. A recognized request can start a guarded Wi-Fi switch.
3. The bridge verifies routing or required services.
4. The app must retry its search or connection after readiness.

Bridge switching time and app refresh time are separate. Delay can include radio association, readiness checks, the quiet/reuse policy and the manufacturer's rescan interval.

## Choosing a particular instrument

Saved SSIDs, optional BSSID binding and validated identity observations help associate a profile with physical equipment. They do not make an original-address TCP attempt uniquely identify a Vespera when multiple devices share the same target address. Ambiguous selection needs explicit choice; same-subnet instruments must be used sequentially.

Use the connection details shown for the selected profile. On private Wi-Fi, original-address access can require a router route. On the hotspot, DHCP supplies device routes. A native relay uses the displayed bridge IP and exact supported ports.

For iOS clients, local-network permission and any required signed multicast entitlement are client-side prerequisites. A bridge cannot grant another app those permissions.
