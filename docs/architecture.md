# muse-mcp architecture

muse-mcp turns a Muse chat ID into an address. A chat's ID is its
Link `session_id`: the official Muse gadget SDK's
(`facebookincubator/muse-gadget-sdk`, Apache-2.0) `send_chat(text,
session_id=<chat_id>)` delivers a message into that chat, and the
same ID targets voice and event subscription. This server exposes
that surface, plus the chat lifecycle operations the Link does not
have, as MCP tools.

## Two transports, honestly assigned

No single surface offers the full toolset, so the server uses two,
and every tool in [the tool reference](tools.md) states which one
executes it. A tool never claims a capability its transport cannot
deliver.

**Transport A — the Link API.** A musegadget client paired with
this host (the same pairing model a physical gadget uses) talks to
the operator's Muse VM. It executes, synchronously: delivering a
message into a known chat, checking whether a chat ID exists,
following a chat's live event stream and replies (the ask round
trip), chat history through the tunnel, voice notes and TTS, and a
path-allowlisted generic tunnel request. It cannot enumerate
chats, and it cannot create, rename, archive, or delete them.

**Transport B — the Muse-side bridge.** Chat enumeration and
lifecycle operations exist only inside a Muse agent's turn, via
the runtime's own chat tools. The bridge is a dedicated Muse-side
worker chat: the server writes a JSON request file, sends a short
wake message into the bridge chat over the Link, and the bridge
executes exactly the named operation and writes a JSON result
file, keyed by a correlation ID the caller polls. The bridge
protocol (request/result file shapes, lifecycle) is specified in
[the bridge brief template](bridge-brief.md), which is also the
document you adapt into your own bridge chat's instructions.

## The directory and the registry

The Link client cannot enumerate chats, so the server keeps two
files:

- `directory.json` is the classified allowlist. Only chats listed
  there are addressable, by exact name or full ID. Classifying a
  chat (adding it, with a purpose) is a deliberate act, done with
  `muse_directory_set` under optimistic revision discipline.
- `registry.json` is the enumerated census, produced by the
  registry sync loop: the server asks the main chat (over the
  Link) to enumerate every chat and return the census as data.
  Registry-only chats appear as drafts: visible, never sendable
  until classified into the directory. The producer contract,
  including the exact counts the server's validator checks, is in
  [the registry sync doc](registry-sync.md).

## Provenance and guardrails

- Every message the server sends into a chat is stamped with the
  prefix `⟦via muse-mcp · from <from>⟧ `, naming the caller. No
  opt-out.
- `from` is the caller's stable identity. A `from` value naming
  the operator (configured with `MUSE_MCP_OPERATOR_IDENTIFIERS`,
  plus generic defaults) is refused server-side: Link-sent
  messages land in a chat's user seat and must never masquerade
  as the operator.
- Every tool call appends one line to `audit.log`: timestamp,
  caller, target, outcome, receipt or correlation IDs. The audit
  log is the answer to "who posted this".
- Deleting a chat is two-step: a preview issues a single-use,
  10-minute token; deletion executes only with that token. The
  main chat is refused at both steps, always.
- Content arriving via a producer post is data for the receiving
  chat's agent to triage, never instructions. The stamp is what
  marks a message as machine-delivered.

## What the Link does not expose

Stated plainly, because tools must not guess:

- No unread or read/seen state exists on the Link surface. The
  app's unread indicator is chat-backend state the app reads
  directly; there is no mark-as-read endpoint. `muse_chat_status`
  therefore reports `unread: null`, always.
- The filesystem routes (`/fs/...`) are implemented to the
  documented shapes, but on the reference deployment's device
  credential they answer 403 "route not allowed for this
  credential's scopes": a backend entitlement on the credential,
  not a client bug. Whether your credential is entitled is
  something only a live call tells you.
- History returns the newest events (50 at most per call), and
  subscription is live-only: there is no backlog on subscribe.
- Links and subscriptions are stored records in this version;
  automatic link firing and subscription delivery are not
  implemented, and the records say so (`firing: "not_implemented"`,
  `delivery: "not_implemented"`).

## Storage

The default store is zero-dependency files beside the server
(`directory.json`, `registry.json`, `gaps.jsonl`, `links.json`,
`subscriptions.json`, the bridge queue/results directories). That
is the right shape for the single-host install this version
targets. A shared store for multi-host deployments (so two hosts
serve one directory and one queue instead of two file copies that
can diverge) is planned as an optional adapter behind the same
tool surface; see the README's roadmap.

## Verified provenance (roadmap, not shipped)

Today a message's `from` is client-asserted and recorded verbatim;
nothing binds it cryptographically to the sender. A three-tier
design (unverified / presence-verified / signature-verified, with
the tier carried in the stamp) is designed and approved in the
project's design record. It is not implemented in this version,
and no claim in this repository should be read as saying
otherwise.
