# Security

muse-mcp holds write access to the operator's Muse chats. Treat
a running instance the way you would treat a logged-in session.

## Posture built into the server

- The directory (`directory.json`) is the allowlist: unlisted
  chat IDs are refused by every chat-targeting tool.
- Callers asserting the operator's own identity as `from`
  (configured via `MUSE_MCP_OPERATOR_IDENTIFIERS`, plus generic
  defaults) are refused, so tooling cannot post as the operator.
- The http transport refuses to start without an
  `ORIGIN_SHARED_SECRET` of at least 32 characters, and every
  non-`/health` request must present it.
- Deleting a chat requires a single-use preview token, and the
  main chat can never be deleted through this server.
- No secret value (pairing token, VM auth token, origin secret)
  is returned by any tool or written to the audit log.

## Your half

- Bind the http transport to loopback or a private network only.
  If you need remote access, put your own authenticated gateway
  or tunnel in front of it; do not expose the port directly.
- Keep the musegadget pairing store (`/var/lib/musegadget`)
  root-only. Whoever reads it holds your Link identity.
- Review `audit.log`: it records every call, caller, target, and
  outcome.

## Reporting

Report vulnerabilities through GitHub's private vulnerability
reporting on this repository. Please do not open a public issue
for a security problem.
