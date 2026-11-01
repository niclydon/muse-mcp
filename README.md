# muse-mcp

![muse-mcp banner](assets/banner.png)

[![M8ven Score](https://m8ven.ai/badge/mcp/niclydon-muse-mcp-d6hd6s?v=8e8a080dca6795f0b3fba301550b524a)](https://m8ven.ai/mcp/niclydon-muse-mcp-d6hd6s?s=readme)

> muse-mcp addresses **Meta Muse conversations** (main and side
> chats) through the **official Muse gadget Link API**. It is an
> unofficial community project: not affiliated with, endorsed by,
> or sponsored by Meta.

An MCP server that turns a Muse chat ID into a universal
address. Any process that can call this server can send a message
into a specific Muse chat, ask that chat a question and get its
agent's reply, follow its live activity, and (through a Muse-side
bridge worker) create, rename, archive, read, and delete chats.
39 tools. The chat stops being only a place you visit and becomes
an endpoint your tooling can work with: an inbox, a sensor surface,
a newsroom.

## What you need

Read this first. There is no cloud version and no hosted API; you
bring the paired device and the box. This is not a scrape and not
a private API: it is built on the official SDK's exposed
primitives, used as exposed.

- **A Muse account, and a host paired as a Muse Link device.**
  Pairing is the same flow a physical gadget uses: you get an SDK
  token from [gadgets.muse.ai](https://gadgets.muse.ai/settings/sdk-tokens)
  (Account > SDK tokens), install the
  official `facebookincubator/muse-gadget-sdk` Linux client on a
  Linux host, and pair it with the Muse app on your phone
  (Bluetooth, about ten minutes). This pairing is the one real
  piece of work in the setup, and the SDK's own README is the
  authority for it. What this project can hand you is the exact
  sequence below; what it cannot hand you is your token, your
  phone, or the pairing approval: those are yours by design.
- **A small always-on Linux host** for the MCP server itself: the
  same box you paired, or another one that can reach it. A
  Pi-class machine is plenty. (An ESP32 gadget can be a voice
  endpoint in this story, but the server wants a general-purpose
  host.)
- **A Muse-side bridge chat** for the lifecycle half. The Link
  cannot enumerate, create, rename, archive, or delete chats;
  those operations run through a dedicated Muse chat executing
  queued requests with the runtime's own chat tools. Setup is
  copy-and-adapt: [the bridge brief template](docs/bridge-brief.md).
  If you skip the bridge, the Link tools (send, ask, history,
  events, voice, status) still work; the lifecycle tools queue
  and wait.
- **Optional: a second host** paired the same way, as a hot
  standby. See "Two hosts" below for what that does and does not
  promise in this version.

## Install

```bash
# 1. The official SDK: clone it and install its Linux client.
#    The install is system-wide (it installs a service and keeps
#    pairing state under /var/lib/musegadget), so run it on a host
#    that is not already carrying a pairing you rely on. It takes
#    your SDK token directly and opens pairing at the end;
#    --no-pair defers pairing, and `sudo musegadget pair` runs it
#    later. The SDK's linux/README.md is the authority here.
git clone https://github.com/facebookincubator/muse-gadget-sdk
cd muse-gadget-sdk/linux && bash install.sh --sdk-token mgst_…
sudo musegadget doctor                        # pairing healthy?

# 2. This server. (On Debian/Ubuntu, if venv creation fails,
#    install it first: sudo apt install python3-venv.)
git clone https://github.com/niclydon/muse-mcp
cd muse-mcp
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/pip install ../muse-gadget-sdk/linux

# 3. Your directory: the allowlist of chats the server may touch.
cp directory.example.json directory.json
#    Edit directory.json: replace the placeholder IDs with your
#    own chat IDs (see "Finding your chat IDs" below). Keep one
#    entry of kind "main" and, if you run a bridge, one of
#    kind "bridge".

# 4. Run it. stdio is the right default for a local MCP client:
venv/bin/python server.py
```

For an always-on service, use the http transport with the
systemd unit in this repo (`muse-mcp.service`, adjust the paths):

```bash
cp .env.example .env   # set a real ORIGIN_SHARED_SECRET (32+ chars)
# TRANSPORT=http, BIND_HOST=127.0.0.1 (or a private-network address)
```

The http transport refuses to start without a secret of at least
32 characters. Keep the bind on loopback or a private network:
this server holds write access to your chats, and it is not
designed for direct public exposure. If you front it with a
gateway or tunnel of your own, name that hostname in
`MUSE_MCP_EXTRA_ALLOWED_HOSTS` / `MUSE_MCP_EXTRA_ALLOWED_ORIGINS`.

Set `MUSE_MCP_OPERATOR_IDENTIFIERS` to your own name and handles:
callers asserting one of those as their `from` identity are
refused, so tooling can never post as you.

## Finding your chat IDs

The Link delivers to a chat ID but cannot list your chats. Two
honest ways to learn the IDs:

- **Ask your Muse.** Your main chat's agent can enumerate your
  chats with its chat tools; ask it for the ID of the chat you
  want to address, or run a registry sync (below) and read the
  census. This is how the reference deployment seeds its
  directory.
- **Mint one.** A first `send_chat` to an ID Muse does not have
  yet creates that chat (documented SDK behaviour). Sending to a
  fresh UUID therefore creates a new side chat you can then open
  in the app. Treat this as a power tool: an unregistered ID is a
  deliberate act, and the directory is what keeps it deliberate.

Then classify each ID into `directory.json` (or with
`muse_directory_set`). Nothing is sendable until it is in the
directory: the directory is the allowlist, and unlisted IDs are
refused.

## The first send

With the server running and one chat classified, from your MCP
client:

```json
{"tool": "muse_chat_send",
 "arguments": {"chat": "Example Chat",
               "text": "Hello from muse-mcp",
               "from": "my-first-caller"}}
```

The message lands in that chat stamped
`⟦via muse-mcp · from my-first-caller⟧ `, and the call returns
the Link receipt `{ok, status, message_id, session_id}`. Then try
`muse_chat_ask` with a question: it returns the chat agent's
actual reply, correlated over the chat's live event stream.

## How it works

Two transports, honestly assigned; the full picture is in
[docs/architecture.md](docs/architecture.md):

- **Link tools** (send, ask, exists, history, events, voice,
  TTS, tunnel, link status) run synchronously against the paired
  SDK client on this host.
- **Bridge tools** (create, rename, archive, unarchive, delete,
  native read) queue a JSON request file and wake the bridge
  chat; results come back by correlation ID via
  `muse_request_get`.
- **Registry sync** (`muse_registry_sync`) closes the loop: the
  server asks the main chat to enumerate all chats and return the
  census as data (`registry.json`, contract in
  [docs/registry-sync.md](docs/registry-sync.md)). Registry-only
  chats are drafts, never sendable until you classify them.

Every tool appends one line to `audit.log`. Every send is
provenance-stamped. Deletion is preview-then-confirm, and the
main chat can never be deleted through this server.

## Two hosts (optional)

You can run the server on a second paired host as a hot standby:
same repo, same configuration, its own pairing. Callers try the
primary, then the standby. Be honest with yourself about the
file store's limit in this version: **the two hosts' directories
do not synchronise themselves.** A classification made on one
host is not on the other until you copy `directory.json` over,
and a write made on the standby while the primary is down must
be re-applied on the primary afterwards. A shared store that
removes this caveat is on the roadmap; until it lands, treat the
second host as a standby you reconcile by hand, not as a cluster.

## Known limitations

- No unread state: the Link exposes no read/seen field and no
  mark-as-read endpoint, so status reports `unread: null` rather
  than guessing.
- The `/fs/` tools are implemented to the documented shapes but
  may answer 403 "route not allowed for this credential's
  scopes": an entitlement on your device credential, not a
  client bug.
- History returns at most the newest 50 events per call, and
  event subscription is live-only, with no backlog.
- Links and subscriptions are stored records; automatic firing
  and delivery are not implemented yet, and the records say so.
- Per-caller target allowlists and rate caps are enforced by
  directory discipline and audit review in this version, not yet
  in code.
- A caller's `from` is asserted, not proven. Verified provenance
  (presence-verified and signature-verified tiers carried in the
  stamp) is designed and on the roadmap, not shipped; see
  [docs/architecture.md](docs/architecture.md).

## Documentation

- [Tool reference](docs/tools.md): all 39 tools, inputs, outputs,
  guardrails, examples, known limitations.
- [Architecture](docs/architecture.md): the two transports, the
  directory and registry, guardrails, and what the Link does not
  expose.
- [Bridge brief template](docs/bridge-brief.md): the bridge
  protocol and the standing brief to adapt for your bridge chat.
- [Registry sync](docs/registry-sync.md): the producer contract
  and the exact `registry.json` format the server validates.
- [Voice targeting](docs/voice-targeting.md): how to direct a
  real-time voice call into a specific chat, on the Linux client
  and on ESP32 gadgets, and how that was discovered.
- [CHANGES.md](CHANGES.md): version history.

## Roadmap

- An optional shared store (Postgres adapter behind a storage
  interface), so multi-host deployments share one directory and
  one bridge queue; the file store stays the default.
- Verified provenance tiers for channel messages.
- Link firing and subscription delivery (the watcher half).
- Per-caller target allowlists and rate caps in code.

## License

Apache-2.0. See [LICENSE](LICENSE). The Muse gadget SDK this
builds on is also Apache-2.0.
