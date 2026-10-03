# Public documentation repository

## Purpose

Document how the Raymarine Element 7 works, which interfaces it exposes, and how readers can consume its data. The established baseline is Element 7 S / E70531, application 3.13.332, bundle 3.13.76, platform 3.13.1. Do not generalize observations to untested models or firmware.

## Source and privacy

The separate `raymarine-element` project is the private research source. Read it when available, but never copy its history, raw evidence or installation-specific material wholesale. Its connection details and device-action authorizations do not become public repository content or authorize new device operations here.

Preserve protocol details, firmware versions, product codes, generic paths, units and failure semantics. Exclude serials (including encoded/hex/binary forms), MACs, real IP addresses, hostnames, SSIDs, credentials, key material, vessel/owner details, positions, routes, tracks, logs and captured live values. Replace identity-bearing path components with explicit placeholders. Use genuinely synthetic examples, labelled as such; do not merely round real coordinates.

Keep raw/private material outside this repository or in ignored local directories. Ignore rules are a guardrail, not a sanitizer: inspect the complete diff before staging or publishing. Do not link to unavailable private paths from public guides.

## Evidence quality

Distinguish observed exchanges, static firmware analysis, proposed consumer policies and unknowns. Retain private experiment IDs as provenance labels and summarize the supporting method publicly. Never imply private fixtures are shipped here. Prefer the latest corrected conclusion, especially GPS date epoch, product/application identity and RayDB signedness.

## Validation

Check relative Markdown links, inspect changes for identifiers and secrets, and syntax-check Python examples where present. Run `python3 -m unittest discover -s tests -v` for PoC changes; all fixtures must be synthetic and tests must run without device access. Keep PoC usage documentation in README.md. Documentation-only changes need no software test framework. Do not contact hardware or perform control writes merely to validate prose. Do not publish or push unless requested.
