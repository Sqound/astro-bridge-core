# Validation scope

[Documentation](../README.md) / Validation

Validation levels describe specific evidence. They are not interchangeable.

| Level | Establishes | Does not establish |
| --- | --- | --- |
| Unit / browser simulation | Parsing, policy, UI and persistence contracts | Physical radios or native apps |
| Isolated Linux packets | Real kernel routing/NAT, counters and bounded protocol behavior | A particular adapter, mount or phone |
| Physical device test | Named model/firmware/app and the checks performed | Every model in that family |
| Application-only OTA | Update transaction and restored service/configuration on an existing installation | Booting a newly assembled SD image |

The core has passed its Python/JavaScript suites, browser scenarios, fresh wheel installation and installed-core Linux packet tests. A 1.0.0 application-only upgrade on an existing Pi Zero W preserved settings, SSH/TLS identity and Web Push subscriptions; the HTTPS interface and exact assets were verified after restoration.

Fresh-image boot, complete native/Docker host-radio integration, specific mount guiding and untested model/app combinations remain separate acceptance tasks. The plugin inventory reports **declared test coverage**; inspect the named models and limitations rather than treating a family label as universal support.

The [implementation validation matrix](https://github.com/Sqound/astro-bridge-implementations/blob/main/doc/reference/validation.md) records platform scope. See [hardware contribution checks](../contributing/hardware-validation.md) for a repeatable test sequence.
