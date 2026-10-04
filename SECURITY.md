# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 1.x | ✅ |

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Use GitHub's private reporting instead: **Security → Report a vulnerability** on this repository.

Include what you found, how to reproduce it, and what an attacker could do with it. You'll get a reply within a week. Once it's fixed, you'll be credited in the changelog unless you'd rather not be.

## Security model

sysdash is meant to be reachable only by the person sitting at the machine.

- The server binds to `127.0.0.1` and refuses requests whose `Host` header isn't localhost.
- Every state-changing request (`POST`) needs a random token generated at startup and embedded in the page. Requests from other origins are refused.
- The Claude integration runs the `claude` CLI in `dontAsk` mode with a fixed allowlist of read-only commands and paths, and only when the user clicks *scan* or sends a chat message.

Issues that break any of these guarantees are in scope.
