# Contributing to Astro Bridge Core

## Choose the right repository

- Application, UI/API, protocol adapters and hardware JSON plugins: **this repository**.
- Raspberry images, host services, containers and installation: [implementations](https://github.com/Sqound/astro-bridge-implementations).

## Hardware support

Start with the [plugin tutorial](doc/plugins/README.md). Add a manifest using existing adapters, or propose a small reviewed adapter if the protocol is new. Include a protocol source, sanitized fixtures and the [hardware test matrix](doc/contributing/hardware-validation.md). Start untested hardware at `unverified`.

## Pull request checklist

- Keep source, comments, documentation and PR text in English. Localized UI/notification catalogs remain localized data.
- Preserve credentials, saved-profile IDs, API compatibility and connection protection.
- Do not include personal names, private conversations, network identifiers, tokens, device captures or private keys.
- Rebuild the catalog and OpenAPI after a plugin change.
- Run the relevant Python/Node/browser or isolated Linux tests. State what was actually tested.
- Keep changes focused and explain the problem, resulting behavior and remaining limitations.

```sh
python3 -m unittest discover -s tests
node --test tests/*.test.mjs
python3 tools/build-plugins.py --check
python3 tools/build-i18n.py --check
python3 scripts/generate_openapi.py
```

Contributions are made under the project's [MIT License](LICENSE). Keep third-party notices and use redistribution-compatible assets.
