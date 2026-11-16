# Security policy

## Supported versions

Security fixes target the latest release of Astro Bridge Core and Astro Bridge Implementations. Older releases are not patched; upgrade to the latest release.

## Report a vulnerability

Please report vulnerabilities privately through GitHub: open the repository's **Security** tab and select **Report a vulnerability**. Do not open a public issue for an unfixed vulnerability.

Include the affected version, platform (board, OS, Docker or Setup), reproduction steps and the impact you observed. Remove passwords, access codes, tokens, Wi-Fi names, device serial numbers, IP addresses of real networks and private keys from logs and screenshots.

You should receive an acknowledgement within 14 days. We will agree on a disclosure date once a fix or mitigation is available.

## Scope

In scope: the web application and API, pairing and tokens, TLS and Web Push handling, network and firewall changes made by the host integration, the OTA updater, the SD image recipes, the Docker installer and Astro Bridge Setup (download verification, media selection and writing).

Out of scope: vulnerabilities in telescope or mount firmware and manufacturer apps, in operating-system packages (report them upstream), and attacks that require physical access to an unlocked host.

Astro Bridge controls host networking. Run it on a dedicated, trusted device, keep the initial password changed after first use, and do not expose its web interface to the Internet.
