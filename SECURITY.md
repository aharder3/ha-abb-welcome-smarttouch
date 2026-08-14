# Security

## Sensitive information

Do not publish:

- MyBuildings passwords
- SIP passwords
- private keys
- client certificates containing private installation identities
- Home Assistant secrets
- pairing QR codes
- Portal UUIDs from private installations
- packet captures containing private traffic
- unredacted diagnostics

Before opening an issue, redact private information.

## Physical access

This integration can interact with building intercom equipment and may expose
door-opening functionality.

Door unlocking should be treated as a security-sensitive operation.

Avoid automatically unlocking doors based only on untrusted external events,
presence detection, voice input or unauthenticated network events.

## Reporting security issues

Do not post credentials, certificates, private keys or exploitable private
installation details in a public issue.

If a security problem can be described without publishing sensitive data,
open an issue containing only the minimum information required to reproduce
the problem.

## Disclaimer

This is a community integration and is not a replacement for certified
physical access-control, alarm, emergency or life-safety systems.
