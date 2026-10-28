# CHANGES

## Public preparation (unreleased, on the public-prep branch)

- Repository prepared for its first public release. No tool
  behaviour changes except the configuration moves below; the
  server version remains 0.4.6.
- Added the Apache-2.0 LICENSE the design always specified.
- `directory.json` is operator-local state now: untracked, with
  `directory.example.json` (placeholder IDs) as the starting
  point. Runtime state files (registry, gaps, links,
  subscriptions, bridge queue/results) are gitignored.
- Removed the estate design record and founding narrative from
  the repo (they are the operator's private records); the public
  design overview lives in docs/architecture.md, with new
  docs/bridge-brief.md and docs/registry-sync.md.
- README rewritten for a first-time installer: requirements up
  front, pairing sequence, directory setup, first send, honest
  two-host limits.
- server.py: no deployment-specific values remain in code. The
  gateway hostnames and the bind literal moved to configuration
  (`MUSE_MCP_EXTRA_ALLOWED_HOSTS` / `MUSE_MCP_EXTRA_ALLOWED_ORIGINS`,
  `BIND_HOST`); operator identities refused as `from` are
  configured with `MUSE_MCP_OPERATOR_IDENTIFIERS` (generic
  defaults remain refused).
- Bridge wake dialect is pluggable: `MUSE_MCP_WAKE_FORMAT=plain`
  (new default) or `cant` (the reference deployment's dialect,
  kept as an option). Deployments running the Cant dialect must
  set the variable before deploying this build.
- Example env file is generic (`.env.example`); the systemd unit
  and wait-for-bind script carry no deployment-specific paths.
- docs/tools.md and this file: examples use placeholder chats
  and IDs; deployment receipts generalised.

## 2026-10-28 — 0.4.6: events filter drops global traffic too

- The 0.4.5 verification re-run of T2-14's quiet window
  returned event_count 1: a global `tasks.updated` job
  notification (no session_id) that fired inside the window.
  The bundle exclusion worked; the remaining count was the
  same defect family — non-chat traffic dressed as chat
  events. `muse_chat_events` now also excludes
  `tasks.updated` and `sessions.updated`, completing the
  digest's global-traffic set (node.* was already excluded).
  The tool's docstring already stated tasks.updated must not
  be attributed to the subscribed chat; the code now honours
  it.
- No other changes from 0.4.5. Deployed to both hosts; /health reports 0.4.6 on both hosts.

## 2026-10-28 — 0.4.5: unarchive resolves through the overlay

- Regression caught by the 0.4.4 fix-pass verification re-run:
  the overlay in `_resolve_chat` also blocked
  `muse_chat_unarchive`, which resolves through
  `_resolve_known_chat` — an overlay-archived chat could not be
  unarchived at all. `_resolve_known_chat` now returns a
  directory-known, Bridge-archived chat as a lifecycle target
  (sendable/addressable false), restoring unarchive and read
  resolution. Send paths use `_resolve_chat` directly and stay
  suspended.
- No other changes from 0.4.4. Deployed to both hosts; /health reports 0.4.5 on both hosts.

## 2026-10-28 — 0.4.4: T2-8/T3-12 suspension overlay completed

- The 0.4.2 overlay was applied only inside `_registry_entries`,
  so it attached nowhere for a chat the registry file does not
  enumerate — including the case the fix-pass verification hit:
  the post-archive sync's registry was rejected by the server's
  own validator (producer counts shape) and the last-good
  registry, which predates the chat, was served. The overlay is
  now consulted at the consumption points as well:
  `_resolve_chat` suspends on Bridge-learned state, the list
  payload applies it per directory entry (`archived_source:
  "bridge_result"` when it decides), and `_refuse_unlisted`
  issues the documented suspension refusal from it. The
  documented refusal now fires on the very next send after a
  Bridge archive completes, regardless of registry-file state.
- No other changes from 0.4.3. Deployed to both hosts; /health reports 0.4.4 on both hosts.

## 2026-10-28 — 0.4.3: T2-3 ask-harvest fix completed

- The 0.4.2 pre-send gate raced the subscribe-time bundle:
  `chat_subscribed` opens before the bundle finishes arriving
  (fix-pass probe on a live subscription: the bundle trio
  completed ~75 ms after subscribe-open), so a bundle event
  landing after the send was still fed to the fresh turn and
  finished it vacuously — the first 0.4.2 verification ask
  reproduced the T2-3 failure exactly (ok:false, empty reply,
  task_status "completed"). `_ask_impl` now waits, bounded at
  8 s, for the full bundle trio (agent.status, task.status,
  approvals.snapshot) to arrive before issuing the send, so
  every bundle event is pre-send and gated out. The probe also
  confirmed every task.status is parentless and carries the
  chat's standing task_id across turns, so no identity-based
  filtering of status events is possible; arrival order is the
  only sound discriminator.
- No other changes from 0.4.2. Deployed to both hosts; /health reports 0.4.3 on both hosts.

## 2026-10-28 — 0.4.2: smoke-battery fix pass (SMOKE-20261027-1)

Five defects from the pre-public smoke battery, each fixed and
re-verified by re-running its failed test procedure:

- T0-5: `muse_link_status`, `muse_chat_list`, and `sidechat_list`
  now append audit.log lines in the existing record shape; they
  were the read tools the audit never recorded, so routing
  evidence could not exist.
- T2-3: ask harvest no longer concludes from the previous turn.
  `_ask_impl` never feeds pre-send stream events to the fresh
  MuseTurn: the subscribe-time state bundle's parentless
  terminal task.status marked the new turn finished and the wait
  loop completed vacuously with an empty reply (3 of 5 asks in
  the battery). Completion vocabulary settled: `turn_complete`
  is the canonical completion string; `reply_done_quiet` remains
  the labelled early exit. docs/tools.md aligned.
- T2-8 / T3-12: archived suspension is immediate. Completed
  Bridge archive/unarchive results are learned in
  `muse_request_get` and applied over the registry file's view
  in `_registry_entries` until a synced registry agrees, so the
  documented fail-closed refusal fires on the very next send
  instead of one sync cycle later.
- T2-14: `muse_chat_events` excludes the subscribe-time state
  bundle (first agent.status / task.status / approvals.snapshot,
  the digest's inferred baseline) and node.* global traffic; an
  idle window now honestly returns event_count 0.
- T0-3: docs/tools.md provenance prefix corrected to the
  observed canonical `⟦via muse-mcp · from <from>⟧`.

Deployed to both hosts; /health reports
0.4.2 on both hosts.

## 2026-10-27 — 0.4.1: gateway hostnames in transport security

- Onboarding onto a gateway fleet (a Resource Server worker in
  front of the origin): TRANSPORT_SECURITY now also allows the
  gateway's public and tunnel hostnames, plus their https
  origins. No tool
  behaviour changes; the X-Origin-Secret guard still gates every
  non-/health request on the gateway path, and the shared
  CF-Access service token gates the tunnel hostname itself.
  Deployed to both hosts; /health reports
  0.4.1 on both hosts.

## 2026-10-27 — 0.4.0: status v2, watch digest, Cant bridge wakes

- Tool count: 38 -> 39. Deployed to both hosts; `/health` reports 0.4.0 on both hosts.
- `muse_chat_status` rewritten: last canonical history events
  and derived turn state (history) are returned alongside a
  short live subscription snapshot (activity, task status,
  subagent count, pending approvals) with each source state
  kept separate and a combined `turn_state` that prefers the
  live snapshot. Pending approval count is `null` unless an
  `approvals.snapshot` was actually received; it is never
  defaulted to 0. Registry drafts and registry-archived chats
  return metadata only and make no live Link calls. `unread`
  remains `null`; no output approximates a read state.
- New `muse_watch_digest`: up to five classified chats watched
  in parallel for a bounded window, each digest separating an
  `inferred_baseline` initial snapshot from changes after it,
  with strict per-chat attribution (global stream events are
  never attributed), delta text counted rather than returned,
  and completed reply message IDs listed.
- Bridge wakes are now Cant frames: every queued operation
  wakes the Bridge chat with one unstamped Cant REQ frame
  (ops CRT/REN/ARC/UAR/DEL/RED, `arg=@file:` request pointer,
  corr8 correlation), the Bridge answers with one Cant RES/ERR
  frame, and queue receipts and audit records carry `cant` and
  `cant_corr`. `muse_registry_sync` is unchanged (it addresses
  the main chat and stays plain-English).
- `muse_request_get`: a Bridge result file caught mid-write
  (unparseable) now returns `status:"pending"` with
  `detail:"result being written"` instead of an error, so
  callers poll through the write.
- Acceptance (caller `muse-pass3-acceptance`): the designed
  history/live disagreement reproduced on the first status run
  (history `awaiting_user_next_message` vs live
  `agent_working`); the approvals integer-vs-null pair proven
  with a forced no-snapshot run; a registry draft returned
  metadata only; a 20-second two-chat watch attributed a
  marked send and its reply to the target chat only; a full
  Cant round trip (create wake and Bridge RES both validated
  with the `cant` repository's decoder, matching corr) ran on
  a throwaway chat that was then deleted through the preview +
  delete flow and confirmed gone. Full record in
  `discovery.md` §0.9.

## 2026-10-27 — 0.3.1: renamed to muse-mcp

- The server, repository, service directory, and systemd unit
  are renamed from `sidechat-mcp` to `muse-mcp` on both hosts
  (the old repository URL redirects). The
  `/health` server name, the MCP server name, and the provenance
  prefix stamped on sends (`⟦via muse-mcp · from <from>⟧`)
  carry the new name.
- No behaviour change: all 38 tools, the directory, the
  registry, and the runtime stores carried over unchanged
  (directory revision verified identical across the rename).
  The `sidechat_*` tool aliases remain as compatibility shims,
  and the workspace caller `sidechat-mcp-call` remains as a
  shim over `muse-mcp-call` (`MUSE_MCP_HOST` is the host-force
  variable; `SIDECHAT_MCP_HOST` still works).
- The homelab vault project and secret id keep their original
  `sidechat-mcp` name; they are internal materialization
  plumbing, not a published surface.

## 2026-10-27 — 0.3.0: registry consumption, directory/gap tools, Bridge lifecycle, links/subscriptions, full documentation

- Tool count: 19 -> 38. Every tool is documented in
  `docs/tools.md`, and every MCP docstring now states purpose,
  arguments, returns, and guardrails.
- Registry consumption: list/status now consume `registry.json`
  with validation, last-good retention on an invalid file,
  revision-chain pair state, and distinct `directory_revision`,
  opaque `registry_revision`, and `registry_synced_at` values.
  Drafts are separate from `chats` and non-addressable; a valid
  registry archived flag suspends a directory entry immediately.
- Directory and gaps: `muse_directory_get`,
  `muse_directory_set` (required `base_revision`, stale writes
  refused and audit-logged), `muse_gap_log`, and
  `muse_gap_list` over append-only `gaps.jsonl`.
- Bridge: the Bridge worker chat is in the directory (kind
  `bridge`). Queue/result tools add `muse_request_get`,
  `muse_chat_create`, `muse_chat_rename`, `muse_chat_archive`,
  `muse_chat_unarchive`, and `muse_chat_read` (native turns,
  distinct from Link history). `muse_chat_relay` is Link-side.
- Deletion safety: `muse_chat_delete_preview` issues a
  single-use 10-minute token; `muse_chat_delete` refuses without
  a valid token and Bridge-queues with `confirmed:true`. The
  main chat is refused at both steps.
- Links and subscriptions (storage/CRUD only):
  `muse_link_create`, `muse_link_list`, `muse_link_delete`,
  `muse_chat_subscribe`, `muse_chat_subscriptions`, and
  `muse_chat_unsubscribe` with a 14-day maximum registration.
  Automatic firing/delivery remains watcher work.
- Ask fix: `agent_statuses` now also scans raw `agent.status`
  events because the SDK's shared-chat turn does not emit an
  `AgentStatus` object for unlinked live status events. The
  latest code is also returned as `agent_status`.
- High availability: transport security now derives additional
  allowed hosts/origins from `BIND_HOST`, retaining the existing
  primary literals; a standby on another tailnet address no
  longer rejects its own Host header with HTTP 421. The systemd
  unit description is host-generic. The service runs on two
  hosts (primary and hot standby); the caller fails over
  automatically, with a host-force override. Failover acceptance
  passed on October 27, 2026 with the primary stopped: a marked
  send and an ask round trip completed through the standby.
- Documentation corrections: history is canonical transcript
  events only; live event names belong to subscriptions;
  `tasks.updated` has no `session_id`; no read/seen field exists
  in either surface and `display_text_ready` is not a read
  indicator; FS tools are implemented to spec but blocked on
  Meta backend credential entitlement (HTTP 403, exact error
  "route not allowed for this credential's scopes").
- Live acceptance (2026-10-27): 38 tools on both hosts; draft
  send refused with the classification error, then the same
  draft classified and a marked send landed; gap round trip;
  full Bridge lifecycle on a throwaway chat (create, brief-bound
  ask, relay, rename, archive, unarchive, archive,
  preview-gated delete, existence then `false`); negatives
  passed (stale `base_revision`, delete without token, delete
  of the main chat).

## 2026-10-27 — 0.2.0: MVP expansion, 19 tools live

- Refactor: one shared `_link_session()` async context manager
  builds every ephemeral LinkSession (was send-only inline code).
- New tools: `muse_chat_list` (alias), `muse_chat_send` (alias),
  `muse_link_status`, `muse_chat_exists` (+ `sidechat_exists`),
  `muse_chat_ask` (+ `sidechat_ask`; SDK `MuseTurn` +
  `subscribe_chat` turn-following, with a labelled
  `reply_done_quiet` early completion because `MuseTurn.complete()`
  waits for the whole agentic task to go terminal), 
  `muse_chat_history` (session_id scoping verified live),
  `muse_chat_events`, `muse_chat_send_voice`, `muse_voice_tts`
  (verified live: 48,384-byte MP3 for a voice-modality reply),
  `muse_link_request` (path-allowlisted tunnel), `muse_fs_read`,
  `muse_fs_write`, `muse_registry_sync` (+
  `sidechat_registry_update`), `muse_chat_status`.
  `sidechat_send` gains `output_modality` (text|voice).
- Guardrails extended to all tools: directory allowlist on every
  chat-targeting tool, `from` required everywhere,
  operator-identity `from` values refused, fs_write credential-path
  refusals, every tool audit-logged.
- Bug fix (latent since v1): the origin guard's
  `hmac.compare_digest` compared bytes header vs str secret and
  500'd every guarded request; the secret is now encoded once at
  guard construction.
- Live acceptance (2026-10-27): link_status paired; existence
  check true; a marked send landed; an ask returned the target
  chat's reply through the turn follower (re-check in ~18s);
  history bare and chat-scoped both 200; an events window
  captured 19 events incl. agent.status/task.status/deltas
  during a concurrent send; send_voice receipt ok;
  voice-modality send + TTS round trip ok; registry_sync landed
  in the main chat and the main chat returned registry.json
  (125 enumerated). Negatives: unlisted chat refused,
  operator-identity `from` refused, off-allowlist tunnel path
  refused, fs_write to .ssh refused.
- Known limits, stated plainly: `/fs/read` and `/fs/write` answer
  403 "route not allowed for this credential's scopes" on this VM
  (tools implemented; scope not enabled for the device token).
  `muse_chat_ask` returns agent_statuses empty so far (raw
  task.status events are captured; the SDK turn has not surfaced
  AgentStatus objects for these chats yet). The server does not
  yet consume registry.json automatically.

## 2026-10-27 — v1: list + send, first deployment

- New service `sidechat-mcp`, streamable HTTP with the
  x-origin-secret guard.
- Tools: `sidechat_list` (directory = allowlist, read at call time)
  and `sidechat_send` (provenance-stamped, audit-logged send via an
  ephemeral musegadget `LinkSession`, `register_device=False`,
  riding the host's paired Link client).
- The env var `ORIGIN_SHARED_SECRET` is provided from the
  deployment's secret store into the 0600 `.env` at deploy time;
  never committed.
- Unit runs as root because the musegadget pairing store
  (/var/lib/musegadget) is root-owned mode 0600; the store is
  read-only to this service. Restart-policy drop-in added.
- Directory seeded with the main chat and two side chats.
- Acceptance (2026-10-27): unit active; tools/list returns both
  tools; list returns the three seeds; one real marked send
  landed; an unlisted chat_id is refused.
