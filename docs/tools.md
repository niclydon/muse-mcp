# muse-mcp tool reference

Server version: 0.4.6  
Repository/service name: `muse-mcp` (renamed from `sidechat-mcp` in 0.3.1)  
Tools documented: 39

This is the complete reference for every tool exposed by the server. A tool is not considered shipped unless it appears here.

## How to read this reference

### Transports

- **Local**: the operation reads or writes server-side state such as `directory.json`, `registry.json`, `gaps.jsonl`, `links.json`, subscriptions, audit state, or the Bridge queue.
- **Link**: the operation uses the Muse gadget Link client paired with this host. Link operations can deliver into a known chat, inspect an existing chat ID, follow live events, retrieve TTS, or tunnel a restricted HTTP request.
- **Bridge**: the server queues a JSON request for the Muse-side `🌉 muse-mcp — Bridge` chat. The Bridge executes the Muse-native operation with chat tools, writes a result file, and the caller retrieves it with `muse_request_get`.

### Caller identity and provenance

Every acting or chat-reading tool requires `from`, except the explicitly read-only local listing tools documented below. A `from` value naming the operator (the operator's own configured identifiers) is server-refused.

Messages sent by the server carry the provenance prefix:

```text
⟦via muse-mcp · from <from>⟧
```

No tool in this reference posts as the operator, reacts for the operator, or edits another chat's memory.

### Directory, registry, and authorization

Three related values are intentionally distinct:

- `directory_revision`: SHA-256 of the server's `directory.json` file.
- `registry_revision`: an opaque revision minted by the registry producer. It is **not** the SHA-256 of `registry.json`.
- `registry_synced_at`: the producer's synchronization timestamp.

The registry's `previous_revision` is the directory revision from which its opaque registry revision was produced. List and status outputs report the pair state as `current`, `directory_ahead`, `registry_ahead_or_drift`, or `invalid`.

Only entries in `directory.json` are classified and addressable. Registry-only entries are returned in a separate `drafts` array with `addressable:false`. A draft becomes sendable only when `muse_directory_set` classifies it into the directory.

If a valid registry marks a directory entry as archived, that entry is suspended from chat-targeting tools immediately. Authorization fails closed.

An invalid registry file does not replace the last valid registry held in memory and does not change authorization by itself. A missing, invalid, or stale registry is reported in `registry_status`; directory-only operation continues where the operation does not depend on registry data.

### Error shape

Most failures return:

```json
{"ok": false, "error": "human-readable refusal or failure"}
```

Bridge operations are asynchronous. A successful queue operation returns a `correlation_id`; its final result must be read with `muse_request_get`.

### Audit

Acting tools and chat-reading tools write JSON lines to the server audit log. Audit records include the tool, caller, target where applicable, outcome, and receipt or correlation IDs. Tool-specific audit fields are called out where they matter.

---

## Directory and registry tools

### `sidechat_list`

List the classified chat directory and, separately, chats enumerated by the registry that have not been classified. Directory entries and registry drafts are never mixed into one addressable set.

**Transport:** Local

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `include_archived` | boolean | No | Include archived registry drafts in `drafts`. Defaults to `false`. Directory entries are always shown, with registry-archived entries marked non-addressable. |

**Output:** `{chats, drafts, count, total_count, directory_count, draft_count, archived_draft_count, include_archived, revision, directory_revision, updated, registry_status, registry_revision, registry_synced_at}`. `chats` contains directory entries only. Drafts carry `addressable:false`, `sendable:false`, and `registration_status:"enumerated but not classified/registered"`.

**Guardrails:** Listing a draft does not authorize sending to it. A directory entry marked archived in a valid registry has `addressable:false` and is refused by chat-targeting tools.

**Example call:**

```json
{"tool":"sidechat_list","arguments":{"include_archived":false}}
```

**Example output:**

```json
{"chats":[{"name":"Example Chat","chat_id":"<id>","registered":true,"sendable":true,"addressable":true,"source":"directory"}],"drafts":[{"name":"Example draft","chat_id":"<draft-chat-id>","registered":false,"sendable":false,"addressable":false,"archived":false,"source":"registry"}],"count":1,"draft_count":1,"registry_status":{"state":"ok","pair_state":"current"}}
```

**Known limitations:** Registry freshness depends on the last successful `muse_registry_sync` data return. A stale registry is still shown with `registry_status.state:"stale"`; it is not silently presented as current.

### `muse_chat_list`

List the classified chat directory and, separately, registry drafts that have not been classified. This is the primary `muse_` name for the same merged-but-separated view provided by `sidechat_list`.

**Transport:** Local

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `include_archived` | boolean | No | Include archived registry drafts in `drafts`. Defaults to `false`. |

**Output:** Identical to `sidechat_list`: directory-only `chats`, separate `drafts`, counts, `directory_revision`, opaque `registry_revision`, `registry_synced_at`, and `registry_status` including `pair_state`.

**Guardrails:** Drafts are visible for classification work but are not addressable. Classified directory entries suspended by a valid registry archived flag are also non-addressable.

**Example call:**

```json
{"tool":"muse_chat_list","arguments":{}}
```

**Example output:**

```json
{"chats":[{"name":"🌉 muse-mcp — Bridge","chat_id":"<id>","kind":"bridge","registered":true,"sendable":true,"addressable":true,"source":"directory"}],"drafts":[],"count":7,"directory_count":7,"draft_count":0}
```

**Known limitations:** This tool reports the server's current directory and last loaded registry. It does not enumerate Muse chats itself; enumeration is the registry sync producer's job.

### `muse_directory_get`

Read either the whole classified directory or one directory/registry-known entry. It is the inspection companion to `muse_directory_set`.

**Transport:** Local

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | No | Directory/registry chat ID or exact chat name. Omit it to return all directory entries. |

**Output:** Without `chat`: `{ok:true, entries, revision, directory_revision, registry_status}`. With `chat`: `{ok:true, entry, in_directory, sendable, revision, ...}`. A registry-only result includes `addressable:false` and `registration_status:"enumerated but not classified/registered"`. Unknown chats return `ok:false`.

**Guardrails:** Exact name or full chat ID only; an 8-character prefix is not authorization. Read-only: this tool never classifies or modifies an entry.

**Example call:**

```json
{"tool":"muse_directory_get","arguments":{"chat":"Example Chat"}}
```

**Example output:**

```json
{"ok":true,"entry":{"name":"Example Chat","chat_id":"<id>","kind":"side"},"in_directory":true,"sendable":true}
```

**Known limitations:** Registry-only entries can be inspected here for classification, but inspection does not make them sendable. Registry data can be stale; check `registry_status` and `registry_synced_at`.

### `muse_directory_set`

Create or update a classified directory entry, including classifying a registry draft so it becomes sendable. Writes use optimistic revision discipline: the caller must name the directory revision it read, and a stale revision is refused.

**Transport:** Local

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat_id` | string | Yes | Full chat ID to classify or update. |
| `base_revision` | string | Yes | `directory_revision` from a current `muse_directory_get`, list, or status read. A mismatch is refused. |
| `name` | string | No | Directory display name. Existing or registry-draft value is retained when omitted. |
| `purpose` | string | No | What the chat is for. Existing or registry-draft value is retained when omitted. |
| `kind` | string | No | Entry kind, for example `main`, `side`, or `bridge`. Existing or registry-draft value is retained when omitted. |
| `pattern` | string | No | Operating pattern, such as inbox, sensor, newsroom, room, lane, or session. |
| `escalation_bar` | string | No | Written condition under which the chat may escalate. |
| `producers` | array of strings | No | Producer identities allowed or expected to write to the chat. |
| `cadence` | string | No | Expected delivery or review cadence. |
| `classified_by` | string | No | Human or agent identity recorded as classifier; defaults to `from`. |
| `from` | string | Yes | Caller identity. Impersonation values are refused. |

**Output:** `{ok:true, entry, revision}`, where `revision` is the new directory SHA-256. The stored entry also receives `classified_by` and `classified_at`.

**Guardrails:** Every successful write and every stale-revision attempt is audit-logged. At least a name, purpose, or kind must be available from the supplied fields, existing entry, or registry draft. This is the only tool in this reference that turns a draft into a sendable directory chat.

**Example call:**

```json
{"tool":"muse_directory_set","arguments":{"chat_id":"<draft-chat-id>","base_revision":"<directory-revision>","name":"Example classified chat","purpose":"Acceptance and classification tests","kind":"side","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"entry":{"chat_id":"<draft-chat-id>","name":"Example classified chat","purpose":"Acceptance and classification tests","kind":"side","classified_by":"nova"},"revision":"<new-directory-revision>"}
```

**Known limitations:** The system of record in this version is the server's `directory.json`; a shared-store registry is future work. Concurrent writers must re-read after a stale-revision refusal rather than retrying blindly.

### `muse_gap_log`

Append a capability gap to the server's append-only `gaps.jsonl`. It is the muse-mcp mirror of the estate's gap-log pattern: record what was needed, what was missing, and any known resolution.

**Transport:** Local

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `needed` | string | Yes | Capability or behaviour the caller needed. |
| `missing` | string | Yes | What was absent, blocked, or insufficient. |
| `resolution` | string | No | Known workaround, owner, or proposed resolution. Stored as `null` when omitted. |
| `from` | string | Yes | Caller identity. Impersonation values are refused. |

**Output:** `{ok:true, gap_id, gap}`, where `gap` contains a server timestamp and all supplied fields.

**Guardrails:** Append-only through this tool; there is no edit or delete tool. Empty `needed` or `missing` values are refused. Logging is audit-logged.

**Example call:**

```json
{"tool":"muse_gap_log","arguments":{"needed":"A native Muse chat enumeration API","missing":"The Link API cannot enumerate chats","resolution":"Registry sync through the main chat","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"gap_id":"<gap-id>","gap":{"needed":"A native Muse chat enumeration API","missing":"The Link API cannot enumerate chats","resolution":"Registry sync through the main chat","from":"example-caller"}}
```

**Known limitations:** This is a local server file in this version, not a future shared gap store. Search and triage are by file inspection or `muse_gap_list` recency only.

### `muse_gap_list`

Read the newest entries from `gaps.jsonl`. Use it to verify a gap-log round trip or review recent missing capabilities.

**Transport:** Local

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `limit` | integer | No | Number of newest records to return. Defaults to 20 and clamps to 1..100. |

**Output:** `{ok:true, gaps, count, total}` in newest-first order. `count` is the number returned; `total` is the full number stored. A missing gaps file is a successful empty result, not an error.

**Guardrails:** Read-only. Malformed individual JSONL lines are skipped rather than making the whole list unavailable.

**Example call:**

```json
{"tool":"muse_gap_list","arguments":{"limit":5}}
```

**Example output:**

```json
{"ok":true,"gaps":[{"gap_id":"<gap-id>","needed":"A native Muse chat enumeration API","missing":"The Link API cannot enumerate chats"}],"count":1}
```

**Known limitations:** There is no full-text search, date filter, or pagination cursor. Large gap histories should be read from the file or a future shared registry surface.


---

## Conversation and Link tools

### `sidechat_send`

Send a provenance-stamped text message into one classified chat through the Link. It is the fire-and-forget delivery primitive used by producers and by higher-level tools.

**Transport:** Link

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | Yes | Directory chat ID or exact name. |
| `text` | string | Yes | Message body to send after the provenance prefix. |
| `output_modality` | string | No | Optional Link output modality. Use `"voice"` when the reply should be TTS-eligible. |
| `from` | string | Yes | Caller identity. Impersonation values are refused. |

**Output:** Link receipt `{ok, status, message_id, session_id}`.

**Guardrails:** Directory allowlist only. Registry drafts return the classification refusal; registry-archived directory entries return a suspension refusal. Every attempt is audit-logged.

**Example call:**

```json
{"tool":"sidechat_send","arguments":{"chat":"Example Chat","text":"Marker SCMCP-EXAMPLE: hello from the tool reference.","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"status":200,"message_id":"<message-id>","session_id":"<id>"}
```

**Known limitations:** A receipt proves the Link accepted the message; it does not prove the chat replied. Use `muse_chat_ask` when a reply is required.

### `muse_chat_send`

Send a provenance-stamped text message into one classified chat through the Link. This is the primary `muse_` name and shares its implementation with `sidechat_send`.

**Transport:** Link

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | Yes | Directory chat ID or exact name. |
| `text` | string | Yes | Message body to send after the provenance prefix. |
| `output_modality` | string | No | Optional Link output modality; `"voice"` makes the reply TTS-eligible. |
| `from` | string | Yes | Caller identity. Impersonation values are refused. |

**Output:** Link receipt `{ok, status, message_id, session_id}`.

**Guardrails:** Identical to `sidechat_send`: classified, non-suspended directory chats only; drafts are refused with the classification path; every attempt is audit-logged.

**Example call:**

```json
{"tool":"muse_chat_send","arguments":{"chat":"Example Chat B","text":"Marker SCMCP-EXAMPLE: delivery test.","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"status":200,"message_id":"<message-id>","session_id":"<id>"}
```

**Known limitations:** Delivery and reply are separate facts. This tool returns only the send receipt.

### `muse_link_status`

Report the health of this server's Link path and paired state without exposing secrets. It also reports the directory revision currently being served.

**Transport:** Link / local

| Input | Type | Required | Meaning |
|---|---|---:|---|
| None | — | — | This tool takes no inputs. |

**Output:** `{server, version, host, paired, node_id, sdk_version, status, vms, vm_error, directory_count, directory_revision, directory_updated}`. `paired` is boolean only; no token is returned.

**Guardrails:** Read-only. Secret values are never included. VM status comes from the Link API's VM surface, not from Muse chat state.

**Example call:**

```json
{"tool":"muse_link_status","arguments":{}}
```

**Example output:**

```json
{"server":"muse-mcp","version":"0.3.1","paired":true,"node_id":"<node-id>","directory_count":6,"vms":[{"vm_name":"Muse VM","is_default":true}]}
```

**Known limitations:** A paired Link and reachable VM do not prove that a particular chat exists or will reply. Use `muse_chat_exists` and `muse_chat_ask` for those separate facts.

### `muse_chat_exists`

Check whether Muse has a chat corresponding to a classified directory entry. The check uses the Link `open_side_chat` endpoint.

**Transport:** Link

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | Yes | Directory chat ID or exact name. |
| `from` | string | Yes | Caller identity. Impersonation values are refused. |

**Output:** `{ok:true, chat, chat_id, exists}` on a completed check, or `{ok:false, error}`.

**Guardrails:** Directory allowlist only; drafts and registry-archived entries are refused. The result is existence only.

**Example call:**

```json
{"tool":"muse_chat_exists","arguments":{"chat":"Example Chat","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"chat":"Example Chat","chat_id":"<id>","exists":true}
```

**Known limitations:** The Link existence response carries no chat title, metadata, message count, activity, or unread state. A deleted or never-created ID can only be reported as not existing; the API does not distinguish those histories here.

### `sidechat_exists`

Check whether Muse has a chat corresponding to a classified directory entry. This alias shares the Link existence implementation with `muse_chat_exists`.

**Transport:** Link

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | Yes | Directory chat ID or exact name. |
| `from` | string | Yes | Caller identity. Impersonation values are refused. |

**Output:** Identical to `muse_chat_exists`: `{ok, chat, chat_id, exists}` or an error object.

**Guardrails:** Identical directory, draft, archived-suspension, caller, and audit rules to `muse_chat_exists`.

**Example call:**

```json
{"tool":"sidechat_exists","arguments":{"chat":"⚙️ Cole — Agent Brain","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"chat":"⚙️ Cole — Agent Brain","chat_id":"<id>","exists":true}
```

**Known limitations:** Existence is a single boolean from the Link surface. It is not a chat-status composite.

### `muse_chat_ask`

Ask a classified chat a question and wait for its reply. The server creates a turn follower before sending, so a reply from an earlier turn cannot be mistaken for the answer.

**Transport:** Link

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | Yes | Directory chat ID or exact name. |
| `text` | string | Yes | Question or message text. |
| `timeout_s` | integer | No | Wait budget in seconds. Defaults to 180 and is clamped by the implementation to 10..300. |
| `from` | string | Yes | Caller identity. Impersonation values are refused. |

**Output:** `{ok, chat, chat_id, correlation_id, send_message_id, reply_text, reply_message_id, agent_statuses, agent_status, task_status, timed_out, completion, ack_error}`.

**Guardrails:** Directory allowlist only. Only assistant messages arriving after this call count. The canonical completion mode is `turn_complete`: the turn's task reached a terminal status with its reply. The alternate mode is `reply_done_quiet`, a labelled early exit: the reply completed and the turn then stayed quiet for 8 seconds, used because a chat may continue agentic work after its visible answer.

**Example call:**

```json
{"tool":"muse_chat_ask","arguments":{"chat":"Example Chat","text":"Reply with exactly: SCMCP-EXAMPLE","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"chat":"Example Chat","correlation_id":"<send-message-id>","reply_text":"SCMCP-EXAMPLE","agent_statuses":["online","working"],"agent_status":"working","completion":"turn_complete","timed_out":false}
```

**Known limitations:** The SDK's shared-chat `MuseTurn` deliberately does not emit an `AgentStatus` object for a live `agent.status` event that carries `session_id` but no message/parent linkage. Version 0.3.0 therefore scans the raw events and appends unique `activity_code` values in first-observed order, mirroring the existing raw `task.status` fallback. A timeout returns the state honestly rather than inventing a reply.

### `sidechat_ask`

Ask a classified chat a question and wait for its reply. This alias shares the complete turn-following implementation with `muse_chat_ask`.

**Transport:** Link

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | Yes | Directory chat ID or exact name. |
| `text` | string | Yes | Question or message text. |
| `timeout_s` | integer | No | Wait budget in seconds. Defaults to 180. |
| `from` | string | Yes | Caller identity. Impersonation values are refused. |

**Output:** Identical to `muse_chat_ask`, including reply IDs, completion mode, raw-fallback `agent_statuses`, latest `agent_status`, and `task_status`.

**Guardrails:** Identical directory, provenance, caller, and audit rules to `muse_chat_ask`.

**Example call:**

```json
{"tool":"sidechat_ask","arguments":{"chat":"Example Chat B","text":"What is your current task in one sentence?","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"chat":"Example Chat B","reply_text":"<reply text>","reply_message_id":"<reply-message-id>","agent_statuses":["working"],"completion":"turn_complete"}
```

**Known limitations:** The alias does not change ask semantics. In particular, it does not switch the SDK turn to `owns_chat=True`; that would change acknowledgement semantics and is deliberately out of scope.

### `muse_chat_history`

Read canonical transcript history through the Link HTTP tunnel. When a chat is named, the request is scoped with its `session_id`; when omitted, the VM's default/main history is returned.

**Transport:** Link

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | No | Directory chat ID or exact name. Omit for the default/main history. |
| `limit` | integer | No | Newest-event count. Defaults to 20 and clamps to 1..50. |
| `from` | string | Yes | Caller identity. Impersonation values are refused. |

**Output:** `{ok, status, scoped_to_chat, response}`, where `response` is the VM response exactly as returned.

**Guardrails:** A named chat must be classified and not suspended as registry-archived. The raw response is not rewritten or enriched by the server.

**Example call:**

```json
{"tool":"muse_chat_history","arguments":{"chat":"Example Chat","limit":10,"from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"status":200,"scoped_to_chat":"Example Chat","response":{"events":[{"event":"message.user"},{"event":"message.assistant"}]}}
```

**Known limitations:** History returns only canonical transcript events: `message.user`, `message.assistant`, and `delta.presentation`, as the newest N in chronological order. Live-stream event names such as `agent.status`, `task.status`, `approvals.snapshot`, and `tasks.updated` come from subscriptions, not history. Session scoping was left unverified by upstream documentation but was live-verified against this account in the 0.2.0 acceptance pass.

### `muse_chat_events`

Watch a classified chat's live Link event stream for a short, bounded window. This is the live-activity surface, not a history snapshot.

**Transport:** Link

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | Yes | Directory chat ID or exact name. |
| `seconds` | integer | No | Observation window. Defaults to 10 and clamps to 1..30. |
| `from` | string | Yes | Caller identity. Impersonation values are refused. |

**Output:** `{ok, chat, chat_id, seconds, event_count, counts, events, error}`. String fields in events are truncated to 500 characters.

**Guardrails:** Directory allowlist only. The subscription offers no backlog, so a quiet window honestly returns zero events rather than replaying old activity.

**Example call:**

```json
{"tool":"muse_chat_events","arguments":{"chat":"Example Chat","seconds":10,"from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"chat":"Example Chat","event_count":3,"counts":{"agent.status":1,"delta.text_append":1,"delta.message_done":1},"events":[{"event":"agent.status","payload":{"activity_code":"working"}}],"error":null}
```

**Known limitations:** Live event names include reply lifecycle deltas, `agent.status`, `task.status`, `approvals.snapshot`, and `tasks.updated`. `tasks.updated` carries no `session_id` and must not be attributed to the subscribed chat merely because it arrived during that chat's window.

### `muse_chat_send_voice`

Send caller-supplied WAV audio as a voice note into a classified chat. The provenance stamp rides in the accompanying text because it cannot be embedded in the audio by this tool.

**Transport:** Link

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | Yes | Directory chat ID or exact name. |
| `wav_base64` | string | Yes | Base64-encoded WAV bytes. Decoded audio must be non-empty and no larger than 2 MiB. |
| `message` | string | No | Accompanying message text. Defaults to `Voice note` before provenance stamping. |
| `from` | string | Yes | Caller identity. Impersonation values are refused. |

**Output:** Link receipt `{ok, status, message_id, session_id}`.

**Guardrails:** Invalid base64, empty audio, and audio over 2 MiB are refused before a Link call. Directory, draft, archived-suspension, and audit rules are the same as text send.

**Example call:**

```json
{"tool":"muse_chat_send_voice","arguments":{"chat":"Example Chat","wav_base64":"<base64-wav>","message":"Voice note for the current test","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"status":200,"message_id":"<message-id>","session_id":"<id>"}
```

**Known limitations:** WAV only through this typed wrapper. A send receipt does not include a transcript or prove that the receiving chat has processed the audio.

### `muse_voice_tts`

Retrieve TTS audio for an assistant reply that Muse produced in voice modality. It uses the Link TTS stream and returns the audio inline as base64.

**Transport:** Link

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `message_id` | string | Yes | Assistant reply message ID. Find it through `muse_chat_ask` or canonical history after a voice-modality send. |
| `from` | string | Yes | Caller identity. Impersonation values are refused. |

**Output:** `{ok:true, mime:"audio/mpeg", bytes, data_base64}`.

**Guardrails:** Streams larger than 8 MiB are refused. Every attempt is audit-logged by message ID; audio contents are not written to the audit log.

**Example call:**

```json
{"tool":"muse_voice_tts","arguments":{"message_id":"<assistant-message-id>","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"mime":"audio/mpeg","bytes":48384,"data_base64":"<base64-mp3>"}
```

**Known limitations:** Text-only replies have no TTS stream and are expected to fail. This tool retrieves audio for an existing reply; it does not synthesize arbitrary new text.

### `muse_link_request`

Make a restricted generic request through the Link HTTP tunnel. It exists for VM endpoints that the typed wrappers do not model.

**Transport:** Link

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `method` | string | Yes | `GET` or `POST` only. |
| `path` | string | Yes | VM path beginning with `/chat/`, `/voice/`, `/api/voice/`, or `/fs/`. |
| `body_json` | object/null | No | Optional JSON request body. |
| `from` | string | Yes | Caller identity. Impersonation values are refused. |

**Output:** `{ok, status, response}` exactly as tunnelled.

**Guardrails:** Other methods and off-allowlist paths are refused. The audit log records method, path, outcome, and body size only, never body contents.

**Example call:**

```json
{"tool":"muse_link_request","arguments":{"method":"GET","path":"/chat/history?limit=5","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"status":200,"response":{"events":[]}}
```

**Known limitations:** This is an escape hatch, not a promise that every allowlisted VM route is entitled for the paired credential. In particular, `/fs/` routes currently fail with the credential-scope error documented under the FS tools.

### `muse_fs_read`

Read bytes from a home-relative file path on the Muse VM. These are VM paths, not paths on the MCP server host.

**Transport:** Link

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `path` | string | Yes | Home-relative path on the Muse VM. |
| `offset` | integer | No | Byte offset. Defaults to 0; negative values clamp to 0. |
| `length` | integer | No | Byte count. Defaults to 4096 and clamps to 1..65536. |
| `from` | string | Yes | Caller identity. Impersonation values are refused. |

**Output:** `{ok, status, response}`. A successful response carries `data_base64`, optional UTF-8 `text`, and `eof`; callers should page until `eof`.

**Guardrails:** Caller validation and audit logging apply. Unlike `muse_fs_write`, this typed read tool has no additional local path-segment refusal list; the VM and credential scopes are the enforcement boundary.

**Example call:**

```json
{"tool":"muse_fs_read","arguments":{"path":"notes/example.txt","offset":0,"length":4096,"from":"example-caller"}}
```

**Example output:**

```json
{"ok":false,"status":403,"response":"route not allowed for this credential's scopes"}
```

**Known limitations:** Implemented to the documented request shape but currently blocked on Meta backend credential entitlement. The VM returns HTTP 403 with the exact error `route not allowed for this credential's scopes`. No SDK-documented app toggle, scope request, or re-pair path is known.

### `muse_fs_write`

Write text to a home-relative file path on the Muse VM using the documented `/fs/write` request shape. Local safety refusals run before the tunnel request.

**Transport:** Link

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `path` | string | Yes | Home-relative path on the Muse VM. |
| `text` | string | Yes | Text content to write. The argument may be an empty string, but the key is required by the tool schema. |
| `overwrite` | boolean | No | Allow replacement of an existing file. Defaults to `false`. |
| `append` | boolean | No | Append instead of replacing/creating. Defaults to `false`. |
| `create_parent` | boolean | No | Create parent directories. Defaults to `true`. |
| `from` | string | Yes | Caller identity. Impersonation values are refused. |

**Output:** `{ok, status, response}` exactly as tunnelled.

**Guardrails:** Paths under `.ssh`, `.aws`, or `.gnupg`, and any path containing `credential`, `secret`, or `token` case-insensitively, are refused locally. Every attempt is audit-logged with path and byte count.

**Example call:**

```json
{"tool":"muse_fs_write","arguments":{"path":"notes/example.txt","text":"hello","overwrite":false,"append":false,"create_parent":true,"from":"example-caller"}}
```

**Example output:**

```json
{"ok":false,"status":403,"response":"route not allowed for this credential's scopes"}
```

**Known limitations:** Implemented to spec but currently blocked on Meta backend credential entitlement. The VM returns HTTP 403 with the exact error `route not allowed for this credential's scopes`. No SDK-documented app toggle, scope request, or re-pair path is known.

### `muse_chat_status`

Return the conversation state of one chat: the last canonical transcript events from scoped Link history, the live turn state from a short subscription snapshot, and directory/registry classification, each labelled with its source. History-derived state and live state are returned separately as well as combined, because the two can disagree: history describes the last completed turn, while the live snapshot describes what the chat is doing now. The answer to "whose turn is it" lives here. Rewritten in 0.4.0; the 0.3.x form returned existence and audit facts only.

**Transport:** Link / local

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | Yes | Directory chat ID or exact name. Wholly unknown chats are refused. |
| `from` | string | Yes | Caller identity. Impersonation values are refused. |
| `history_limit` | integer | No | Newest history events to read. Default 50, accepted range 1..50. |
| `snapshot_seconds` | number | No | How long to listen for the live snapshot bundle. Default 3, accepted range 0..10. `0` skips the snapshot entirely. |
| `include_text` | boolean | No | Add a text excerpt (at most 280 characters) to each message block. Default `false`. |

**Output:** `{ok, chat, registered, addressable, archived, exists, exists_error, last_activity, last_user_message, last_assistant_message, turn_state, turn_state_source, history_turn_state, live_turn_state, live, history_error, recent_audit, registry_entry, registry_status, registry_revision, registry_synced_at, unread}`.

- `last_activity` is the last canonical transcript event: `{occurred_at_ms, seq, event_name, role, label}`. `delta.presentation` events are excluded from all message derivation.
- `last_user_message` and `last_assistant_message` are `{message_id, occurred_at_ms, seq}` blocks; the assistant block also carries `status` (`completed`, `failed`, or `interrupted`).
- Turn-state values: `awaiting_assistant_response` (newest message is the user's), `awaiting_user_next_message` (newest assistant message completed), `assistant_attempt_incomplete` (newest assistant message failed or was interrupted), `agent_working` (live snapshot shows a running task, active subagents, or a working activity code), `waiting_for_approval` (live snapshot shows pending approvals). History-only derivation yields the first three; the combined `turn_state` prefers the live state when the snapshot produced one, and `turn_state_source` says which (`live_snapshot` or `history`). Both source states are always returned separately.
- `live` is `{agent_activity_code, agent_activity_text, task_status, active_subagent_count, pending_approval_count, snapshot_received}`. `pending_approval_count` is `null` unless an `approvals.snapshot` event was actually received during the window; it is never reported as 0 by default. Pass `snapshot_seconds: 0` to force the no-snapshot case and observe the null.
- `unread` is always `null`, with a note in the output explaining why (see limitations).
- `recent_audit` contains at most the five newest matching audit records.

**Guardrails:** Registry drafts and registry-archived chats return metadata only (`registered`/`addressable`/`archived`, `exists: null` with `exists_note`, `last_activity` and `live` null) and make no live Link calls; the audit record carries `metadata_only: true`. Stream events whose payload session does not match the chat are ignored, and global `tasks.updated` snapshots are never attributed to the chat.

**Example call:**

```json
{"tool":"muse_chat_status","arguments":{"chat":"Example Chat","from":"example-caller"}}
```

**Example output** (live acceptance values, 0.4.0; note the designed disagreement: the last completed turn awaits the user while the chat is live-working):

```json
{"ok":true,"chat":{"name":"Example Chat","chat_id":"<id>"},"registered":true,"addressable":true,"archived":false,"exists":true,"last_user_message":{"message_id":"<id>","occurred_at_ms":1793154049970,"seq":252818},"last_assistant_message":{"message_id":"<assistant-message-id>","occurred_at_ms":1793154053643,"seq":252819,"status":"completed"},"history_turn_state":"awaiting_user_next_message","live_turn_state":"agent_working","turn_state":"agent_working","turn_state_source":"live_snapshot","live":{"agent_activity_code":"waiting_for_subagents","agent_activity_text":"is working","task_status":"running","active_subagent_count":2,"pending_approval_count":0,"snapshot_received":true},"unread":null}
```

(Audit and registry fields omitted for print; every value shown is exact.)

**Known limitations:** No read/seen field exists anywhere in either the Link history or live-event surface; `display_text_ready` is **not** a read indicator. There is no unread count, mark-as-read endpoint, typing indicator, or chat presence field in the surfaces available to this server, and no output of this tool approximates one. The live snapshot is a current-state bundle observed at subscribe time, not a replay: activity that starts and finishes entirely outside the window is visible only through history. A chat mid-turn reports `agent_working` from the snapshot even when its last completed turn awaits the user; that disagreement is designed behaviour, and both states are returned so the caller can choose.

### `muse_watch_digest`

Watch up to five classified chats in parallel for a bounded window and return a per-chat digest that separates the initial snapshot baseline from changes after it. Built for "who needs attention" sweeps without opening five chats.

**Transport:** Link

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chats` | array of string | Yes | 1..5 directory chat IDs or exact names. Registry drafts are refused. |
| `from` | string | Yes | Caller identity. Impersonation values are refused. |
| `seconds` | number | No | Watch window per chat. Default 20, accepted range 1..30. |
| `include_initial_snapshot` | boolean | No | Include the baseline block in each digest. Default `true`. |

**Output:** `{ok, seconds, chat_count, digests, from}`. Each digest is `{chat, chat_id, window_seconds, event_count, counts, initial_snapshot, change_events, changes_after_baseline, quiet_after_snapshot, reply_message_ids_completed, pending_approval_count_start, pending_approval_count_end, delta_summary, error}`.

- `initial_snapshot` holds the first `agent.status`, `task.status`, and `approvals.snapshot` observed on that chat's own subscription, each labelled `inferred_baseline`: it is the current state observed at subscribe time, not change history. Baseline event types are excluded from `change_events`.
- `change_events` (at most 50, with a `truncated` marker past the cap) carries the post-baseline events for that chat only. `reply_message_ids_completed` lists assistant messages that completed during the changes, including completions signalled by `delta.message_done`.
- `delta.text_append` payloads are never returned raw; they are counted in `delta_summary` as `{text_append_events, text_chars}`.
- `pending_approval_count_start` / `_end` come from that subscription's `approvals.snapshot` events and are `null` when none arrived; they are never guessed.
- One chat's failure lands in that digest's `error` field; it does not fail the whole call. `quiet_after_snapshot: true` means nothing changed after the baseline.

**Guardrails:** Registered, addressable chats only. Attribution is strict: an event counts for a chat only when it arrives on that chat's subscription and, where the payload carries a session ID, the session matches; global events (`tasks.updated`, `sessions.updated`, node events) are never attributed to any chat, and an `approvals.snapshot` belongs to the subscription that received it. Read-only: a watch never sends, wakes, or writes.

**Example call:**

```json
{"tool":"muse_watch_digest","arguments":{"chats":["Example Chat","<id>"],"seconds":20,"from":"example-caller"}}
```

**Example output** (live acceptance values, 0.4.0; a marked send landed in the first chat mid-window):

```json
{"ok":true,"seconds":20,"chat_count":2,"digests":[{"chat":"Example Chat","event_count":16,"counts":{"message.user":1,"delta.message_start":1,"delta.text_append":2,"delta.message_done":1},"changes_after_baseline":13,"quiet_after_snapshot":false,"reply_message_ids_completed":["<assistant-message-id>"],"pending_approval_count_start":0,"pending_approval_count_end":0,"delta_summary":{"text_append_events":2,"text_chars":20},"error":null},{"chat":"Example Session Chat","event_count":3,"counts":{"agent.status":1,"task.status":1,"approvals.snapshot":1},"changes_after_baseline":0,"quiet_after_snapshot":true,"reply_message_ids_completed":[],"pending_approval_count_start":0,"pending_approval_count_end":0,"delta_summary":{"text_append_events":0,"text_chars":0},"error":null}]}
```

**Known limitations:** A watch sees only its own window: there is no event backlog or replay, so activity between windows is invisible here (use `muse_chat_status` for at-rest state). Five parallel per-chat Link sessions are the cap. Pending-approval counts are per-subscription observations of that chat, not a global approvals queue.


---

## Bridge and lifecycle tools

The Bridge is the Muse-side worker chat named `🌉 muse-mcp — Bridge` (`chat_id` `<id>`, directory kind `bridge`). It is a wake target for queued operations, not a producer target.

A Bridge operation follows the same lifecycle:

1. The server writes `bridge-queue/<correlation_id>.json`.
2. The server sends a Link wake into the Bridge chat naming the tool and correlation ID.
3. The Bridge reads the request file, executes exactly that operation, writes `bridge-results/<correlation_id>.json`, and moves the request into `bridge-queue/done/`.
4. The caller polls `muse_request_get` with the correlation ID.

The wake in step 2 is a single unstamped message in the configured wake dialect (`MUSE_MCP_WAKE_FORMAT`). The default dialect is plain: the message names the tool, the correlation ID, the request file, and the result file, and the Bridge answers with one plain line. An optional `cant` dialect replaces it with a Cant 0.1 REQ frame (`op=` CRT/REN/ARC/UAR/DEL/RED, `arg=@file:<absolute request path>`), for deployments whose bridge worker is briefed for that machine dialect; queue receipts and audit records then carry `cant: 1` and `cant_corr`. Either way the wake is a doorbell: the request and result JSON files are the protocol. The wake dialect never appears in sends to classified chats, and `muse_registry_sync` (which addresses the main chat, an operator-read channel) keeps its plain-English stamped form.

### `muse_request_get`

Read the result of a Bridge-queued operation by correlation ID. This is the completion surface for every Bridge tool in this reference.

**Transport:** Bridge / local

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `correlation_id` | string | Yes | Correlation ID returned by a Bridge-queued tool. Path separators and traversal sequences are refused. |

**Output:** Completed: `{ok:true, correlation_id, status:"completed", result}`. Pending: `{ok:true, correlation_id, status:"pending"}`. Unknown: `{ok:false, correlation_id, status:"unknown", error}`. A Bridge result that itself reports failure is returned inside `result` rather than being disguised as a transport failure. Since 0.4.0, a result file caught mid-write (present but not yet valid JSON) also returns `pending`, with `detail: "result being written"`; callers poll through it instead of surfacing a parse error.

**Guardrails:** Read-only. It never executes the queued operation and never wakes the Bridge again.

**Example call:**

```json
{"tool":"muse_request_get","arguments":{"correlation_id":"<correlation-id>"}}
```

**Example output:**

```json
{"ok":true,"correlation_id":"<correlation-id>","status":"completed","result":{"ok":true,"chat_id":"<new-chat-id>"}}
```

**Known limitations:** There is no server-side completion deadline. If the Bridge chat is not running or does not take its turn, a request remains `pending`; callers must poll with a generous timeout and report an unfinished Bridge step as unfinished.

### `muse_chat_create`

Create a new Muse side chat through the Bridge and return its new chat ID in the Bridge result. Creation alone does not classify the chat or make it sendable.

**Transport:** Bridge

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `name` | string | Yes | Name for the new chat. |
| `brief` | string | Yes | One brief or opening instruction for the new chat, as supplied by the caller. |
| `from` | string | Yes | Caller identity. A value naming the operator is refused. |

**Output:** Immediate: `{ok, status:"queued", correlation_id, tool, wake_format, bridge_chat_id, wake_message_id, wake_error}`. Final Bridge result: an object carrying `ok` and the new `chat_id` on success.

**Guardrails:** The server queues exactly one create operation and wakes only the Bridge chat. The main chat is never a create target. Use `muse_directory_set` after creation to classify the returned ID before Link sends.

**Example call:**

```json
{"tool":"muse_chat_create","arguments":{"name":"🧪 muse-mcp throwaway P2","brief":"Temporary acceptance chat. Remember the marker BRIDGE-BRIEF-P2.","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"status":"queued","correlation_id":"<correlation-id>","tool":"<bridge-tool>","wake_format":"plain","bridge_chat_id":"<id>","wake_message_id":"<wake-message-id>","wake_error":null}
```

**Known limitations:** Creation is asynchronous and depends on the Bridge taking a turn. If the wake send fails, the result is `queued_unwoken`; the request file exists, but completion cannot be assumed.

### `muse_chat_rename`

Rename a Muse chat through the Bridge. The server resolves the chat to an ID before queueing, so the Bridge receives an unambiguous target.

**Transport:** Bridge

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | Yes | Directory/registry chat ID or exact name. |
| `name` | string | Yes | New chat name. |
| `from` | string | Yes | Caller identity. A value naming the operator is refused. |

**Output:** Immediate queue receipt `{ok, status:"queued", correlation_id, tool, wake_format, bridge_chat_id, wake_message_id, wake_error}`. Final result is read with `muse_request_get`.

**Guardrails:** Only directory/registry-known chats resolve. Renaming through the Bridge does not automatically rewrite `directory.json`; run a directory update or registry sync to converge server metadata.

**Example call:**

```json
{"tool":"muse_chat_rename","arguments":{"chat":"<throwaway-chat-id>","name":"🧪 muse-mcp throwaway P2 renamed","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"status":"queued","correlation_id":"<correlation-id>","tool":"<bridge-tool>","wake_format":"plain","bridge_chat_id":"<id>","wake_message_id":"<wake-message-id>","wake_error":null}
```

**Known limitations:** Asynchronous. A queue receipt is not proof that the rename landed; the Bridge result is the evidence.

### `muse_chat_archive`

Archive a Muse chat through the Bridge. Archiving is a Muse-native lifecycle state and is therefore not attempted through the Link.

**Transport:** Bridge

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | Yes | Directory/registry chat ID or exact name. |
| `from` | string | Yes | Caller identity. A value naming the operator is refused. |

**Output:** Immediate queue receipt `{ok, status:"queued", correlation_id, tool, wake_format, bridge_chat_id, wake_message_id, wake_error}`. Final result is read with `muse_request_get`.

**Guardrails:** The main chat is refused server-side. Registry-known chats may be resolved for lifecycle work even when they are not sendable; that does not change their send authorization.

**Example call:**

```json
{"tool":"muse_chat_archive","arguments":{"chat":"<throwaway-chat-id>","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"status":"queued","correlation_id":"<correlation-id>","tool":"<bridge-tool>","wake_format":"plain","bridge_chat_id":"<id>","wake_message_id":"<wake-message-id>","wake_error":null}
```

**Known limitations:** The server's registry may continue to show an older archived flag until the next registry sync returns data. A valid registry archived flag, once loaded, suspends Link targeting immediately.

### `muse_chat_unarchive`

Restore an archived Muse chat through the Bridge. It is the inverse lifecycle operation to `muse_chat_archive`.

**Transport:** Bridge

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | Yes | Directory/registry chat ID or exact name. Archived registry-known chats remain resolvable for this operation. |
| `from` | string | Yes | Caller identity. A value naming the operator is refused. |

**Output:** Immediate queue receipt `{ok, status:"queued", correlation_id, tool, wake_format, bridge_chat_id, wake_message_id, wake_error}`. Final result is read with `muse_request_get`.

**Guardrails:** The main chat is not specially blocked from unarchive because unarchiving is non-destructive, but the Bridge's own standing rules still apply. Resolution for lifecycle work never makes a draft sendable.

**Example call:**

```json
{"tool":"muse_chat_unarchive","arguments":{"chat":"<throwaway-chat-id>","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"status":"queued","correlation_id":"<correlation-id>","tool":"<bridge-tool>","wake_format":"plain","bridge_chat_id":"<id>","wake_message_id":"<wake-message-id>","wake_error":null}
```

**Known limitations:** Asynchronous. The directory/registry view converges only after the relevant metadata update or registry sync.

### `muse_chat_read`

Read a chat's native Muse turns through the Bridge. This is the correct tool when the caller needs the conversation as the Muse app represents it rather than the Link's transcript-event form.

**Transport:** Bridge

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | Yes | Directory/registry chat ID or exact name. |
| `limit` | integer | No | Maximum native turns to return. Defaults to 5 and clamps to 1..20. |
| `from` | string | Yes | Caller identity. A value naming the operator is refused. |

**Output:** Immediate queue receipt with `correlation_id`. The final Bridge result carries structured native turns in its result object.

**Guardrails:** Only directory/registry-known chats resolve. Read access does not classify a draft or grant send access.

**Example call:**

```json
{"tool":"muse_chat_read","arguments":{"chat":"Example Chat","limit":5,"from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"status":"queued","correlation_id":"<correlation-id>","tool":"<bridge-tool>","wake_format":"plain","bridge_chat_id":"<id>","wake_message_id":"<wake-message-id>","wake_error":null}
```

**Known limitations:** This is deliberately distinct from `muse_chat_history`: `muse_chat_read` returns native chat turns through the Bridge, while history returns Link canonical transcript events (`message.user`, `message.assistant`, `delta.presentation`). Bridge reads are asynchronous and depend on the Bridge's own turn.

### `muse_chat_relay`

Relay caller-supplied text from one classified chat to another through the Link. The destination message explicitly names the source chat and relaying caller in addition to the normal provenance stamp.

**Transport:** Link

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `from_chat` | string | Yes | Source directory chat ID or exact name. Used as provenance; the server does not read this chat. |
| `to_chat` | string | Yes | Destination directory chat ID or exact name. |
| `text` | string | Yes | Exact caller-supplied text to relay. |
| `from` | string | Yes | Relaying caller identity. A value naming the operator is refused. |

**Output:** Destination Link receipt plus `{from_chat, from_chat_id, to_chat, to_chat_id}`.

**Guardrails:** Both ends must be classified, non-suspended directory entries. The audit record includes both source and destination. Empty text is refused.

**Example call:**

```json
{"tool":"muse_chat_relay","arguments":{"from_chat":"Example Chat","to_chat":"Example Chat B","text":"Marker SCMCP-P2-RELAY: relay acceptance text.","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"status":200,"message_id":"<message-id>","from_chat":"Example Chat","from_chat_id":"<id>","to_chat":"Example Chat B","to_chat_id":"<id>"}
```

**Known limitations:** Relay is not forwarding in the Muse-native sense. The server sends new caller-supplied text; it does not fetch, quote, or cryptographically attest a message from the source chat.

### `muse_chat_delete_preview`

Preview a permanent chat deletion and obtain the required single-use confirmation token. This step deletes nothing.

**Transport:** Local

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | Yes | Directory/registry chat ID or exact name. |
| `from` | string | Yes | Caller identity. A value naming the operator is refused. |

**Output:** `{ok, chat_id, name, kind, source, registered, sendable, archived, last_audit_activity, preview_token, expires_at}`. The token expires after 10 minutes.

**Guardrails:** The main chat is refused at this step and at confirmation, always. Token hashes are stored server-side; the plain token is returned only in this preview result. Issuing a preview is audit-logged.

**Example call:**

```json
{"tool":"muse_chat_delete_preview","arguments":{"chat":"<throwaway-chat-id>","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"chat_id":"<throwaway-chat-id>","name":"🧪 muse-mcp throwaway P2 renamed","kind":"side","registered":true,"sendable":true,"preview_token":"<preview-token>","expires_at":"<expiry-timestamp>"}
```

**Known limitations:** Preview tokens are held in server memory, so a service restart invalidates outstanding tokens. A preview is a point-in-time view; it does not lock the chat against other operations.

### `muse_chat_delete`

Queue permanent deletion of a chat through the Bridge, but only after a valid deletion preview. The Bridge request carries `confirmed:true`.

**Transport:** Bridge

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | Yes | Same directory/registry chat ID or exact name used for the preview. |
| `preview_token` | string | Yes | Single-use, unexpired token from `muse_chat_delete_preview` for the same chat. |
| `from` | string | Yes | Caller identity. A value naming the operator is refused. |

**Output:** Immediate Bridge queue receipt `{ok, status:"queued", correlation_id, tool, wake_format, bridge_chat_id, wake_message_id, wake_error}` after token validation and consumption. Final deletion evidence is read with `muse_request_get`.

**Guardrails:** Missing, invalid, expired, reused, and wrong-chat tokens are refused before queueing. The main chat is refused always. A successful token validation consumes the token even if the asynchronous Bridge operation later fails, so a retry requires a fresh preview.

**Example call:**

```json
{"tool":"muse_chat_delete","arguments":{"chat":"<throwaway-chat-id>","preview_token":"<preview-token>","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"status":"queued","correlation_id":"<correlation-id>","tool":"<bridge-tool>","wake_format":"plain","bridge_chat_id":"<id>","wake_message_id":"<wake-message-id>","wake_error":null}
```

**Known limitations:** Permanent deletion is asynchronous and irreversible once the Bridge executes it. Verification after deletion depends on the surfaces still available for an unlisted chat; the Bridge result is the primary evidence, supplemented by any list/read/existence check that can still resolve the ID.

---

## Link, subscription, and registry-sync tools

### `muse_link_create`

Create a standing relationship record between two known chats in `links.json`. A link declares how two chats are intended to relate; it does not create a behaviour by itself.

**Transport:** Local

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `a_chat` | string | Yes | Source directory/registry chat ID or exact name. |
| `b_chat` | string | Yes | Destination directory/registry chat ID or exact name. |
| `mode` | string | Yes | One of `escalation`, `mirror-summary`, or `handoff`. |
| `from` | string | Yes | Creator identity. A value naming the operator is refused. |

**Output:** `{ok:true, link}`, where `link` includes `link_id`, both chat IDs/names, `mode`, `created_by`, `created_at`, and `firing:"not_implemented"`.

**Guardrails:** Links into a chat whose name or kind identifies the Verify or Gmail lane are refused. This server-side name/kind check remains in force if such a chat is ever added to the directory or registry.

**Example call:**

```json
{"tool":"muse_link_create","arguments":{"a_chat":"Example Chat B","b_chat":"Example Chat","mode":"handoff","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"link":{"link_id":"<link-id>","a_chat":"Example Chat B","b_chat":"Example Chat","mode":"handoff","firing":"not_implemented"}}
```

**Known limitations:** Storage and CRUD only. No automatic firing exists in this version; escalation, mirroring, and handoff delivery land with the future watcher work.

### `muse_link_list`

List all standing chat-link records stored in `links.json`. Use it to audit declared relationships before changing or deleting them.

**Transport:** Local

| Input | Type | Required | Meaning |
|---|---|---:|---|
| None | — | — | This tool takes no inputs. |

**Output:** `{ok:true, links, count}`.

**Guardrails:** Read-only. Listing a link does not activate it.

**Example call:**

```json
{"tool":"muse_link_list","arguments":{}}
```

**Example output:**

```json
{"ok":true,"links":[{"link_id":"<link-id>","mode":"handoff","firing":"not_implemented"}],"count":1}
```

**Known limitations:** There is no filter by chat or mode. More importantly, records are declarations only until watcher firing is implemented.

### `muse_link_delete`

Delete one standing chat-link record by its `link_id`. This removes the declaration from `links.json` and nothing else.

**Transport:** Local

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `link_id` | string | Yes | Exact ID returned by `muse_link_create` or `muse_link_list`. |
| `from` | string | Yes | Caller identity. A value naming the operator is refused. |

**Output:** `{ok:true, deleted:"<link-id>"}`. Unknown IDs return `ok:false`.

**Guardrails:** Audit-logged. It never deletes a chat and has no delivery side effect because link firing is not implemented.

**Example call:**

```json
{"tool":"muse_link_delete","arguments":{"link_id":"<link-id>","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"deleted":"<link-id>"}
```

**Known limitations:** Deletion is immediate and has no undo tool. Recreate the link if the declaration is still wanted.

### `muse_chat_subscribe`

Register a time-bounded watch on a classified chat and a named event label. Registrations carry the standing 14-day sunset unless a shorter lifetime is requested.

**Transport:** Local

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `chat` | string | Yes | Classified directory chat ID or exact name. Drafts cannot be subscribed through this tool. |
| `event` | string | Yes | Event label to watch, for example `reply`, `needs_operator`, or `any_turn`. Labels are stored as supplied. |
| `days` | integer | No | Registration lifetime. Defaults to 14, clamps to 1..14. |
| `from` | string | Yes | Subscriber identity. A value naming the operator is refused. |

**Output:** `{ok:true, subscription}`, including `subscription_id`, chat ID/name, event, subscriber, `created_at`, `expires_at`, `days`, and `delivery:"not_implemented"`.

**Guardrails:** Directory allowlist only. The registration is audit-logged and time-bounded; there is no permanent subscription through this tool.

**Example call:**

```json
{"tool":"muse_chat_subscribe","arguments":{"chat":"Example Chat","event":"reply","days":14,"from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"subscription":{"subscription_id":"<subscription-id>","chat":"Example Chat","event":"reply","days":14,"delivery":"not_implemented"}}
```

**Known limitations:** Storage and CRUD only. No notification or delivery fires in this version; delivery wiring is the watcher follow-up, and a stored subscription must not be described as an active alert.

### `muse_chat_subscriptions`

List all stored chat-watch registrations, including their delivery state and expiry. Use it to audit what watchers will need to honour when delivery wiring lands.

**Transport:** Local

| Input | Type | Required | Meaning |
|---|---|---:|---|
| None | — | — | This tool takes no inputs. |

**Output:** `{ok:true, subscriptions, count}`.

**Guardrails:** Read-only. Listing an expired registration does not renew it.

**Example call:**

```json
{"tool":"muse_chat_subscriptions","arguments":{}}
```

**Example output:**

```json
{"ok":true,"subscriptions":[{"subscription_id":"<subscription-id>","chat":"Example Chat","event":"reply","delivery":"not_implemented"}],"count":1}
```

**Known limitations:** Expired records remain visible until removed with `muse_chat_unsubscribe` or a future cleanup process. Their presence does not imply active delivery.

### `muse_chat_unsubscribe`

Remove one stored chat-watch registration by `subscription_id`. This is the cancellation companion to `muse_chat_subscribe`.

**Transport:** Local

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `subscription_id` | string | Yes | Exact ID returned by `muse_chat_subscribe` or `muse_chat_subscriptions`. |
| `from` | string | Yes | Caller identity. A value naming the operator is refused. |

**Output:** `{ok:true, deleted:"<subscription-id>"}`. Unknown IDs return `ok:false`.

**Guardrails:** Audit-logged. It removes only the local registration, never a chat or link.

**Example call:**

```json
{"tool":"muse_chat_unsubscribe","arguments":{"subscription_id":"<subscription-id>","from":"example-caller"}}
```

**Example output:**

```json
{"ok":true,"deleted":"<subscription-id>"}
```

**Known limitations:** There is no undo. Because delivery is not implemented in this version, unsubscribing has no in-flight notification to cancel.

### `muse_registry_sync`

Ask the main chat to refresh the server's chat registry. The request travels over the Link; the refreshed registry returns as data rather than as a Link reply.

**Transport:** Link

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `from` | string | Yes | Requester identity. A value naming the operator is refused. |
| `reason` | string | No | Human-readable reason included in the structured request. |
| `known_revision` | string | No | Directory revision the requester already knows. When omitted, the server sends its current directory revision. |

**Output:** `{ok, status, correlation_id, message_id, session_id, known_revision}`. This is the request receipt, not the registry itself.

**Guardrails:** Only the directory's designated main chat receives the request. The request asks the producer to preserve human-approved metadata, classify nothing implicitly, and enter newly enumerated chats as unregistered drafts.

**Example call:**

```json
{"tool":"muse_registry_sync","arguments":{"from":"example-caller","reason":"Pass 2 registry refresh","known_revision":"<directory-revision>"}}
```

**Example output:**

```json
{"ok":true,"status":200,"correlation_id":"<correlation-id>","message_id":"<message-id>","session_id":"<id>","known_revision":"<directory-revision>"}
```

**Known limitations:** Completion is observed by reading the returned registry data and its `registry_revision`, `previous_revision`, `registry_synced_at`, counts, and pair state. A shared store remains the planned system of record; this version consumes the returned `registry.json` working copy.

### `sidechat_registry_update`

Ask the main chat to refresh the server's chat registry. This alias shares the complete registry-sync implementation with `muse_registry_sync`.

**Transport:** Link

| Input | Type | Required | Meaning |
|---|---|---:|---|
| `from` | string | Yes | Requester identity. A value naming the operator is refused. |
| `reason` | string | No | Human-readable reason included in the structured request. |
| `known_revision` | string | No | Directory revision already known to the requester. |

**Output:** Identical to `muse_registry_sync`: Link request receipt plus `correlation_id` and `known_revision`.

**Guardrails:** Identical main-chat targeting, caller, metadata-preservation, draft-entry, and audit rules to `muse_registry_sync`.

**Example call:**

```json
{"tool":"sidechat_registry_update","arguments":{"from":"example-caller","reason":"Alias-path registry refresh"}}
```

**Example output:**

```json
{"ok":true,"status":200,"correlation_id":"<correlation-id>","message_id":"<message-id>","known_revision":"<directory-revision>"}
```

**Known limitations:** The alias does not make the asynchronous data return synchronous. Verify completion through list/status registry fields, not from the send receipt alone.
