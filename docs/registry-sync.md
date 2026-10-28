# Registry sync: the producer contract

The Link API cannot enumerate chats. The registry loop is how the
server learns what chats exist:

1. A caller invokes `muse_registry_sync`. The server sends a
   structured request into the **main chat** over the Link: a
   provenance-stamped message carrying a JSON block with
   `type: "registry_sync"`, a `correlation_id`, the requester,
   and the `known_revision` the caller holds.
2. The main chat's agent enumerates every chat with its own chat
   tools (including archived), diffs against `known_revision`,
   preserves human-approved metadata (purpose, pattern,
   classification) on existing entries, and enters genuinely new
   chats as unregistered drafts.
3. The agent returns the census **as data, not as a Link reply**:
   it writes `registry.json` to every host running the server
   (the file sits beside that host's `directory.json`), in the
   format below.
4. The server validates the file on load. An invalid file is
   rejected and the last valid registry keeps serving; validation
   is fail-closed by design and is never loosened to make a bad
   file pass.

## The `registry.json` format

```json
{
  "type": "registry_sync_result",
  "revision": "<producer-minted opaque revision>",
  "previous_revision": "<directory.json SHA-256 this census was based on>",
  "synced_at": "<ISO-8601 timestamp>",
  "counts": {
    "enumerated_total": 3,
    "registered": 1,
    "unregistered_drafts": 2
  },
  "entries": [
    {
      "chat_id": "<full chat ID>",
      "name": "<chat name>",
      "archived": false,
      "registered": true
    }
  ]
}
```

The server's validator (`_validate_registry` in `server.py`)
checks, and the producer must satisfy, all of the following:

- The root is an object with `type` exactly
  `"registry_sync_result"`.
- `entries` is a list; every entry has a non-empty `chat_id`; no
  `chat_id` repeats.
- `counts.enumerated_total` equals the number of entries,
  exactly. The census counts what it lists: if a chat is counted,
  it is in `entries`, and if it is in `entries`, it is counted.
  (A producer that counts registered-but-not-enumerated chats in
  `enumerated_total` writes files the validator rejects; this is
  the single most likely way to break the loop.)
- `counts.registered` equals the number of entries with
  `registered` true, and `counts.unregistered_drafts` equals the
  remaining entries.

Three revision values are distinct and must not be conflated:
`directory_revision` is the SHA-256 of `directory.json`;
`revision` (the registry revision) is the producer's opaque
string; `previous_revision` chains the census to the directory
revision it was produced from, which is how the server reports
the pair state (`current`, `directory_ahead`,
`registry_ahead_or_drift`).

## Idempotence

If enumeration matches the caller's `known_revision`, the correct
outcome is no new file and no new revision. Repeated sync calls
are cheap and safe by design.
