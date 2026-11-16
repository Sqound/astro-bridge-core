# Third-party notices

Astro Bridge's original source, documentation and demonstration screenshots are distributed under the [MIT License](LICENSE). This does not relicense dependencies or operating-system components.

| Component | Use | Upstream license |
| --- | --- | --- |
| py-vapid | Optional Web Push signing | MPL-2.0 |
| http-ece | Optional Web Push encryption | MIT |
| cryptography | Optional cryptographic primitives | Apache-2.0 OR BSD-3-Clause |
| Playwright | Optional development/browser tooling | Apache-2.0 |

The core wheel does not bundle these optional libraries. Installations and containers must retain the notices supplied by their dependencies. See the [implementation notices](https://github.com/Sqound/astro-bridge-implementations/blob/main/THIRD_PARTY_NOTICES.md) for bundled Linux/firmware/Netfilter components.

Manufacturer/product names are used for identification. Third-party trademarks remain with their respective holders.

ASCOM Alpaca interoperability follows the [official Alpaca API specification](https://ascom-standards.org/api/) and [ASCOM standards documentation](https://ascom-standards.org/). Specification references do not confer ASCOM certification; official conformance and physical-device acceptance are separate gates.

No vendor SDK binaries, manufacturer packages or authentication keys are included.
ZWO and QHY libraries retain their vendor terms and require separate owner review
and installation. The owner-only installer does not grant redistribution rights.
The optional INDI service uses separately installed INDI packages and their own
licenses; no INDI executable, driver or bubblewrap binary is bundled.
