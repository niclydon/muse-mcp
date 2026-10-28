# Directing real-time voice into a specific chat

A Muse chat ID is not only a message address. It is also the
thread target for a real-time voice call: a Link client that names
a chat when it starts a call is admitted into that chat, and the
conversation happens in that thread, with that thread's context
and memory. A client that names nothing lands in the account's
primary (main) chat. This page describes how that works on both
client platforms the SDK ships, because it is documented nowhere
else we could find.

## Linux (the musegadget client)

The Linux client supports targeting directly, no modification
needed.

Command line:

```bash
musegadget voice --thread-id <chat-id>
```

(`--thread-id` is described in the CLI's own help as "target
thread, otherwise the primary thread.")

From Python, the `VoiceCall` class in `musegadget.voice_call`
takes the same parameter:

```python
VoiceCall(session, device_id=ident.node_id, thread_id="<chat-id>")
```

What happens on the wire: the call admission record (type
`hatch.voice.call.start`) carries a `thread_id` field when one is
set, and the readiness record the VM returns echoes the admitted
`thread_id`; the client checks that the admitted thread is the
one it asked for, and says so if it is not. The end-of-call
record carries the field as well. Leave `thread_id` unset and
every part of this is absent: the call is admitted to the
primary thread.

## ESP32 (gadget firmware)

The stock ESP32 firmware has no notion of a call destination:
its realtime call admission carries no thread field at all, so
an unmodified gadget can only ever wake up in the main chat.
This is a firmware omission, not a platform restriction.

Adding the same `thread_id` field to the admission record the
firmware sends, set to a chosen side chat's ID, is sufficient:
the platform honors it, the call is admitted into that side
chat, and a normal two-way voice conversation takes place in
that thread. We verified this live with an ESP32 unit running a
modified build: the call landed in the named side chat, and the
conversation is recorded in that thread like any other.

## One corollary: the persona does not follow the thread

A call admitted into a chat that is briefed as a different
character still speaks as the account's main assistant. As far
as we can tell, the thread is the room and the persona is the
seat's. Practical consequence: you can give every device its
own room, with its own memory and continuity, but they all walk
in as the same assistant. Design characters accordingly.

## Aside: how this was found

This fell out of building muse-mcp. The server's premise is
that a chat ID is a routable address for messages, and the proof
series for that premise (run against the estate's own paired
hosts in October 2026) included two voice tests:

1. An untargeted voice call placed by a Linux client resolved
   to the main chat, confirming the main chat is the default
   thread of the whole system, not merely the first chat in a
   list.
2. The same call, targeted with a side chat's ID as
   `thread_id`, was admitted with that exact ID in its
   readiness state. Caller-supplied targeting is honored on
   the VM; it is not filtered to the main chat.

The ESP32 result came later, by asking whether the omission
was in the platform or only in the firmware. It was only in
the firmware.

That is why this page lives in muse-mcp's documentation even
though muse-mcp itself does not place voice calls: addressing
is the platform property the server is built on, and voice
turned out to be one more surface where the same address
works. For reference, muse-mcp's own voice-shaped tools are
message-level: `muse_chat_send` with `output_modality: "voice"`
makes a reply TTS-eligible, and `muse_voice_tts` streams
synthesised speech. Live call placement stays with the Link
client on the host or the gadget, using the mechanism above.
