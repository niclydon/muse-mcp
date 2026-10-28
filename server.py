"""muse-mcp: MCP server for Muse side chats (the Muse MCP).

Rides the paired musegadget Linux client on this host (pairing
store /var/lib/musegadget, read-only here).
A Muse chat's ID is its Link session_id: LinkSession.send_chat(text,
session_id=<chat_id>) lands a message in that chat (proven 2026-10-27,
thread-targeting proof T3/T5/T6 on the gadgets' VM).

Tools (docs/architecture.md is the design overview):
  sidechat_list / muse_chat_list   the chat directory (directory.json
                 beside this file, re-read on every call). The
                 directory is the allowlist for every chat tool.
  sidechat_send / muse_chat_send   send one message into a listed
                 chat, provenance-stamped, audit-logged (v1 tool).
  muse_link_status    pairing/node/VM/directory health, no secrets.
  muse_chat_exists    Link open_side_chat existence check.
  muse_chat_ask       send + follow the reply via subscribe_chat and
                 the SDK's MuseTurn (Transport A conversational ask).
  muse_chat_history   GET /chat/history through the Link tunnel.
  muse_chat_events    bounded subscribe_chat window: live activity.
  muse_chat_send_voice  send_voice with a WAV voice note.
  muse_voice_tts      stream_tts MP3 for a voice-modality reply.
  muse_link_request   generic tunnel request, path-allowlisted.
  muse_fs_read / muse_fs_write  user-VM files via /fs/read, /fs/write.
  muse_registry_sync  ask the main chat (via Link) to refresh the
                 registry; the answer returns as data, not by Link.
  muse_chat_status    composite v2: directory/registry metadata +
                 Link existence + scoped history (last activity,
                 last user/assistant messages, history turn state)
                 + a short subscribe snapshot (live activity, task
                 status, pending approval count-or-null). Drafts get
                 metadata only. The app unread state is NOT exposed
                 by the Link API (docs/architecture.md); the result's
                 unread field is always null.
  muse_watch_digest   prototype: parallel bounded subscriptions to
                 up to 5 classified chats, per-chat inferred baseline
                 snapshot plus attributed changes, counts, and reply
                 IDs. Strict session attribution; tasks.updated and
                 other session-less global events are never claimed
                 for a chat.
  muse_directory_get/set  classified directory CRUD. The merged
                 list serves directory entries as sendable and
                 registry-only entries as non-sendable drafts.
  muse_gap_log / muse_gap_list  append-only capability-gap intake.
  muse_chat_create / rename / archive / unarchive / read, and
  muse_chat_delete (preview-then-confirm)  Muse-side operations
                 queued for the 🌉 muse-mcp — Bridge chat; callers
                 poll muse_request_get for the bridge result.
  muse_chat_relay     caller-supplied text relayed over the Link.
  muse_link_*         standing chat-link records (storage/CRUD;
                 automatic firing is a later watcher feature).
  muse_chat_subscribe / muse_chat_subscriptions /
  muse_chat_unsubscribe  watch registrations with a 14-day sunset
                 (storage/CRUD; delivery wiring is a later watcher).

Guardrails: the directory is the allowlist for every chat-targeting
tool; `from` (caller identity) is required on every acting/reading
tool and a `from` value naming the operator (the operator's own
configured identifiers) is refused, because Link-sent messages
land in a chat's user seat and must never masquerade as the
operator. Every tool appends one line to audit.log.
No secret value (pairing token, vm_auth_token, origin secret) is
ever returned or logged.

Env:
  TRANSPORT   stdio (default) or http for a standalone service
  BIND_HOST / PORT  for http transport (default 127.0.0.1:7899)
  ORIGIN_SHARED_SECRET  required for the HTTP transport: every
              request except GET /health must present it as the
              x-origin-secret header, and the server refuses to
              start in http mode without it (see _OriginSecretGuard)
  MUSE_MCP_OPERATOR_IDENTIFIERS  comma-separated caller identities
              refused as operator masquerade, in addition to the
              built-in generic defaults (see _REFUSED_FROM)
  MUSE_MCP_WAKE_FORMAT  bridge wake dialect: plain (default) or
              cant (see _wake_frame)
  MUSE_MCP_EXTRA_ALLOWED_HOSTS / MUSE_MCP_EXTRA_ALLOWED_ORIGINS
              optional gateway/tunnel hostnames to admit through
              DNS-rebinding protection (see TRANSPORT_SECURITY)
  SIDECHAT_DIRECTORY_FILE  directory.json path override
  SIDECHAT_AUDIT_LOG       audit.log path override
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.responses import JSONResponse

SERVICE_VERSION = "0.4.6"

BASE_DIR = Path(__file__).resolve().parent
DIRECTORY_FILE = Path(
    os.environ.get("SIDECHAT_DIRECTORY_FILE", BASE_DIR / "directory.json"))
REGISTRY_FILE = Path(
    os.environ.get("SIDECHAT_REGISTRY_FILE", BASE_DIR / "registry.json"))
AUDIT_LOG = Path(
    os.environ.get("SIDECHAT_AUDIT_LOG", BASE_DIR / "audit.log"))
GAPS_FILE = Path(
    os.environ.get("SIDECHAT_GAPS_FILE", BASE_DIR / "gaps.jsonl"))
LINKS_FILE = Path(
    os.environ.get("SIDECHAT_LINKS_FILE", BASE_DIR / "links.json"))
SUBSCRIPTIONS_FILE = Path(os.environ.get(
    "SIDECHAT_SUBSCRIPTIONS_FILE", BASE_DIR / "subscriptions.json"))
BRIDGE_QUEUE_DIR = Path(os.environ.get(
    "SIDECHAT_BRIDGE_QUEUE_DIR", BASE_DIR / "bridge-queue"))
BRIDGE_RESULTS_DIR = Path(os.environ.get(
    "SIDECHAT_BRIDGE_RESULTS_DIR", BASE_DIR / "bridge-results"))

# A registry older than this is still served, but list/status calls
# label it stale so callers do not mistake an old census for current
# chat state.
_REGISTRY_STALE_SECONDS = 24 * 60 * 60
_DELETE_PREVIEW_TTL_SECONDS = 10 * 60
_delete_previews: dict[str, dict] = {}
_last_good_registry: dict | None = None

# Archived state learned from completed Bridge results, applied
# over the registry file's view until a synced registry agrees.
# Registry enumeration lags Bridge archive/unarchive operations
# by at least one sync cycle (smoke SMOKE-20261027-1, T2-8 and
# T3-12: the first sync after an archive still enumerated the
# chat as unarchived, and the documented suspension refusal only
# fired after a later sync). The server receives every Bridge
# result through muse_request_get, so it records the outcome
# there and _registry_entries consults it, making the documented
# archived suspension fire on the very next send.
_bridge_archived_overrides: dict[str, dict] = {}

MAIN_CHAT_NAME_HINT = "Main chat"

# DNS-rebinding protection stays ON: requests reach this service
# either from a local MCP client or from remote clients presenting
# the origin secret. The configured BIND_HOST is part of the
# allowlist: a host bound to a non-loopback address must accept its
# own Host header (without this, a second host bound to a different
# address rejects itself with HTTP 421).
_BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
_BIND_HOSTS = (
    [] if _BIND_HOST in ("127.0.0.1", "localhost", "::1")
    else [f"{_BIND_HOST}:*"])
_BIND_ORIGINS = (
    [] if _BIND_HOST in ("127.0.0.1", "localhost", "::1")
    else [f"http://{_BIND_HOST}:*"])

# Optional gateway path: if you front this server with a reverse
# proxy or tunnel under your own hostname(s), those Hosts must pass
# DNS-rebinding protection or the proxied requests 421. Name them in
# MUSE_MCP_EXTRA_ALLOWED_HOSTS (comma-separated hostnames) and
# MUSE_MCP_EXTRA_ALLOWED_ORIGINS (comma-separated https origins).
# The X-Origin-Secret guard still gates every non-/health request
# on that path. No gateway hostname is built into the server.
_GATEWAY_HOSTS = [h.strip() + ":*" for h in
                  os.environ.get("MUSE_MCP_EXTRA_ALLOWED_HOSTS",
                                 "").split(",") if h.strip()]
_GATEWAY_ORIGINS = [o.strip() for o in
                    os.environ.get("MUSE_MCP_EXTRA_ALLOWED_ORIGINS",
                                   "").split(",") if o.strip()]
TRANSPORT_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=list(dict.fromkeys([
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
        *_BIND_HOSTS,
        *_GATEWAY_HOSTS,
    ])),
    allowed_origins=list(dict.fromkeys([
        "https://claude.ai",
        "http://127.0.0.1:*",
        "http://localhost:*",
        *_BIND_ORIGINS,
        *_GATEWAY_ORIGINS,
    ])),
)

mcp = MCPServer("muse-mcp")

_SEND_LOCK = asyncio.Lock()

# Caller identities that would masquerade as the operator. The
# generic defaults are refused for everyone; add your own name and
# handles via MUSE_MCP_OPERATOR_IDENTIFIERS (comma-separated) so a
# caller can never post under the operator's own identity.
_REFUSED_FROM = {"user", "owner"} | {
    value.strip().lower() for value in
    os.environ.get("MUSE_MCP_OPERATOR_IDENTIFIERS", "").split(",")
    if value.strip()}

_LINK_PATH_PREFIXES = ("/chat/", "/voice/", "/api/voice/", "/fs/")
_FS_REFUSED_SEGMENTS = {".ssh", ".aws", ".gnupg"}
_FS_REFUSED_WORDS = ("credential", "secret", "token")


def _load_directory() -> list[dict]:
    """Load directory.json, preserving classification metadata.

    The directory remains the send allowlist. Older entries contain
    only name/chat_id/kind/purpose; newer entries may also carry
    pattern, escalation_bar, producers, cadence, and classification
    provenance. Preserve those fields rather than silently dropping
    them at the server boundary.
    """
    data = json.loads(DIRECTORY_FILE.read_text(encoding="utf-8"))
    entries = data["chats"] if isinstance(data, dict) else data
    loaded: list[dict] = []
    for entry in entries:
        item = dict(entry)
        item.setdefault("kind", "")
        item.setdefault("purpose", "")
        loaded.append(item)
    return loaded


def _save_directory(entries: list[dict]) -> None:
    """Atomically replace directory.json (same-directory temp file)."""
    DIRECTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DIRECTORY_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.replace(tmp, DIRECTORY_FILE)


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _registry_pair_state(data: dict) -> str:
    """Classify directory/registry revision-chain agreement.

    registry.previous_revision is the directory-file SHA-256 from
    which the opaque registry revision was minted. Equality means
    current. If it differs and the directory still contains every
    registry-classified chat, the directory moved ahead after the
    sync. If the registry names classified chats the directory
    lacks, the pair is registry-ahead or drift and must be treated
    as a reconciliation problem, not silently resolved.
    """
    if data.get("previous_revision") == _directory_revision():
        return "current"
    directory = _load_directory()
    directory_ids = {entry["chat_id"] for entry in directory}
    registered_ids = {
        entry["chat_id"] for entry in data.get("entries", [])
        if isinstance(entry, dict) and entry.get("registered")}
    if registered_ids <= directory_ids:
        # The directory contains every registry-classified chat
        # (possibly plus classifications made after the sync).
        return "directory_ahead"
    return "registry_ahead_or_drift"


def _validate_registry(data) -> str | None:
    """Return a validation error, or None for a usable registry."""
    if not isinstance(data, dict):
        return "registry root is not an object"
    if data.get("type") != "registry_sync_result":
        return "registry type is not 'registry_sync_result'"
    entries = data.get("entries")
    counts = data.get("counts")
    if not isinstance(entries, list):
        return "registry entries is not a list"
    if not isinstance(counts, dict):
        return "registry counts is not an object"
    chat_ids = [entry.get("chat_id") for entry in entries
                if isinstance(entry, dict)]
    if len(chat_ids) != len(entries) or any(not x for x in chat_ids):
        return "registry contains an entry without a chat_id"
    if len(chat_ids) != len(set(chat_ids)):
        return "registry contains duplicate chat_ids"
    enumerated = counts.get("enumerated_total")
    if enumerated is not None and enumerated != len(entries):
        return "registry entry count does not match counts.enumerated_total"
    registered = counts.get("registered")
    drafts = counts.get("unregistered_drafts")
    if registered is not None and drafts is not None:
        actual_registered = sum(
            1 for entry in entries if entry.get("registered"))
        if registered != actual_registered:
            return "registry registered count does not match entries"
        if drafts != len(entries) - actual_registered:
            return "registry draft count does not match entries"
    return None


def _load_registry() -> tuple[dict | None, dict]:
    """Load and validate registry.json, retaining the last good copy.

    Three revision values are distinct: registry_revision is the
    opaque producer-minted canonical revision; directory_revision
    is directory.json's SHA-256; registry_synced_at is producer
    time. A bad new file never changes authorization: callers get
    the in-memory last-good registry (if any) plus state:"invalid".
    """
    global _last_good_registry
    if not REGISTRY_FILE.exists():
        return None, {
            "state": "missing",
            "pair_state": "missing",
            "path": str(REGISTRY_FILE),
            "revision": None,
            "registry_revision": None,
            "directory_revision": _directory_revision(),
            "synced_at": None,
            "registry_synced_at": None,
            "age_seconds": None,
            "counts": None,
        }
    try:
        data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
        validation_error = _validate_registry(data)
        if validation_error:
            raise ValueError(validation_error)
    except Exception as e:
        last_good_status = None
        if _last_good_registry is not None:
            last_good_status = {
                "revision": _last_good_registry.get("revision"),
                "synced_at": _last_good_registry.get("synced_at"),
                "counts": _last_good_registry.get("counts"),
            }
        return _last_good_registry, {
            "state": "invalid",
            "pair_state": "invalid",
            "path": str(REGISTRY_FILE),
            "revision": None,
            "registry_revision": None,
            "directory_revision": _directory_revision(),
            "synced_at": None,
            "registry_synced_at": None,
            "age_seconds": None,
            "counts": None,
            "error": f"{type(e).__name__}: {e}",
            "last_good": last_good_status,
        }
    _last_good_registry = data
    synced = _parse_time(data.get("synced_at"))
    if synced is None:
        synced = datetime.fromtimestamp(
            REGISTRY_FILE.stat().st_mtime, timezone.utc)
    age = max(0, int((datetime.now(timezone.utc) - synced).total_seconds()))
    return data, {
        "state": "stale" if age > _REGISTRY_STALE_SECONDS else "ok",
        "pair_state": _registry_pair_state(data),
        "path": str(REGISTRY_FILE),
        "revision": data.get("revision"),
        "registry_revision": data.get("revision"),
        "directory_revision": _directory_revision(),
        "synced_at": data.get("synced_at"),
        "registry_synced_at": data.get("synced_at"),
        "age_seconds": age,
        "counts": data.get("counts"),
        "previous_revision": data.get("previous_revision"),
    }


def _registry_entries() -> list[dict]:
    data, _status = _load_registry()
    if not data:
        return []
    entries: list[dict] = []
    for entry in data.get("entries", []):
        if not isinstance(entry, dict) or not entry.get("chat_id"):
            continue
        item = dict(entry)
        item.setdefault("kind", "")
        item.setdefault("purpose", "")
        item.setdefault("registered", False)
        item.setdefault("archived", False)
        override = _bridge_archived_overrides.get(item["chat_id"])
        if override is not None:
            if bool(item.get("archived")) == override["archived"]:
                # The synced registry agrees with the Bridge
                # outcome; the file is the source of truth again.
                del _bridge_archived_overrides[item["chat_id"]]
            else:
                item["archived"] = override["archived"]
                item["archived_source"] = "bridge_result"
        entries.append(item)
    return entries


def _registry_entry(chat: str) -> dict | None:
    return next(
        (e for e in _registry_entries()
         if chat == e["chat_id"] or chat == e.get("name")),
        None)


def _bridge_archived_override(chat: str) -> bool | None:
    """Bridge-learned archived state for a chat, by ID or name.

    _registry_entries applies overrides to chats the registry
    file enumerates. A chat the registry has never enumerated
    (created since the last sync) has no entry to override, so
    consumers that resolve directory entries directly consult
    this as well: the server's own Bridge result is fresher than
    the file's silence. Returns None when nothing was learned.
    """
    override = _bridge_archived_overrides.get(chat)
    if override is not None:
        return override["archived"]
    for entry in _load_directory():
        if chat == entry["chat_id"] or chat == entry.get("name"):
            override = _bridge_archived_overrides.get(
                entry["chat_id"])
            return override["archived"] if override else None
    return None


def _resolve_known_chat(chat: str) -> dict | None:
    """Resolve a directory entry first, then a registry-known chat.

    This is for bridge lifecycle/read operations. It does NOT make a
    registry draft sendable: Link sends still use _resolve_chat and
    therefore directory.json only.
    """
    target = _resolve_chat(chat)
    if target is not None:
        return {**target, "source": "directory", "sendable": True}
    if _bridge_archived_override(chat):
        # Lifecycle/read resolution for a Bridge-archived chat
        # the registry does not (yet) suspend: unarchive and
        # read must still reach it. Send paths never come
        # through here; they use _resolve_chat and stay refused.
        directory_entry = next(
            (e for e in _load_directory()
             if chat == e["chat_id"] or chat == e.get("name")),
            None)
        if directory_entry is not None:
            return {**directory_entry, "source": "directory",
                    "sendable": False, "addressable": False,
                    "archived": True,
                    "archived_source": "bridge_result"}
    draft = _registry_entry(chat)
    if draft is not None:
        return {**draft, "source": "registry", "sendable": False,
                "addressable": False,
                "registration_status": (
                    "enumerated but not classified/registered")}
    return None


def _is_main_entry(entry: dict | None) -> bool:
    if not entry:
        return False
    if entry.get("kind") == "main":
        return True
    return any(
        entry.get("chat_id") == candidate.get("chat_id")
        and candidate.get("kind") == "main"
        for candidate in _load_directory())


def _directory_revision() -> str:
    return hashlib.sha256(DIRECTORY_FILE.read_bytes()).hexdigest()


def _directory_updated() -> str:
    ts = DIRECTORY_FILE.stat().st_mtime
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _audit(record: dict) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _validate_from(from_: str | None) -> tuple[str | None, dict | None]:
    """Return (caller, None) or (None, refusal-dict)."""
    caller = (from_ or "").strip()
    if not caller:
        return None, {"ok": False,
                      "error": "'from' (caller identity) is required"}
    if caller.lower() in _REFUSED_FROM:
        return None, {
            "ok": False,
            "error": (f"refused: from={caller!r} would masquerade as the "
                      "operator; use your own caller identity"),
        }
    return caller, None


def _resolve_chat(chat: str) -> dict | None:
    target = next(
        (e for e in _load_directory()
         if chat == e["chat_id"] or chat == e["name"]),
        None)
    if target is None:
        return None
    registry_entry = _registry_entry(target["chat_id"])
    if registry_entry and registry_entry.get("archived"):
        # Fail closed: a valid registry's archived flag suspends a
        # directory entry from chat-targeting tools immediately,
        # even though directory.json still contains the entry.
        return None
    if _bridge_archived_override(target["chat_id"]):
        # Fail closed on the server's own Bridge knowledge too:
        # the registry may lag the archive, may not enumerate
        # this chat yet, or may currently be invalid (last-good
        # served); the completed archive result is fresher than
        # all three.
        return None
    return target


def _refuse_unlisted(tool: str, caller: str, chat: str) -> dict:
    if _bridge_archived_override(chat):
        # The suspension refusal also fires on Bridge-learned
        # state, with the same wording as the registry path.
        directory_entry = next(
            (e for e in _load_directory()
             if chat == e["chat_id"] or chat == e.get("name")),
            None)
        chat_id = (directory_entry or {}).get("chat_id", chat)
        error = (
            f"refused: {chat!r} is archived in the valid synced "
            "registry and is suspended from chat-targeting tools "
            "immediately (fail closed)")
        _audit({"tool": tool, "from": caller, "chat": chat,
                "chat_id": chat_id, "ok": False, "error": error})
        return {"ok": False, "error": error, "chat_id": chat_id,
                "registered": directory_entry is not None,
                "sendable": False, "addressable": False,
                "archived": True}
    draft = _registry_entry(chat)
    if draft is not None:
        if draft.get("archived"):
            error = (
                f"refused: {chat!r} is archived in the valid synced "
                "registry and is suspended from chat-targeting tools "
                "immediately (fail closed)")
            _audit({"tool": tool, "from": caller, "chat": chat,
                    "chat_id": draft.get("chat_id"), "ok": False,
                    "error": error})
            return {"ok": False, "error": error,
                    "chat_id": draft.get("chat_id"),
                    "registered": bool(draft.get("registered")),
                    "sendable": False, "addressable": False,
                    "archived": True}
        error = (
            f"refused: {chat!r} is enumerated but not "
            "classified/registered in the synced registry "
            "(addressable:false); classify it with "
            "muse_directory_set to add it to directory.json and "
            "enable sends")
        _audit({"tool": tool, "from": caller, "chat": chat,
                "chat_id": draft.get("chat_id"), "ok": False,
                "error": error})
        return {"ok": False, "error": error,
                "chat_id": draft.get("chat_id"),
                "registered": False, "sendable": False,
                "addressable": False,
                "registration_status": (
                    "enumerated but not classified/registered")}
    _audit({"tool": tool, "from": caller, "chat": chat, "chat_id": None,
            "ok": False, "error": "chat not in directory (allowlist)"})
    return {
        "ok": False,
        "error": (f"refused: {chat!r} is not in the muse-mcp "
                  "directory (the directory is the allowlist); "
                  "see sidechat_list"),
    }


def _stamped(caller: str, text: str) -> str:
    return f"⟦via muse-mcp · from {caller}⟧ " + text


def _dig(obj, key: str, depth: int = 0):
    """Find key in a Link response wrapper ({ok,status,response} and
    the {result:...} envelope nest message_id at varying depths)."""
    if depth > 4 or not isinstance(obj, dict):
        return None
    value = obj.get(key)
    if value:
        return value
    for sub_key in ("response", "result"):
        found = _dig(obj.get(sub_key), key, depth + 1)
        if found:
            return found
    return None


def _receipt(result: dict, chat_id: str) -> dict:
    return {
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "message_id": _dig(result, "message_id"),
        "session_id": _dig(result, "session_id") or chat_id,
    }


# ---------------------------------------------------------------- Link


def _pairing_and_identity():
    """(pairing dict or None, identity). Pairing values never leave
    this module: callers get booleans/ids only, never tokens."""
    from musegadget import config, identity

    pairing = config.load_json(config.PAIRING_FILE)
    ident = identity.load_or_create()
    return pairing, ident


async def _fetch_vms(pairing: dict) -> tuple[list[dict], int | None]:
    from musegadget import muse_api

    return await asyncio.to_thread(
        muse_api.fetch_vms_with_status,
        pairing["access_token"],
        muse_api.api_root(pairing.get("api_url_v2", "")),
    )


def _pick_vm(vms: list[dict]) -> dict:
    defaults = [vm for vm in vms if vm.get("is_default")]
    if defaults:
        return defaults[0]
    if len(vms) == 1:
        return vms[0]
    raise RuntimeError(
        f"cannot pick a VM unambiguously: {len(vms)} leased, "
        "none marked default")


@asynccontextmanager
async def _link_session():
    """One ready ephemeral LinkSession, built exactly as the
    musegadget CLI builds its voice-call session (pairing load, VM
    fetch, register_device=False, wait_transport_ready). Proven flow:
    thread-targeting proof T6, 2026-10-27. Every Link tool shares
    this helper."""
    from musegadget import __version__ as mg_version
    from musegadget.link_client import DeviceDescription, LinkSession
    try:
        from musegadget.service import DEFAULT_NOISE_HOST
    except Exception:
        DEFAULT_NOISE_HOST = "hatch.metaaivm.com"

    # WebSocket debug logs include the Authorization header; never let
    # them run for a credentialed session (mirrors the CLI).
    logging.getLogger("websockets").setLevel(logging.WARNING)

    pairing, ident = _pairing_and_identity()
    if not pairing or not pairing.get("access_token"):
        raise RuntimeError("musegadget client on this host is not paired")
    vms, status = await _fetch_vms(pairing)
    if status != 200:
        raise ConnectionError(
            "VM lookup failed; refresh the paired device credentials")
    vm = _pick_vm(vms)
    session = LinkSession(
        noise_host=pairing.get("noise_host") or DEFAULT_NOISE_HOST,
        vm_id=vm["vm_id"],
        vm_auth_token=vm["vm_auth_token"],
        app_id="hatch-web",
        register_device=False,
        device=DeviceDescription(ident.node_id, "sidechat-mcp",
                                 mg_version, {}),
        run_command=lambda *unused: {"ok": False,
                                     "error": "unsupported command"},
    )
    stop = asyncio.Event()
    connection = asyncio.create_task(session.run(stop))
    try:
        await session.wait_transport_ready(35)
        yield session
    finally:
        stop.set()
        try:
            await asyncio.wait_for(connection, 3)
        except Exception:
            connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)


async def _send_via_link(chat_id: str, text: str,
                         output_modality: str = "text") -> dict:
    async with _link_session() as session:
        return await session.send_chat(
            text, session_id=chat_id, output_modality=output_modality)


# ------------------------------------------------------------- tools


def _list_payload(include_archived: bool = False) -> dict:
    """Build the directory view plus a separate registry-drafts view.

    Directory entries are classified and addressable, except any a
    valid registry marks archived: those remain visible in chats
    for diagnosis but carry addressable/sendable:false and are
    refused by chat-targeting tools. Registry-only entries are
    NEVER mixed into chats. Unarchived drafts are returned in
    drafts; archived drafts require include_archived:true.
    """
    entries = _load_directory()
    directory_ids = {entry["chat_id"] for entry in entries}
    registry_by_id = {
        entry["chat_id"]: entry for entry in _registry_entries()}
    chats: list[dict] = []
    for entry in entries:
        registry_entry = registry_by_id.get(entry["chat_id"], {})
        archived = bool(registry_entry.get("archived"))
        override = _bridge_archived_overrides.get(entry["chat_id"])
        archived_source = None
        if override is not None \
                and override["archived"] != archived:
            archived = override["archived"]
            archived_source = "bridge_result"
        chats.append({
            **entry,
            "registered": True,
            "sendable": not archived,
            "addressable": not archived,
            "archived": archived,
            **({"archived_source": archived_source}
               if archived_source else {}),
            "source": "directory",
        })
    registry, registry_status = _load_registry()
    drafts: list[dict] = []
    archived_draft_count = 0
    if registry:
        for entry in _registry_entries():
            if entry["chat_id"] in directory_ids:
                continue
            if entry.get("archived"):
                archived_draft_count += 1
                if not include_archived:
                    continue
            drafts.append({
                **entry,
                "sendable": False,
                "addressable": False,
                "source": "registry",
                "classification_required": True,
                "registration_status": (
                    "enumerated but not classified/registered"),
            })
    return {
        "chats": chats,
        "drafts": drafts,
        "count": len(chats),
        "total_count": len(chats) + len(drafts),
        "directory_count": len(entries),
        "draft_count": len(drafts),
        "archived_draft_count": archived_draft_count,
        "include_archived": include_archived,
        "revision": _directory_revision(),
        "directory_revision": _directory_revision(),
        "updated": _directory_updated(),
        "registry_status": registry_status,
        "registry_revision": registry_status.get("registry_revision"),
        "registry_synced_at": registry_status.get("registry_synced_at"),
    }


@mcp.tool()
def sidechat_list(include_archived: bool = False) -> dict:
    """List classified chats and, separately, registry drafts.

    Purpose: show the chats the server can address without mixing
    unclassified enumerations into that addressable set.

    Args:
      include_archived: when true, also include archived registry
        drafts in the separate drafts array. Directory entries are
        always shown, with archived ones marked non-addressable.

    Returns: chats (directory entries only, registered:true;
    addressable/sendable true unless a valid registry marks the
    entry archived), drafts (registry-only entries, addressable:
    false and registration_status "enumerated but not
    classified/registered"), counts, directory_revision (file
    SHA-256), registry_revision (opaque producer revision),
    registry_synced_at, and registry_status including pair_state.

    Guardrails: drafts are never sendable by virtue of being
    listed. A directory entry marked archived in a valid registry
    is suspended from chat-targeting tools immediately.
    """
    payload = _list_payload(include_archived)
    _audit({"tool": "sidechat_list", "ok": True,
            "count": payload["count"],
            "draft_count": payload["draft_count"],
            "directory_revision": payload["directory_revision"]})
    return payload


@mcp.tool()
def muse_chat_list(include_archived: bool = False) -> dict:
    """Alias of sidechat_list with the same separated views.

    Purpose, arguments, return shape, and guardrails are identical
    to sidechat_list: chats contains directory entries only;
    registry drafts are separate and non-addressable until
    classified with muse_directory_set.
    """
    payload = _list_payload(include_archived)
    _audit({"tool": "muse_chat_list", "ok": True,
            "count": payload["count"],
            "draft_count": payload["draft_count"],
            "directory_revision": payload["directory_revision"]})
    return payload


# ------------------------------------------------- directory CRUD


@mcp.tool()
def muse_directory_get(chat: str | None = None) -> dict:
    """Read directory entries and classification metadata.

    Purpose: inspect the classified allowlist without guessing from
    the merged chat list. With no chat, returns every directory
    entry. With a chat ID or exact name, returns that directory
    entry; if the chat is known only as a registry draft, returns
    the draft with in_directory:false and sendable:false.

    Args:
      chat: optional chat_id or exact directory/registry name.

    Returns: {ok, entries, revision, updated, registry_status} for
    the whole directory, or {ok, entry, in_directory, sendable,
    revision} for one chat. Unknown chats return ok:false.

    Guardrails: read-only. A registry draft returned here remains
    non-sendable until muse_directory_set classifies it.
    """
    revision = _directory_revision()
    _registry, registry_status = _load_registry()
    if chat is None:
        return {
            "ok": True,
            "entries": _load_directory(),
            "revision": revision,
            "updated": _directory_updated(),
            "registry_status": registry_status,
        }
    target = _resolve_chat(chat)
    if target is not None:
        return {"ok": True, "entry": target, "in_directory": True,
                "sendable": True, "revision": revision}
    draft = _registry_entry(chat)
    if draft is not None:
        return {"ok": True, "entry": draft, "in_directory": False,
                "sendable": False, "addressable": False,
                "registration_status": (
                    "enumerated but not classified/registered"),
                "revision": revision,
                "directory_revision": revision,
                "registry_revision": registry_status.get(
                    "registry_revision"),
                "registry_synced_at": registry_status.get(
                    "registry_synced_at"),
                "registry_status": registry_status}
    return {"ok": False,
            "error": f"unknown chat {chat!r} in directory and registry",
            "revision": revision}


@mcp.tool()
def muse_directory_set(
        chat_id: str, base_revision: str,
        name: str | None = None, purpose: str | None = None,
        kind: str | None = None, pattern: str | None = None,
        escalation_bar: str | None = None,
        producers: list[str] | None = None,
        cadence: str | None = None, classified_by: str = "",
        from_: str = Field(validation_alias="from")) -> dict:
    """Create or update one classified directory entry.

    Purpose: classify a registry draft into directory.json (which
    makes it sendable) or update metadata on an existing entry.
    This is the governed path from draft to addressable chat.

    Args:
      chat_id: exact Muse chat ID to create/update. Required.
      base_revision: directory sha256 revision the caller based
        this write on. Required; a stale value is refused.
      name, purpose, kind, pattern, escalation_bar, producers,
      cadence: optional fields. Unspecified fields retain their
        existing directory value, or the registry draft's value
        when classifying a draft.
      classified_by: who classified the chat; defaults to `from`.
      from: caller identity. Required; a value naming the operator
        (the operator's own configured identifiers) is refused.

    Returns: {ok, entry, previous_revision, revision}. Stale-base
    and validation failures return ok:false and write nothing.

    Guardrails: revision-checked single-writer discipline; every
    attempted and successful write is audit-logged. Writing an
    entry does not post anything to the chat.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    if not (chat_id or "").strip():
        return {"ok": False, "error": "chat_id is required"}
    current_revision = _directory_revision()
    if base_revision != current_revision:
        error = ("refused: stale base_revision; reload the directory "
                 "and retry against the current revision")
        _audit({"tool": "muse_directory_set", "from": caller,
                "chat_id": chat_id, "ok": False, "error": error,
                "base_revision": base_revision,
                "current_revision": current_revision})
        return {"ok": False, "error": error,
                "revision": current_revision}
    entries = _load_directory()
    existing = next(
        (entry for entry in entries if entry["chat_id"] == chat_id),
        None)
    draft = _registry_entry(chat_id)
    entry = dict(existing or draft or {"chat_id": chat_id})
    entry["chat_id"] = chat_id
    # Registry transport/status fields are computed from the
    # source at read time; never persist them as directory data.
    for status_key in (
            "registered", "archived", "source", "sendable",
            "addressable", "classification_required",
            "registration_status"):
        entry.pop(status_key, None)
    values = {
        "name": name,
        "purpose": purpose,
        "kind": kind,
        "pattern": pattern,
        "escalation_bar": escalation_bar,
        "producers": producers,
        "cadence": cadence,
    }
    for key, value in values.items():
        if value is not None:
            entry[key] = value
    entry.setdefault("name", chat_id)
    entry.setdefault("purpose", "")
    entry.setdefault("kind", "side")
    entry["classified_by"] = classified_by or caller
    entry["classified_at"] = datetime.now(timezone.utc).isoformat()
    if existing is None:
        entries.append(entry)
    else:
        entries[entries.index(existing)] = entry
    _save_directory(entries)
    new_revision = _directory_revision()
    _audit({"tool": "muse_directory_set", "from": caller,
            "chat_id": chat_id, "chat": entry.get("name"), "ok": True,
            "previous_revision": current_revision,
            "revision": new_revision,
            "classified_by": entry["classified_by"]})
    return {"ok": True, "entry": entry,
            "previous_revision": current_revision,
            "revision": new_revision}


# ------------------------------------------------------------ gaps


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except Exception:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


@mcp.tool()
def muse_gap_log(
        needed: str, missing: str, resolution: str = "",
        from_: str = Field(validation_alias="from")) -> dict:
    """Record a muse-mcp capability gap in gaps.jsonl.

    Purpose: give callers a durable capability-gap intake. When a
    caller needed a Muse capability this server lacks, the gap
    becomes input for the next build instead of disappearing into
    a chat transcript.

    Args:
      needed: capability or outcome the caller needed. Required.
      missing: what the server lacks or why it could not comply.
        Required.
      resolution: optional proposed fix or workaround.
      from: caller identity. Required; impersonating identities
        are refused.

    Returns: {ok, gap_id, gap} with the stored record.

    Guardrails: append-only local log plus an audit.log entry. It
    does not create an operator-backlog item or notify anyone.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    if not (needed or "").strip() or not (missing or "").strip():
        return {"ok": False,
                "error": "'needed' and 'missing' must not be empty"}
    gap = {
        "gap_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "from": caller,
        "needed": needed,
        "missing": missing,
        "resolution": resolution or None,
    }
    GAPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with GAPS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(gap, ensure_ascii=False) + "\n")
    _audit({"tool": "muse_gap_log", "from": caller,
            "gap_id": gap["gap_id"], "ok": True})
    return {"ok": True, "gap_id": gap["gap_id"], "gap": gap}


@mcp.tool()
def muse_gap_list(limit: int = 20) -> dict:
    """List recent muse-mcp capability gaps.

    Purpose: read the append-only gap intake written by
    muse_gap_log, newest first.

    Args:
      limit: maximum records to return; clamps to 1..100.

    Returns: {ok, gaps, count, total}. Missing gaps.jsonl is a
    successful empty result, not an error.

    Guardrails: read-only; returns only gap records, never audit
    or credential data.
    """
    limit = max(1, min(int(limit), 100))
    records = _read_jsonl(GAPS_FILE)
    return {"ok": True, "gaps": list(reversed(records[-limit:])),
            "count": min(len(records), limit), "total": len(records)}


async def _do_send(target: dict, stamped_text: str,
                   output_modality: str = "text") -> dict:
    async with _SEND_LOCK:
        return await _send_via_link(
            target["chat_id"], stamped_text, output_modality)


async def _send_impl(chat: str, text: str, from_: str,
                     output_modality: str = "text") -> dict:
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    if not (text or "").strip():
        return {"ok": False, "error": "'text' must not be empty"}
    if output_modality not in ("text", "voice"):
        return {"ok": False,
                "error": "output_modality must be 'text' or 'voice'"}

    target = _resolve_chat(chat)
    if target is None:
        return _refuse_unlisted("sidechat_send", caller, chat)

    try:
        result = await _do_send(
            target, _stamped(caller, text), output_modality)
    except Exception as e:
        _audit({"tool": "sidechat_send", "from": caller,
                "chat": target["name"], "chat_id": target["chat_id"],
                "ok": False, "status": None, "message_id": None,
                "error": f"{type(e).__name__}: {e}"})
        return {"ok": False,
                "error": f"send failed: {type(e).__name__}: {e}"}

    receipt = _receipt(result, target["chat_id"])
    _audit({"tool": "sidechat_send", "from": caller,
            "chat": target["name"], "chat_id": target["chat_id"],
            "ok": receipt["ok"], "status": receipt["status"],
            "message_id": receipt["message_id"],
            "output_modality": output_modality})
    return receipt


@mcp.tool()
async def sidechat_send(
        chat: str, text: str,
        from_: str = Field(validation_alias="from"),
        output_modality: str = "text") -> dict:
    """Send a message into a Muse side chat.

    chat: a chat_id from sidechat_list, or an exact directory name.
    The directory is the allowlist: an unlisted chat is refused.
    text: the message body. It is sent stamped with a one-line
    provenance prefix: "⟦via muse-mcp · from <from>⟧ " + text.
    from: caller identity (required), e.g. "nova", "health-watcher".
    output_modality: "text" (default) or "voice" (the reply becomes
    TTS-eligible; retrieve it with muse_voice_tts).

    Returns the Link API receipt {ok, status, message_id, session_id}.

    Guardrails: directory allowlist only; registry drafts and
    registry-archived directory entries are refused. Every attempt
    is audit-logged, and no message is ever sent as the operator.
    """
    return await _send_impl(chat, text, from_, output_modality)


@mcp.tool()
async def muse_chat_send(
        chat: str, text: str,
        from_: str = Field(validation_alias="from"),
        output_modality: str = "text") -> dict:
    """Alias of sidechat_send with identical behaviour.

    Purpose, arguments, return shape, provenance stamp, directory
    guardrails, and audit behaviour are exactly those documented
    for sidechat_send; the muse_ name is primary in the corrected
    scope and both names share one implementation.
    """
    return await _send_impl(chat, text, from_, output_modality)


# ------------------------------------------------------------ bridge


# The bridge wake is a pluggable dialect, selected by
# MUSE_MCP_WAKE_FORMAT: "plain" (the default for a fresh install:
# a plain-language wake any Muse-side worker can act on) or
# "cant" (Cant 0.1 REQ frames, for deployments whose bridge worker
# is briefed for that dialect). The bridge protocol itself is the
# request/result JSON files; the wake is only the doorbell, and
# either dialect names the same request file. Deployments that
# already run the Cant dialect keep it by setting the variable;
# nothing else in the server depends on the choice.
_WAKE_FORMAT = os.environ.get("MUSE_MCP_WAKE_FORMAT", "plain").lower()
if _WAKE_FORMAT not in ("plain", "cant"):
    _WAKE_FORMAT = "plain"

# Cant 0.1 op codes for the bridge tools (cant repo: PRIMER.md),
# used only when the wake format is "cant".
_CANT_BRIDGE_OPS = {
    "muse_chat_create": "CRT",
    "muse_chat_rename": "REN",
    "muse_chat_archive": "ARC",
    "muse_chat_unarchive": "UAR",
    "muse_chat_delete": "DEL",
    "muse_chat_read": "RED",
}


def _cant_wake_frame(tool: str, args: dict, correlation_id: str,
                     request_path: Path) -> str:
    """Compose the Cant REQ wake frame for one bridge operation.

    corr is the first 8 hex of the correlation_id; the full ID
    remains the queue/result file key. Body lines follow the
    primer's per-op argument shape; the full arguments (briefs,
    confirmations) stay in the referenced request file. Values are
    single-line by construction: newlines in a display name are
    flattened so the frame can never break.
    """
    corr = correlation_id[:8]
    op = _CANT_BRIDGE_OPS.get(tool, "QRY")
    def flat(value) -> str:
        return str(value).replace("\r", " ").replace("\n", " ")
    lines = [f"§CANT/1 REQ {corr}", f"op={op}"]
    if op == "CRT":
        lines.append(f"name={flat(args.get('name', ''))}")
    elif op == "REN":
        lines.append(f"chat={args.get('chat_id', '')}")
        lines.append(f"name={flat(args.get('name', ''))}")
    elif op in ("ARC", "UAR", "DEL"):
        lines.append(f"chat={args.get('chat_id', '')}")
    elif op == "RED":
        lines.append(f"chat={args.get('chat_id', '')}")
        lines.append(f"n={args.get('limit', '')}")
    lines.append(f"arg=@file:{request_path}")
    lines.append(f"§/CANT {corr}")
    return "\n".join(lines)


def _plain_wake_frame(tool: str, args: dict, correlation_id: str,
                      request_path: Path) -> str:
    """Compose the plain-language wake for one bridge operation.

    The frame names the operation, the correlation ID, and the
    request and result files. The full arguments stay in the
    referenced request file (payload by reference, as with every
    wake dialect). The bridge worker's standing brief
    (docs/bridge-brief.md) defines how it answers.
    """
    result_path = BRIDGE_RESULTS_DIR / f"{correlation_id}.json"
    lines = [
        f"muse-mcp bridge request {correlation_id}",
        f"tool: {tool}",
        f"request file: {request_path}",
        f"result file: {result_path}",
        "Execute exactly this operation with your chat tools, "
        "write the result JSON to the result file, then reply in "
        "this chat with one line: "
        f"muse-mcp bridge {correlation_id} ok",
        "If the request cannot be executed, write the result file "
        "with ok:false and an error, and reply with one line: "
        f"muse-mcp bridge {correlation_id} error",
    ]
    return "\n".join(lines)


def _wake_frame(tool: str, args: dict, correlation_id: str,
                request_path: Path) -> tuple[str, str]:
    """Compose the bridge wake in the configured dialect."""
    if _WAKE_FORMAT == "cant":
        return _cant_wake_frame(tool, args, correlation_id,
                                request_path), "cant"
    return _plain_wake_frame(tool, args, correlation_id,
                             request_path), "plain"


async def _enqueue_bridge(tool: str, args: dict, caller: str) -> dict:
    """Queue one Muse-side operation for the Bridge chat and wake it.

    The server cannot call Muse chat tools. It therefore writes a
    durable request file, sends a Link wake into the dedicated
    Bridge chat, and returns a correlation ID. The Bridge executes
    exactly the named operation and writes bridge-results/<id>.json.

    The wake is sent unstamped in the configured wake dialect
    (MUSE_MCP_WAKE_FORMAT: "plain" by default, or "cant"; see
    _wake_frame). Either way the frame is the whole message and
    names the request file; this server never parses chat
    replies — results remain the bridge-results files.
    """
    correlation_id = str(uuid.uuid4())
    BRIDGE_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    BRIDGE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    request = {
        "correlation_id": correlation_id,
        "tool": tool,
        "args": args,
        "from": caller,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    request_path = BRIDGE_QUEUE_DIR / f"{correlation_id}.json"
    tmp_path = request_path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(request, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.replace(tmp_path, request_path)
    target = next(
        (entry for entry in _load_directory()
         if entry.get("kind") == "bridge"),
        None)
    if target is None:
        error = "bridge chat is not in directory.json (kind='bridge')"
        _audit({"tool": "bridge_enqueue", "from": caller,
                "bridge_tool": tool, "correlation_id": correlation_id,
                "ok": False, "error": error})
        return {"ok": False, "status": "queued_unwoken",
                "correlation_id": correlation_id, "tool": tool,
                "error": error}
    wake_frame, wake_format = _wake_frame(tool, args, correlation_id,
                                          request_path)
    corr8 = correlation_id[:8]
    try:
        async with _SEND_LOCK:
            result = await _send_via_link(target["chat_id"], wake_frame)
        receipt = _receipt(result, target["chat_id"])
        wake_error = None
    except Exception as e:
        receipt = {"ok": False, "status": None, "message_id": None,
                   "session_id": target["chat_id"]}
        wake_error = f"{type(e).__name__}: {e}"
    audit = {"tool": "bridge_enqueue", "from": caller,
             "bridge_tool": tool, "correlation_id": correlation_id,
             "wake_format": wake_format,
             "chat": target["name"], "chat_id": target["chat_id"],
             "ok": bool(receipt.get("ok")), "status": receipt.get("status"),
             "message_id": receipt.get("message_id"),
             "error": wake_error}
    response = {"ok": bool(receipt.get("ok")),
                "status": "queued" if receipt.get("ok") else "queued_unwoken",
                "correlation_id": correlation_id, "tool": tool,
                "wake_format": wake_format,
                "bridge_chat_id": target["chat_id"],
                "wake_message_id": receipt.get("message_id"),
                "wake_error": wake_error}
    if wake_format == "cant":
        # Compat fields for callers of the Cant dialect.
        audit["cant"] = 1
        audit["cant_corr"] = corr8
        response["cant"] = 1
        response["cant_corr"] = corr8
    _audit(audit)
    return response


def _learn_bridge_result(result) -> None:
    """Record archived state from a completed Bridge result.

    Bridge lifecycle results carry their outcome explicitly:
    archive completes with inner result {"archived": true,
    "chat_id": ...} and unarchive with {"archived": false, ...}.
    Anything else teaches the registry view nothing. The record
    lands in _bridge_archived_overrides, which _registry_entries
    applies over the registry file until a synced registry
    agrees (see the note at the dict's definition).
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return
    inner = result.get("result")
    if not isinstance(inner, dict):
        return
    archived = inner.get("archived")
    chat_id = inner.get("chat_id")
    if isinstance(archived, bool) and isinstance(chat_id, str) \
            and chat_id:
        _bridge_archived_overrides[chat_id] = {
            "archived": archived,
            "correlation_id": result.get("correlation_id"),
            "completed_at": result.get("completed_at"),
        }


@mcp.tool()
def muse_request_get(correlation_id: str) -> dict:
    """Retrieve the result of a bridge-queued muse-mcp operation.

    Purpose: poll for operations executed by the Muse-side Bridge
    chat (create, rename, archive, unarchive, delete, native read).

    Args:
      correlation_id: the UUID returned by a bridge tool. Required.

    Returns one of:
      {status:"completed", result:<bridge result object>},
      {status:"pending", correlation_id} while queued/running —
        including when a result file exists but does not parse yet,
        because the Bridge writes results non-atomically and a poll
        can catch one mid-write; that case adds
        detail:"result being written" and callers should keep
        polling rather than treat it as an error, or
      {status:"unknown", ok:false} when no queue/result record has
      that ID.

    Guardrails: read-only and path-safe; correlation IDs are used
    as file stems only after rejecting path separators/traversal.
    """
    if (not correlation_id or "/" in correlation_id
            or "\\" in correlation_id or correlation_id in (".", "..")):
        return {"ok": False, "status": "unknown",
                "error": "invalid correlation_id"}
    result_path = BRIDGE_RESULTS_DIR / f"{correlation_id}.json"
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            # The Bridge writes result files non-atomically, so a
            # poll can catch one mid-write (observed in the 0.3.0
            # acceptance: one poll read invalid JSON, the re-read
            # was complete). Report pending, not an error, so
            # callers poll through the write.
            return {"ok": True, "status": "pending",
                    "correlation_id": correlation_id,
                    "detail": "result being written"}
        _learn_bridge_result(result)
        return {"ok": True, "status": "completed",
                "correlation_id": correlation_id, "result": result}
    pending_paths = [
        BRIDGE_QUEUE_DIR / f"{correlation_id}.json",
        BRIDGE_QUEUE_DIR / "done" / f"{correlation_id}.json",
    ]
    if any(path.exists() for path in pending_paths):
        return {"ok": True, "status": "pending",
                "correlation_id": correlation_id}
    return {"ok": False, "status": "unknown",
            "correlation_id": correlation_id,
            "error": "no bridge request or result has this ID"}


@mcp.tool()
async def muse_chat_create(
        name: str, brief: str,
        from_: str = Field(validation_alias="from")) -> dict:
    """Queue creation of a new Muse side chat through the Bridge.

    Purpose: create a chat with a name and standing brief, which
    only a Muse-side agent can do. The Bridge creates it fresh,
    posts the brief as its standing instructions, and returns the
    new chat_id in the bridge result.

    Args:
      name: new chat name. Required.
      brief: standing instructions for the new chat. Required.
      from: caller identity. Required and impersonation-refused.

    Returns: {status:"queued", correlation_id, tool}. Poll
    muse_request_get(correlation_id) for {chat_id, name}.

    Guardrails: creation is asynchronous. The new chat is not
    sendable until it is also classified in directory.json with
    muse_directory_set.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    if not (name or "").strip() or not (brief or "").strip():
        return {"ok": False, "error": "'name' and 'brief' are required"}
    return await _enqueue_bridge(
        "muse_chat_create", {"name": name, "brief": brief}, caller)


@mcp.tool()
async def muse_chat_rename(
        chat: str, name: str,
        from_: str = Field(validation_alias="from")) -> dict:
    """Queue a chat rename through the Bridge.

    Purpose: change a chat's display name using Muse-side chat
    tools. Args are the directory/registry chat ID or exact name,
    the new name, and required caller identity in `from`.

    Returns a queued correlation ID; poll muse_request_get for the
    Bridge result. Guardrails: the chat must be directory- or
    registry-known; unknown chats are refused before queueing.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    if not (name or "").strip():
        return {"ok": False, "error": "'name' must not be empty"}
    target = _resolve_known_chat(chat)
    if target is None:
        return _refuse_unlisted("muse_chat_rename", caller, chat)
    return await _enqueue_bridge(
        "muse_chat_rename",
        {"chat_id": target["chat_id"], "name": name}, caller)


@mcp.tool()
async def muse_chat_archive(
        chat: str, from_: str = Field(validation_alias="from")) -> dict:
    """Queue archival of a known chat through the Bridge.

    Purpose: reversibly remove a side chat from the active list.
    Args are a directory/registry chat ID or exact name and the
    required caller identity in `from`.

    Returns a queued correlation ID; poll muse_request_get.
    Guardrails: the main chat is refused server-side. Archival is
    executed by the Bridge, not by this tool directly.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    target = _resolve_known_chat(chat)
    if target is None:
        return _refuse_unlisted("muse_chat_archive", caller, chat)
    if _is_main_entry(target):
        return {"ok": False, "error": "refused: the main chat cannot be archived"}
    return await _enqueue_bridge(
        "muse_chat_archive", {"chat_id": target["chat_id"]}, caller)


@mcp.tool()
async def muse_chat_unarchive(
        chat: str, from_: str = Field(validation_alias="from")) -> dict:
    """Queue restoration of an archived chat through the Bridge.

    Purpose: return a known archived side chat to the active list.
    Args are a directory/registry chat ID or exact name and the
    required caller identity in `from`.

    Returns a queued correlation ID; poll muse_request_get.
    Guardrails: unknown chats are refused before queueing; the
    Bridge performs the actual Muse-side unarchive.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    target = _resolve_known_chat(chat)
    if target is None:
        return _refuse_unlisted("muse_chat_unarchive", caller, chat)
    return await _enqueue_bridge(
        "muse_chat_unarchive", {"chat_id": target["chat_id"]}, caller)


@mcp.tool()
async def muse_chat_read(
        chat: str, limit: int = 5,
        from_: str = Field(validation_alias="from")) -> dict:
    """Queue a native-turn read of a chat through the Bridge.

    Purpose: retrieve a chat's actual conversation turns (sender,
    text, and message IDs, newest first) using Muse-side chat
    tools. This is deliberately distinct from muse_chat_history:
    history returns Link event envelopes, while this returns the
    chat's native turns via the Bridge.

    Args:
      chat: directory/registry chat ID or exact name.
      limit: number of turns to request; clamps to 1..20.
      from: caller identity. Required and impersonation-refused.

    Returns a queued correlation ID; poll muse_request_get for the
    structured turns. Guardrails: unknown chats are refused; the
    read is asynchronous because only the Bridge can execute it.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    target = _resolve_known_chat(chat)
    if target is None:
        return _refuse_unlisted("muse_chat_read", caller, chat)
    limit = max(1, min(int(limit), 20))
    return await _enqueue_bridge(
        "muse_chat_read",
        {"chat_id": target["chat_id"], "limit": limit}, caller)


@mcp.tool()
async def muse_chat_relay(
        from_chat: str, to_chat: str, text: str,
        from_: str = Field(validation_alias="from")) -> dict:
    """Relay caller-supplied text from one listed chat to another.

    Purpose: move a statement, excerpt, or instruction between two
    Muse chats over the Link with provenance naming the source
    chat, destination chat, and relaying caller. This tool does not
    read the source chat; the caller supplies the exact text.

    Args:
      from_chat: source directory chat ID or exact name.
      to_chat: destination directory chat ID or exact name.
      text: exact text to relay. Required.
      from: relaying caller identity. Required and
        impersonation-refused.

    Returns: the destination Link receipt plus source/destination
    names and IDs. Guardrails: both chats must be classified,
    non-archived directory entries; the sent body is stamped with
    the standard provenance prefix and an explicit relay line.
    The audit record names both ends.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    if not (text or "").strip():
        return {"ok": False, "error": "'text' must not be empty"}
    source = _resolve_chat(from_chat)
    if source is None:
        return _refuse_unlisted("muse_chat_relay", caller, from_chat)
    destination = _resolve_chat(to_chat)
    if destination is None:
        return _refuse_unlisted("muse_chat_relay", caller, to_chat)
    body = (f"Relay from {source['name']} ({source['chat_id']}), "
            f"relayed by {caller}: {text}")
    try:
        result = await _do_send(destination, _stamped(caller, body))
    except Exception as e:
        _audit({"tool": "muse_chat_relay", "from": caller,
                "from_chat": source["name"],
                "from_chat_id": source["chat_id"],
                "chat": destination["name"],
                "chat_id": destination["chat_id"], "ok": False,
                "error": f"{type(e).__name__}: {e}"})
        return {"ok": False,
                "error": f"relay failed: {type(e).__name__}: {e}"}
    receipt = _receipt(result, destination["chat_id"])
    _audit({"tool": "muse_chat_relay", "from": caller,
            "from_chat": source["name"],
            "from_chat_id": source["chat_id"],
            "chat": destination["name"],
            "chat_id": destination["chat_id"],
            "ok": receipt["ok"], "status": receipt["status"],
            "message_id": receipt["message_id"]})
    return {**receipt, "from_chat": source["name"],
            "from_chat_id": source["chat_id"],
            "to_chat": destination["name"],
            "to_chat_id": destination["chat_id"]}


def _last_audit_activity(chat_id: str) -> dict | None:
    try:
        lines = AUDIT_LOG.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except Exception:
            continue
        if record.get("chat_id") == chat_id:
            return record
    return None


@mcp.tool()
def muse_chat_delete_preview(
        chat: str, from_: str = Field(validation_alias="from")) -> dict:
    """Preview permanent deletion of a chat and issue a token.

    Purpose: make the irreversible Bridge deletion a two-step
    operation. This first step changes nothing; it identifies the
    chat and returns a single-use preview_token valid for 10
    minutes. Pass that exact token to muse_chat_delete.

    Args:
      chat: directory/registry chat ID or exact name.
      from: caller identity. Required and impersonation-refused.

    Returns: {ok, chat_id, name, kind, source, registered,
    sendable, archived, last_audit_activity, preview_token,
    expires_at}. The main chat is refused at this step and at
    confirmation, always.

    Guardrails: a preview token is bound to one chat, expires,
    and is consumed by one confirmation attempt that passes token
    validation. No deletion is queued by this tool.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    target = _resolve_known_chat(chat)
    if target is None:
        return _refuse_unlisted("muse_chat_delete_preview", caller, chat)
    if _is_main_entry(target):
        _audit({"tool": "muse_chat_delete_preview", "from": caller,
                "chat": target["name"], "chat_id": target["chat_id"],
                "ok": False, "error": "main chat deletion refused"})
        return {"ok": False,
                "error": "refused: the main chat can never be deleted"}
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = time.time() + _DELETE_PREVIEW_TTL_SECONDS
    _delete_previews[token_hash] = {
        "chat_id": target["chat_id"],
        "expires_at": expires,
        "from": caller,
    }
    preview = {
        "ok": True,
        "chat_id": target["chat_id"],
        "name": target.get("name"),
        "kind": target.get("kind"),
        "source": target.get("source"),
        "registered": target.get("source") == "directory"
        or bool(target.get("registered")),
        "sendable": bool(target.get("sendable")),
        "archived": bool(target.get("archived")),
        "last_audit_activity": _last_audit_activity(target["chat_id"]),
        "preview_token": token,
        "expires_at": datetime.fromtimestamp(
            expires, timezone.utc).isoformat(),
    }
    _audit({"tool": "muse_chat_delete_preview", "from": caller,
            "chat": target.get("name"), "chat_id": target["chat_id"],
            "ok": True, "preview_token_issued": True})
    return preview


@mcp.tool()
async def muse_chat_delete(
        chat: str, preview_token: str,
        from_: str = Field(validation_alias="from")) -> dict:
    """Queue permanent deletion after a valid deletion preview.

    Purpose: execute the second, confirming step of chat deletion
    through the Bridge. The Bridge request carries confirmed:true;
    the Bridge itself must refuse any deletion request without it.

    Args:
      chat: the same directory/registry chat ID or exact name used
        for muse_chat_delete_preview.
      preview_token: the single-use, unexpired token from preview.
        Required. Missing, mismatched, expired, and reused tokens
        are refused before anything is queued.
      from: caller identity. Required and impersonation-refused.

    Returns a queued correlation ID; poll muse_request_get for the
    Bridge result. Guardrails: main chat refused always; the token
    is consumed when validation succeeds; permanent deletion is
    executed by the Bridge, not directly by this server.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    target = _resolve_known_chat(chat)
    if target is None:
        return _refuse_unlisted("muse_chat_delete", caller, chat)
    if _is_main_entry(target):
        return {"ok": False,
                "error": "refused: the main chat can never be deleted"}
    if not preview_token:
        return {"ok": False,
                "error": "refused: preview_token is required; call "
                         "muse_chat_delete_preview first"}
    token_hash = hashlib.sha256(preview_token.encode()).hexdigest()
    preview = _delete_previews.get(token_hash)
    if preview is None:
        _audit({"tool": "muse_chat_delete", "from": caller,
                "chat": target.get("name"),
                "chat_id": target["chat_id"], "ok": False,
                "error": "invalid or already-used preview_token"})
        return {"ok": False,
                "error": "refused: invalid or already-used preview_token"}
    if preview["expires_at"] < time.time():
        _delete_previews.pop(token_hash, None)
        return {"ok": False,
                "error": "refused: preview_token has expired"}
    if preview["chat_id"] != target["chat_id"]:
        return {"ok": False,
                "error": "refused: preview_token was issued for another chat"}
    _delete_previews.pop(token_hash, None)
    return await _enqueue_bridge(
        "muse_chat_delete",
        {"chat_id": target["chat_id"], "confirmed": True}, caller)


@mcp.tool()
async def muse_link_status() -> dict:
    """Report Link/server health without exposing secrets.

    Purpose: establish whether this host is paired, which Muse VM
    it can reach, and which directory revision it is serving.

    Args: none.

    Returns: service/version/host, paired boolean, node_id, SDK
    version, VM fetch status and leased VMs (vm_id, vm_name,
    is_default), and directory count/revision/updated time.

    Guardrails: token values are never returned. Pairing presence
    is a boolean only. This is local/Link health, not chat status.
    """
    from musegadget import __version__ as mg_version

    pairing, ident = _pairing_and_identity()
    node_id = ident.node_id
    paired = bool(pairing and pairing.get("access_token"))
    vms: list[dict] = []
    vm_status = None
    if paired:
        try:
            raw_vms, vm_status = await _fetch_vms(pairing)
            vms = [
                {"vm_id": vm.get("vm_id"),
                 "vm_name": vm.get("vm_name"),
                 "is_default": bool(vm.get("is_default"))}
                for vm in raw_vms
            ]
        except Exception as e:
            vm_status = f"{type(e).__name__}: {e}"
    entries = _load_directory()
    result = {
        "ok": True,
        "service": "muse-mcp",
        "service_version": SERVICE_VERSION,
        "host": socket.gethostname(),
        "paired": paired,
        "node_id": node_id,
        "sdk_version": mg_version,
        "vm_fetch_status": vm_status,
        "vms": vms,
        "directory": {
            "count": len(entries),
            "revision": _directory_revision(),
            "updated": _directory_updated(),
        },
    }
    _audit({"tool": "muse_link_status", "ok": True,
            "paired": paired, "node_id": node_id,
            "host": result["host"]})
    return result


async def _exists_impl(chat: str, from_: str) -> dict:
    """Check whether Muse has a chat with this directory entry's ID
    (Link open_side_chat: 2xx = exists, 404 = not). The directory
    allowlist applies. Note the asymmetry: a listed chat that does
    not exist yet would be created by the first send_chat to it."""
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    target = _resolve_chat(chat)
    if target is None:
        return _refuse_unlisted("muse_chat_exists", caller, chat)
    try:
        async with _link_session() as session:
            side = await session.open_side_chat(target["chat_id"])
        exists = bool(side.exists)
        error = None
    except Exception as e:
        exists, error = None, f"{type(e).__name__}: {e}"
    _audit({"tool": "muse_chat_exists", "from": caller,
            "chat": target["name"], "chat_id": target["chat_id"],
            "ok": error is None, "exists": exists, "error": error})
    if error:
        return {"ok": False, "chat": target["name"],
                "chat_id": target["chat_id"], "error": error}
    return {"ok": True, "chat": target["name"],
            "chat_id": target["chat_id"], "exists": exists}


@mcp.tool()
async def muse_chat_exists(
        chat: str,
        from_: str = Field(validation_alias="from")) -> dict:
    """Check whether Muse has a chat with a classified chat's ID.

    Purpose: perform the Link open_side_chat existence check.
    Args are a directory chat ID or exact name and required caller
    identity in `from`. Returns {ok, chat, chat_id, exists} or an
    error object. Guardrails: directory allowlist only; drafts and
    registry-archived entries are refused. Existence is the only
    per-chat datum this Link call returns: no title, metadata,
    unread state, or activity is included.
    """
    return await _exists_impl(chat, from_)


@mcp.tool()
async def sidechat_exists(
        chat: str,
        from_: str = Field(validation_alias="from")) -> dict:
    """Alias of muse_chat_exists.

    Purpose, arguments, return shape, and directory guardrails are
    identical to muse_chat_exists; both names share one
    implementation.
    """
    return await _exists_impl(chat, from_)


async def _ask_impl(chat: str, text: str, from_: str,
                    timeout_s: int = 180) -> dict:
    """Ask a side chat a question and return its agent's reply.

    The conversational tool: creates an SDK MuseTurn BEFORE sending,
    subscribes to the chat's event stream as a background task
    feeding every event into the turn, sends the provenance-stamped
    text via send_chat, acknowledges the turn with the send
    response, then waits until the turn completes or timeout_s
    (clamped 10..300) elapses. Returns reply_text, reply_message_id,
    the send message_id (the correlation id), agent activity codes
    seen, the terminal task status, and timed_out.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    if not (text or "").strip():
        return {"ok": False, "error": "'text' must not be empty"}
    timeout_s = max(10, min(int(timeout_s), 300))
    target = _resolve_chat(chat)
    if target is None:
        return _refuse_unlisted("muse_chat_ask", caller, chat)
    chat_id = target["chat_id"]

    from musegadget.muse_turn import AgentStatus, MuseTurn, TaskFinished

    turn = MuseTurn(chat_id)
    raw_events: list[dict] = []
    turn_events: list = []
    ack_error = None
    send_result: dict = {}
    timed_out = False
    completion = "not_started"
    send_issued = False
    bundle_seen: set[str] = set()
    try:
        async with _link_session() as session:

            async def _pump():
                try:
                    async for event in session.subscribe_chat(chat_id):
                        raw_events.append(event)
                        if event.get("event") in (
                                "agent.status", "task.status",
                                "approvals.snapshot"):
                            bundle_seen.add(event["event"])
                        if not send_issued:
                            # Never feed pre-send events to the
                            # fresh turn. The subscribe-time state
                            # bundle restates the PREVIOUS turn: its
                            # parentless terminal task.status is
                            # accepted by a shared-chat MuseTurn
                            # (owns_chat=False) and marks this turn
                            # task_finished, so the wait loop below
                            # completes vacuously with no reply
                            # (smoke SMOKE-20261027-1 T2-3: 3 of 5
                            # asks returned ok:false/empty in <10s
                            # with task_status already "completed").
                            # Only events after our send can belong
                            # to this turn; pre-ack ones are still
                            # held by MuseTurn.feed itself.
                            continue
                        try:
                            turn_events.extend(turn.feed(event))
                        except Exception:
                            pass
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass

            pump = asyncio.create_task(_pump())
            try:
                try:
                    await asyncio.wait_for(
                        session.chat_subscribed.wait(), 10)
                except asyncio.TimeoutError:
                    pass
                # Wait (bounded) for the subscribe-time bundle to
                # finish arriving BEFORE sending. chat_subscribed
                # opens before the bundle lands (probe, SMOKE-
                # 20261027-1 fix pass: bundle trio complete ~75 ms
                # after subscribe-open), so the pre-send gate alone
                # races it: a bundle event arriving after the send
                # is fed, and its parentless terminal task.status —
                # indistinguishable from the new turn's, same
                # task_id, no parent linkage — finishes the fresh
                # turn vacuously. With the bundle fully arrived,
                # every bundle event is pre-send and gated out.
                bundle_deadline = time.monotonic() + 8
                while time.monotonic() < bundle_deadline:
                    if {"agent.status", "task.status",
                            "approvals.snapshot"} <= bundle_seen:
                        break
                    await asyncio.sleep(0.05)
                send_issued = True
                send_result = await session.send_chat(
                    _stamped(caller, text), session_id=chat_id)
                ack_arg = send_result.get("response")
                if not isinstance(ack_arg, dict):
                    ack_arg = send_result
                try:
                    turn_events.extend(turn.acknowledge(ack_arg))
                except Exception as e:
                    ack_error = f"{type(e).__name__}: {e}"
                deadline = time.monotonic() + timeout_s
                completion = "timeout"
                while True:  # noqa: B007 - completion set on every exit
                    now = time.monotonic()
                    if (turn.acknowledged and turn.complete(now)
                            and (turn.replies or turn.task_finished)):
                        completion = "turn_complete"
                        break
                    # Pragmatic early exit (labelled in the result):
                    # MuseTurn.complete() waits for the whole task
                    # to go terminal, but an agentic chat may keep
                    # working long after answering. A finished reply
                    # plus 8s of turn silence is an answer.
                    if (turn.replies
                            and all(r.done
                                    for r in turn.replies.values())
                            and now - turn.last_activity >= 8.0):
                        completion = "reply_done_quiet"
                        break
                    if now >= deadline:
                        timed_out = True
                        break
                    await asyncio.sleep(0.25)
            finally:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
    except Exception as e:
        _audit({"tool": "muse_chat_ask", "from": caller,
                "chat": target["name"], "chat_id": chat_id,
                "ok": False, "error": f"{type(e).__name__}: {e}"})
        return {"ok": False, "chat": target["name"],
                "chat_id": chat_id,
                "error": f"ask failed: {type(e).__name__}: {e}"}

    replies = list(turn.replies.values())
    reply_text = "\n\n".join(r.text for r in replies if r.text)
    reply_message_id = replies[-1].message_id if replies else None
    agent_statuses: list[str] = []
    for ev in turn_events:
        if isinstance(ev, AgentStatus) and ev.activity_code:
            if ev.activity_code not in agent_statuses:
                agent_statuses.append(ev.activity_code)
    # MuseTurn is constructed as a shared-chat turn (owns_chat
    # defaults false), and the SDK deliberately does not emit an
    # AgentStatus object for a live agent.status event that carries
    # session_id but no message/parent linkage. The raw subscription
    # events are therefore the reliable source for activity codes;
    # mirror the raw task.status fallback below. Do not switch the
    # turn to owns_chat=True: that changes acknowledgement semantics.
    last_raw_agent_code = None
    for raw in raw_events:
        if raw.get("event") != "agent.status":
            continue
        payload = raw.get("payload")
        if isinstance(payload, dict):
            code = payload.get("activity_code")
            if code:
                last_raw_agent_code = code
                if code not in agent_statuses:
                    agent_statuses.append(code)
    agent_status = last_raw_agent_code or getattr(
        turn, "activity_code", None)
    if agent_status is None and agent_statuses:
        agent_status = agent_statuses[-1]
    task_status = None
    for raw in raw_events:
        if raw.get("event") == "task.status":
            payload = raw.get("payload")
            if isinstance(payload, dict) and payload.get("status"):
                task_status = payload["status"]
    if task_status is None and any(
            isinstance(ev, TaskFinished) for ev in turn_events):
        task_status = "finished"
    send_message_id = _dig(send_result, "message_id")
    result = {
        "ok": bool(reply_text),
        "chat": target["name"],
        "chat_id": chat_id,
        "correlation_id": send_message_id,
        "send_message_id": send_message_id,
        "reply_text": reply_text,
        "reply_message_id": reply_message_id,
        "agent_statuses": agent_statuses,
        "agent_status": agent_status,
        "task_status": task_status,
        "timed_out": timed_out,
        "completion": completion,
        "ack_error": ack_error,
    }
    _audit({"tool": "muse_chat_ask", "from": caller,
            "chat": target["name"], "chat_id": chat_id,
            "ok": result["ok"], "message_id": send_message_id,
            "reply_message_id": reply_message_id,
            "timed_out": timed_out, "task_status": task_status})
    return result


@mcp.tool()
async def muse_chat_ask(
        chat: str, text: str,
        from_: str = Field(validation_alias="from"),
        timeout_s: int = 180) -> dict:
    """Ask a classified chat a question and return its reply.

    Purpose: send a provenance-stamped message, then follow that
    thread's live turn stream until the reply is complete. Only a
    message sent after this call counts.

    Args:
      chat: directory chat ID or exact name.
      text: question/message text.
      timeout_s: optional wait budget, default 180, clamped by
        the implementation.
      from: required caller identity; impersonation-refused.

    Returns: {ok, correlation/message IDs, reply text,
    reply_message_id, completion mode, agent_statuses,
    agent_status, task_status, timed_out}. Completion is normally
    turn_complete (the canonical string): the turn's task reached
    a terminal status with its reply. reply_done_quiet is the
    labelled early exit: reply done plus 8 seconds without new
    turn activity, because agentic work may continue after the
    answer.

    Guardrails: directory allowlist only. agent_statuses combines
    SDK AgentStatus objects with a raw agent.status fallback; see
    docs/tools.md for the precise source limitation.
    """
    return await _ask_impl(chat, text, from_, timeout_s)


@mcp.tool()
async def sidechat_ask(
        chat: str, text: str,
        from_: str = Field(validation_alias="from"),
        timeout_s: int = 180) -> dict:
    """Alias of muse_chat_ask.

    Purpose, arguments, return shape, completion semantics, and
    directory guardrails are identical to muse_chat_ask.
    """
    return await _ask_impl(chat, text, from_, timeout_s)


async def _tunnel_request(session, method: str, path: str,
                          body_obj: dict | None = None) -> dict:
    from musegadget.noise import Header

    body = (json.dumps(body_obj).encode() if body_obj is not None
            else b"")
    headers = [Header("Content-Type", "application/json")] if body \
        else None
    return await session.request(method, path, body, headers=headers)


@mcp.tool()
async def muse_chat_history(
        chat: str | None = None, limit: int = 20,
        from_: str = Field(validation_alias="from")) -> dict:
    """Read canonical chat history through the Link tunnel.

    Purpose: retrieve the transcript events for one classified
    chat, or the VM default/main history when chat is omitted.

    Args:
      chat: optional directory chat ID or exact name. When given,
        GET /chat/history receives session_id=<chat_id>; session
        scoping was live-verified in the 0.2.0 acceptance pass.
      limit: newest-event count, default 20, clamped 1..50.
      from: required caller identity; impersonation-refused.

    Returns: {ok, status, scoped_to_chat, response}, where response
    is exactly what the VM returned. Known limitation: history
    returns only canonical transcript event names message.user,
    message.assistant, and delta.presentation, as the newest N in
    chronological order. Live-stream names such as agent.status,
    task.status, approvals.snapshot, and tasks.updated come from
    subscriptions, not history.

    Guardrails: a named chat must be classified and not suspended
    as registry-archived. The raw response is not rewritten.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    limit = max(1, min(int(limit), 50))
    target = None
    path = f"/chat/history?limit={limit}"
    if chat:
        target = _resolve_chat(chat)
        if target is None:
            return _refuse_unlisted("muse_chat_history", caller, chat)
        path += f"&session_id={target['chat_id']}"
    try:
        async with _link_session() as session:
            result = await _tunnel_request(session, "GET", path)
    except Exception as e:
        _audit({"tool": "muse_chat_history", "from": caller,
                "chat": target["name"] if target else None,
                "chat_id": target["chat_id"] if target else None,
                "ok": False, "error": f"{type(e).__name__}: {e}"})
        return {"ok": False,
                "error": f"history failed: {type(e).__name__}: {e}"}
    _audit({"tool": "muse_chat_history", "from": caller,
            "chat": target["name"] if target else None,
            "chat_id": target["chat_id"] if target else None,
            "ok": bool(result.get("ok")), "status": result.get("status"),
            "limit": limit, "scoped": target is not None})
    return {"ok": bool(result.get("ok")), "status": result.get("status"),
            "scoped_to_chat": target["name"] if target else None,
            "response": result.get("response")}


def _truncate_event(obj, depth: int = 0):
    if depth > 6:
        return obj
    if isinstance(obj, str):
        return obj if len(obj) <= 500 else obj[:500] + "…[truncated]"
    if isinstance(obj, dict):
        return {k: _truncate_event(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_truncate_event(v, depth + 1) for v in obj]
    return obj


@mcp.tool()
async def muse_chat_events(
        chat: str, seconds: int = 10,
        from_: str = Field(validation_alias="from")) -> dict:
    """Watch a classified chat's live Link event stream briefly.

    Purpose: observe live activity and reply lifecycle events for
    a bounded window. Args are a directory chat ID/exact name,
    seconds (default 10, clamped 1..30), and required caller
    identity in `from`.

    Returns: {ok, chat, chat_id, seconds, event_count, counts,
    events, error}; string fields are truncated to 500 chars.

    Guardrails and limitations: directory allowlist only. Events
    are live-stream only: subscribing offers no backlog, so a
    quiet window returns zero events. The subscribe-time state
    bundle (the first agent.status, task.status, and
    approvals.snapshot on the subscription) is current state,
    not events, and is excluded — the same inferred baseline
    muse_watch_digest separates — as is node.* global traffic.
    The stream can carry agent.status, task.status, reply deltas,
    approvals.snapshot, and tasks.updated. tasks.updated carries
    no session_id and must not be attributed to the subscribed
    chat.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    seconds = max(1, min(int(seconds), 30))
    target = _resolve_chat(chat)
    if target is None:
        return _refuse_unlisted("muse_chat_events", caller, chat)
    chat_id = target["chat_id"]
    events: list[dict] = []
    error = None
    try:
        async with _link_session() as session:

            async def _pump():
                async for event in session.subscribe_chat(chat_id):
                    events.append(event)

            pump = asyncio.create_task(_pump())
            try:
                await asyncio.wait_for(pump, seconds)
            except asyncio.TimeoutError:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    # The subscribe-time state bundle is not events (smoke
    # SMOKE-20261027-1 T2-14: an idle window returned the bundle
    # as 4 events instead of the documented 0). Mirror the
    # digest's inferred baseline: drop the first agent.status,
    # task.status, and approvals.snapshot. Global stream traffic
    # is likewise not this chat's events — the digest excludes
    # tasks.updated, sessions.updated, and node.* on the same
    # grounds, and a fix-pass re-run caught a tasks.updated job
    # notification landing in a quiet window.
    kept: list[dict] = []
    baseline_seen: set[str] = set()
    for ev in events:
        name = ev.get("event") or ""
        if name.startswith("node.") or name in (
                "tasks.updated", "sessions.updated"):
            continue
        if name in ("agent.status", "task.status",
                    "approvals.snapshot") \
                and name not in baseline_seen:
            baseline_seen.add(name)
            continue
        kept.append(ev)
    events = kept
    counts = dict(Counter(
        (ev.get("event") or "?") for ev in events))
    _audit({"tool": "muse_chat_events", "from": caller,
            "chat": target["name"], "chat_id": chat_id,
            "ok": error is None, "seconds": seconds,
            "event_count": len(events), "counts": counts,
            "error": error})
    return {"ok": error is None, "chat": target["name"],
            "chat_id": chat_id, "seconds": seconds,
            "event_count": len(events), "counts": counts,
            "events": [_truncate_event(ev) for ev in events],
            "error": error}


@mcp.tool()
async def muse_chat_send_voice(
        chat: str, wav_base64: str, message: str = "",
        from_: str = Field(validation_alias="from")) -> dict:
    """Send a WAV voice note into a classified chat.

    Purpose: deliver caller-supplied audio through Link
    send_voice. Args are a directory chat ID/exact name,
    wav_base64, optional accompanying message text, and required
    caller identity in `from`. Returns the Link receipt.

    Guardrails: decoded audio must be non-empty and no larger
    than 2 MiB; invalid base64 is refused. The provenance stamp
    rides in the accompanying message text, not in the audio.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    target = _resolve_chat(chat)
    if target is None:
        return _refuse_unlisted("muse_chat_send_voice", caller, chat)
    try:
        wav = base64.b64decode(wav_base64 or "", validate=True)
    except Exception:
        return {"ok": False, "error": "wav_base64 is not valid base64"}
    if not wav:
        return {"ok": False, "error": "wav audio is empty"}
    if len(wav) > 2 * 1024 * 1024:
        return {"ok": False,
                "error": f"wav too large: {len(wav)} bytes > 2 MiB"}
    stamped = _stamped(caller, message or "Voice note")
    try:
        async with _link_session() as session:
            result = await session.send_voice(
                wav, session_id=target["chat_id"], message=stamped)
    except Exception as e:
        _audit({"tool": "muse_chat_send_voice", "from": caller,
                "chat": target["name"], "chat_id": target["chat_id"],
                "ok": False, "error": f"{type(e).__name__}: {e}"})
        return {"ok": False,
                "error": f"send_voice failed: {type(e).__name__}: {e}"}
    receipt = _receipt(result, target["chat_id"])
    _audit({"tool": "muse_chat_send_voice", "from": caller,
            "chat": target["name"], "chat_id": target["chat_id"],
            "ok": receipt["ok"], "status": receipt["status"],
            "message_id": receipt["message_id"], "wav_bytes": len(wav)})
    return receipt


@mcp.tool()
async def muse_voice_tts(
        message_id: str,
        from_: str = Field(validation_alias="from")) -> dict:
    """Fetch TTS audio for a voice-modality assistant reply.

    Purpose: retrieve the spoken form of a reply Muse produced
    with output_modality="voice" through Link stream_tts.

    Args:
      message_id: the assistant reply message ID (find it with
        muse_chat_ask or canonical history after a voice send).
      from: required caller identity; impersonation-refused.

    Returns: {ok, mime:"audio/mpeg", bytes, data_base64}.
    Guardrails: streams over 8 MiB are refused. Text-only replies
    have no TTS stream and are expected to fail.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    if not (message_id or "").strip():
        return {"ok": False, "error": "message_id is required"}
    chunks: list[bytes] = []
    total = 0
    try:
        async with _link_session() as session:
            async for chunk in session.stream_tts(message_id):
                total += len(chunk)
                if total > 8 * 1024 * 1024:
                    _audit({"tool": "muse_voice_tts", "from": caller,
                            "message_id": message_id, "ok": False,
                            "error": "tts stream exceeded 8 MiB"})
                    return {"ok": False,
                            "error": "tts stream exceeded 8 MiB; refused"}
                chunks.append(chunk)
    except Exception as e:
        _audit({"tool": "muse_voice_tts", "from": caller,
                "message_id": message_id, "ok": False,
                "error": f"{type(e).__name__}: {e}"})
        return {"ok": False,
                "error": f"tts failed: {type(e).__name__}: {e}"}
    audio = b"".join(chunks)
    _audit({"tool": "muse_voice_tts", "from": caller,
            "message_id": message_id, "ok": True, "bytes": len(audio)})
    return {"ok": True, "mime": "audio/mpeg", "bytes": len(audio),
            "data_base64": base64.b64encode(audio).decode("ascii")}


@mcp.tool()
async def muse_link_request(
        method: str, path: str, body_json: dict | None = None,
        from_: str = Field(validation_alias="from")) -> dict:
    """Make a generic request through the Link HTTP tunnel.

    Purpose: reach VM endpoints the typed wrappers do not model,
    without widening the server into an arbitrary network proxy.

    Args:
      method: GET or POST only.
      path: must start with /chat/, /voice/, /api/voice/, or /fs/.
      body_json: optional JSON request body.
      from: required caller identity; impersonation-refused.

    Returns: {ok, status, response} exactly as tunnelled.
    Guardrails: off-allowlist paths and other methods are
    refused. The audit log records method, path, outcome, and
    body size only, never body contents.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    method = (method or "").upper()
    if method not in ("GET", "POST"):
        return {"ok": False, "error": "method must be GET or POST"}
    if not any((path or "").startswith(p) for p in _LINK_PATH_PREFIXES):
        _audit({"tool": "muse_link_request", "from": caller,
                "method": method, "path": path, "ok": False,
                "error": "path outside allowlist"})
        return {"ok": False,
                "error": (f"refused: path {path!r} is outside the "
                          f"allowlist {list(_LINK_PATH_PREFIXES)}")}
    body_bytes = (len(json.dumps(body_json).encode())
                  if body_json is not None else 0)
    try:
        async with _link_session() as session:
            result = await _tunnel_request(session, method, path,
                                           body_obj=body_json)
    except Exception as e:
        _audit({"tool": "muse_link_request", "from": caller,
                "method": method, "path": path, "ok": False,
                "body_bytes": body_bytes,
                "error": f"{type(e).__name__}: {e}"})
        return {"ok": False,
                "error": f"request failed: {type(e).__name__}: {e}"}
    _audit({"tool": "muse_link_request", "from": caller,
            "method": method, "path": path,
            "ok": bool(result.get("ok")), "status": result.get("status"),
            "body_bytes": body_bytes})
    return {"ok": bool(result.get("ok")), "status": result.get("status"),
            "response": result.get("response")}


@mcp.tool()
async def muse_fs_read(
        path: str, offset: int = 0, length: int = 4096,
        from_: str = Field(validation_alias="from")) -> dict:
    """Read a file on the Muse VM through POST /fs/read.

    Purpose: fetch bytes from a home-relative VM path (these are
    paths on the Muse VM, not on this server host).

    Args:
      path: home-relative VM path.
      offset: byte offset, default 0, minimum 0.
      length: byte count, default 4096, clamped 1..65536.
      from: required caller identity; impersonation-refused.

    Returns: {ok, status, response}; a successful response carries
    data_base64, optional UTF-8 text, and eof. Read until eof, not
    a guessed size.

    Known limitation: the tool is implemented to the documented
    shape but currently blocked on Meta backend credential
    entitlement. The VM returns HTTP 403 with the exact error
    "route not allowed for this credential's scopes"; no
    SDK-documented app toggle, scope request, or re-pair path is
    known.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    length = max(1, min(int(length), 65536))
    offset = max(0, int(offset))
    try:
        async with _link_session() as session:
            result = await _tunnel_request(
                session, "POST", "/fs/read",
                {"path": path, "offset": offset, "len": length})
    except Exception as e:
        _audit({"tool": "muse_fs_read", "from": caller, "path": path,
                "ok": False, "error": f"{type(e).__name__}: {e}"})
        return {"ok": False,
                "error": f"fs_read failed: {type(e).__name__}: {e}"}
    _audit({"tool": "muse_fs_read", "from": caller, "path": path,
            "offset": offset, "len": length,
            "ok": bool(result.get("ok")), "status": result.get("status")})
    return {"ok": bool(result.get("ok")), "status": result.get("status"),
            "response": result.get("response")}


def _fs_write_refusal(path: str) -> str | None:
    parts = [p for p in (path or "").replace("\\", "/").split("/") if p]
    if any(p in _FS_REFUSED_SEGMENTS for p in parts):
        return "path is under a refused segment (.ssh/.aws/.gnupg)"
    lowered = (path or "").lower()
    for word in _FS_REFUSED_WORDS:
        if word in lowered:
            return f"path contains refused word {word!r}"
    return None


@mcp.tool()
async def muse_fs_write(
        path: str, text: str, overwrite: bool = False,
        append: bool = False, create_parent: bool = True,
        from_: str = Field(validation_alias="from")) -> dict:
    """Write a text file on the Muse VM through POST /fs/write.

    Purpose: write caller-supplied text to a home-relative VM
    path using the documented exact request shape.

    Args:
      path: home-relative VM path.
      text: text content.
      overwrite, append: write-mode booleans (defaults false).
      create_parent: create parent directories, default true.
      from: required caller identity; impersonation-refused.

    Returns: {ok, status, response}. Guardrails: paths under
    .ssh, .aws, or .gnupg and paths containing credential,
    secret, or token (case-insensitive) are refused locally.
    Every attempt is audit-logged with path and byte count.

    Known limitation: implemented to spec but currently blocked
    on Meta backend credential entitlement. The VM returns HTTP
    403 with the exact error "route not allowed for this
    credential's scopes"; no SDK-documented app toggle, scope
    request, or re-pair path is known.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    refused = _fs_write_refusal(path)
    if refused:
        _audit({"tool": "muse_fs_write", "from": caller, "path": path,
                "ok": False, "error": f"refused: {refused}"})
        return {"ok": False, "error": f"refused: {refused}"}
    body = {"path": path, "text": text, "overwrite": overwrite,
            "append": append, "create_parent": create_parent}
    try:
        async with _link_session() as session:
            result = await _tunnel_request(session, "POST", "/fs/write",
                                           body)
    except Exception as e:
        _audit({"tool": "muse_fs_write", "from": caller, "path": path,
                "ok": False, "error": f"{type(e).__name__}: {e}"})
        return {"ok": False,
                "error": f"fs_write failed: {type(e).__name__}: {e}"}
    _audit({"tool": "muse_fs_write", "from": caller, "path": path,
            "ok": bool(result.get("ok")), "status": result.get("status"),
            "bytes": len((text or "").encode()), "overwrite": overwrite,
            "append": append})
    return {"ok": bool(result.get("ok")), "status": result.get("status"),
            "response": result.get("response")}


# ------------------------------------------- links & subscriptions


def _load_record_list(path: Path, key: str) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data[key] if isinstance(data, dict) else data
    return [dict(record) for record in records
            if isinstance(record, dict)]


def _save_record_list(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.replace(tmp, path)


@mcp.tool()
def muse_link_create(
        a_chat: str, b_chat: str, mode: str,
        from_: str = Field(validation_alias="from")) -> dict:
    """Create a standing relationship record between two chats.

    Purpose: register an auditable relationship (escalation,
    mirror-summary, or handoff) as data in links.json. A link is a
    declared relationship, never implicit behaviour.

    Args:
      a_chat, b_chat: source and destination directory/registry
        chat IDs or exact names.
      mode: escalation, mirror-summary, or handoff.
      from: creator identity. Required and impersonation-refused.

    Returns: {ok, link} with a generated link_id and timestamps.

    Guardrails: storage/CRUD only in this version. Creating a
    link does NOT make anything fire automatically; link firing
    lands with the future watcher work. Links into a chat whose
    name or kind identifies the Verify or Gmail lane are refused.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    if mode not in ("escalation", "mirror-summary", "handoff"):
        return {"ok": False,
                "error": "mode must be escalation, mirror-summary, or handoff"}
    source = _resolve_known_chat(a_chat)
    if source is None:
        return _refuse_unlisted("muse_link_create", caller, a_chat)
    destination = _resolve_known_chat(b_chat)
    if destination is None:
        return _refuse_unlisted("muse_link_create", caller, b_chat)
    dest_text = (f"{destination.get('name', '')} "
                 f"{destination.get('kind', '')}").lower()
    if "verify" in dest_text or "gmail" in dest_text:
        return {"ok": False,
                "error": "refused: links into the Verify or Gmail lane are not permitted"}
    links = _load_record_list(LINKS_FILE, "links")
    link = {
        "link_id": str(uuid.uuid4()),
        "a_chat_id": source["chat_id"],
        "a_chat": source.get("name"),
        "b_chat_id": destination["chat_id"],
        "b_chat": destination.get("name"),
        "mode": mode,
        "created_by": caller,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "firing": "not_implemented",
    }
    links.append(link)
    _save_record_list(LINKS_FILE, links)
    _audit({"tool": "muse_link_create", "from": caller,
            "link_id": link["link_id"], "a_chat_id": link["a_chat_id"],
            "chat_id": link["b_chat_id"], "mode": mode, "ok": True})
    return {"ok": True, "link": link}


@mcp.tool()
def muse_link_list() -> dict:
    """List standing muse-mcp chat-link records.

    Purpose: inspect links created with muse_link_create.

    Args: none.

    Returns: {ok, links, count}. Guardrails: read-only. A listed
    link is a stored relationship only; automatic firing is not
    implemented in this version and lands with watcher work.
    """
    links = _load_record_list(LINKS_FILE, "links")
    return {"ok": True, "links": links, "count": len(links)}


@mcp.tool()
def muse_link_delete(
        link_id: str, from_: str = Field(validation_alias="from")) -> dict:
    """Delete one standing chat-link record by link_id.

    Purpose: remove a relationship declaration from links.json.
    Args are the exact link_id and required caller identity in
    `from`. Returns {ok, deleted}. Unknown IDs return ok:false.

    Guardrails: this deletes only the local link record, never a
    chat. Because link firing is not implemented yet, deletion has
    no delivery side effect.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    links = _load_record_list(LINKS_FILE, "links")
    remaining = [link for link in links
                 if link.get("link_id") != link_id]
    if len(remaining) == len(links):
        return {"ok": False, "error": f"unknown link_id {link_id!r}"}
    _save_record_list(LINKS_FILE, remaining)
    _audit({"tool": "muse_link_delete", "from": caller,
            "link_id": link_id, "ok": True})
    return {"ok": True, "deleted": link_id}


@mcp.tool()
def muse_chat_subscribe(
        chat: str, event: str, days: int = 14,
        from_: str = Field(validation_alias="from")) -> dict:
    """Register a time-bounded watch on a classified chat.

    Purpose: record that a caller wants future delivery when a
    chat produces a named event class (for example reply,
    needs_nic, or any_turn), with the standing 14-day sunset used
    for push-born watches unless a shorter period is requested.

    Args:
      chat: classified directory chat ID or exact name.
      event: event label to watch. Required.
      days: registration lifetime; defaults and clamps to 14 days
        maximum (minimum 1).
      from: subscriber identity. Required and
        impersonation-refused.

    Returns: {ok, subscription} including subscription_id and
    expires_at. Guardrails: storage/CRUD only in this version;
    no delivery fires yet. Delivery wiring is the watcher
    follow-up, and this description must not be read as a live
    notification promise.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    if not (event or "").strip():
        return {"ok": False, "error": "'event' must not be empty"}
    target = _resolve_chat(chat)
    if target is None:
        return _refuse_unlisted("muse_chat_subscribe", caller, chat)
    days = max(1, min(int(days), 14))
    now = datetime.now(timezone.utc)
    subscription = {
        "subscription_id": str(uuid.uuid4()),
        "chat_id": target["chat_id"],
        "chat": target["name"],
        "event": event,
        "from": caller,
        "created_at": now.isoformat(),
        "expires_at": datetime.fromtimestamp(
            now.timestamp() + days * 24 * 60 * 60,
            timezone.utc).isoformat(),
        "days": days,
        "delivery": "not_implemented",
    }
    subscriptions = _load_record_list(
        SUBSCRIPTIONS_FILE, "subscriptions")
    subscriptions.append(subscription)
    _save_record_list(SUBSCRIPTIONS_FILE, subscriptions)
    _audit({"tool": "muse_chat_subscribe", "from": caller,
            "chat": target["name"], "chat_id": target["chat_id"],
            "subscription_id": subscription["subscription_id"],
            "event": event, "ok": True})
    return {"ok": True, "subscription": subscription}


@mcp.tool()
def muse_chat_subscriptions() -> dict:
    """List stored muse-mcp chat-watch registrations.

    Purpose: inspect subscriptions created with muse_chat_subscribe,
    including expiry and delivery status.

    Args: none. Returns {ok, subscriptions, count}.

    Guardrails: read-only. Delivery is not implemented in this
    version; listed subscriptions are registrations awaiting the
    watcher follow-up, not active notification guarantees.
    """
    subscriptions = _load_record_list(
        SUBSCRIPTIONS_FILE, "subscriptions")
    return {"ok": True, "subscriptions": subscriptions,
            "count": len(subscriptions)}


@mcp.tool()
def muse_chat_unsubscribe(
        subscription_id: str,
        from_: str = Field(validation_alias="from")) -> dict:
    """Remove one stored chat-watch registration.

    Purpose: cancel a subscription created by muse_chat_subscribe.
    Args are the exact subscription_id and required caller
    identity in `from`. Returns {ok, deleted}; unknown IDs return
    ok:false. Guardrails: removes only the local registration,
    never a chat or a link.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    subscriptions = _load_record_list(
        SUBSCRIPTIONS_FILE, "subscriptions")
    remaining = [item for item in subscriptions
                 if item.get("subscription_id") != subscription_id]
    if len(remaining) == len(subscriptions):
        return {"ok": False,
                "error": f"unknown subscription_id {subscription_id!r}"}
    _save_record_list(SUBSCRIPTIONS_FILE, remaining)
    _audit({"tool": "muse_chat_unsubscribe", "from": caller,
            "subscription_id": subscription_id, "ok": True})
    return {"ok": True, "deleted": subscription_id}


async def _registry_sync_impl(from_: str, reason: str = "",
                              known_revision: str = "") -> dict:
    """Ask the main chat to refresh this server's chat registry.

    The core loop (docs/architecture.md): the Link cannot
    enumerate chats, so this composes a structured registry_sync
    request (provenance line + machine-readable JSON block with a
    fresh correlation_id) and sends it into the MAIN chat via the
    Link. The main chat enumerates with its own chat tools and
    returns the updated registry as DATA (a registry.json write,
    per docs/registry-sync.md), never by Link; this tool's receipt
    is the Link message_id. Idempotence lives in the revision: pass
    known_revision (or leave empty to send the server's current
    directory revision) and a no-change sync mints no revision.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    entries = _load_directory()
    main = next((e for e in entries if e["kind"] == "main"), None) \
        or next((e for e in entries
                 if e["name"] == MAIN_CHAT_NAME_HINT), None)
    if main is None:
        return {"ok": False,
                "error": "directory has no main chat entry to sync with"}
    correlation_id = str(uuid.uuid4())
    revision = known_revision or _directory_revision()
    block = {
        "type": "registry_sync",
        "correlation_id": correlation_id,
        "from": caller,
        "reason": reason or None,
        "known_revision": revision,
        "server_revision": _directory_revision(),
        "directory_count": len(entries),
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    text = ("Registry sync request: please enumerate all chats "
            "(chat.list, including archived), diff against "
            "known_revision, preserve human-approved metadata, enter "
            "new chats as unregistered drafts, and return the "
            "updated registry as data (write registry.json in the "
            "validated format described in docs/registry-sync.md "
            "to every host running this server).\n\n"
            "```json\n" + json.dumps(block, indent=2) + "\n```")
    try:
        result = await _do_send(main, _stamped(caller, text))
    except Exception as e:
        _audit({"tool": "muse_registry_sync", "from": caller,
                "chat_id": main["chat_id"], "ok": False,
                "correlation_id": correlation_id,
                "error": f"{type(e).__name__}: {e}"})
        return {"ok": False,
                "error": f"registry sync failed: {type(e).__name__}: {e}"}
    receipt = _receipt(result, main["chat_id"])
    _audit({"tool": "muse_registry_sync", "from": caller,
            "chat": main["name"], "chat_id": main["chat_id"],
            "ok": receipt["ok"], "status": receipt["status"],
            "message_id": receipt["message_id"],
            "correlation_id": correlation_id,
            "known_revision": revision})
    return {"ok": receipt["ok"], "status": receipt["status"],
            "correlation_id": correlation_id,
            "message_id": receipt["message_id"],
            "session_id": receipt["session_id"],
            "known_revision": revision}


@mcp.tool()
async def muse_registry_sync(
        from_: str = Field(validation_alias="from"),
        reason: str = "", known_revision: str = "") -> dict:
    """Ask the main chat to refresh this server's chat registry.

    Purpose: start the core registry loop, because the Link API
    cannot enumerate Muse chats. The request is sent to the main
    chat over the Link; the refreshed registry returns as data,
    never as a Link message.

    Args:
      from: required requester identity; impersonation-refused.
      reason: optional human-readable reason recorded in the
        request and audit log.
      known_revision: optional caller-known directory revision;
        when omitted, the server's current directory revision is
        sent so an unchanged enumeration mints no new revision.

    Returns: the Link receipt plus correlation_id and
    known_revision. Guardrails: only the directory's designated
    main chat receives the request; this tool does not itself
    enumerate, classify, or authorize chats.
    """
    return await _registry_sync_impl(from_, reason, known_revision)


@mcp.tool()
async def sidechat_registry_update(
        from_: str = Field(validation_alias="from"),
        reason: str = "", known_revision: str = "") -> dict:
    """Alias of muse_registry_sync.

    Purpose, arguments, return shape, and guardrails are identical
    to muse_registry_sync; both names share one implementation.
    """
    return await _registry_sync_impl(from_, reason, known_revision)


# ------------------------------------------------- status v2 / watch


_WORKING_ACTIVITY_CODES = {
    "working", "responding", "thinking", "researching", "planning",
    "listening", "speaking", "processing", "generating", "executing",
    "waiting_for_subagents",
}
_RUNNING_TASK_STATUSES = {
    "running", "in_progress", "started", "active", "working",
}

_UNREAD_NOTE = (
    "No read/seen field exists in either the Link history or "
    "live-event surface; display_text_ready is not a read "
    "indicator, reaction_emoji proves nothing about the operator "
    "reading, and the app's unread state is not exposed (no "
    "mark-as-read endpoint exists). unread is therefore null. "
    "See docs/architecture.md.")


def _history_events_from(result: dict) -> list[dict]:
    """Extract history events from a Link tunnel result.

    GET /chat/history answers through the tunnel wrapper as
    {response: {ok, result: {chat_events: [...]}}}. The walk is
    defensive (the wrapper may nest one more level on some VM
    builds) but never invents events: anything unrecognised
    yields an empty list.
    """
    def find(obj, depth=0):
        if depth > 4 or not isinstance(obj, dict):
            return None
        events = obj.get("chat_events")
        if isinstance(events, list):
            return events
        for key in ("result", "response"):
            found = find(obj.get(key), depth + 1)
            if found is not None:
                return found
        return None

    found = find(result)
    if not found:
        return []
    return [e for e in found if isinstance(e, dict)]


def _message_block(event: dict, include_text: bool,
                   with_status: bool) -> dict:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    block = {
        "message_id": event.get("message_id")
        or payload.get("message_id"),
        "occurred_at_ms": event.get("occurred_at_ms"),
        "seq": event.get("seq"),
    }
    if with_status:
        block["status"] = payload.get("status")
    if include_text:
        text = payload.get("content")
        if not isinstance(text, str):
            text = event.get("display_text")
        if not isinstance(text, str):
            text = payload.get("display_text")
        block["text_excerpt"] = text[:280] if isinstance(
            text, str) else None
    return block


def _history_facts(events: list[dict], include_text: bool) -> dict:
    """Derive last activity and turn state from history events.

    Only message.user / message.assistant are conversational
    turns; delta.presentation events are transcript artifacts and
    are excluded from last-message derivation (they still count
    as the last canonical transcript event for last_activity).
    Derivation keys on seq, so a partial window cannot invert
    the chronological order the API returns.
    """
    ordered = sorted(events, key=lambda e: e.get("seq") or 0)
    last_activity = None
    if ordered:
        newest = ordered[-1]
        last_activity = {
            "occurred_at_ms": newest.get("occurred_at_ms"),
            "seq": newest.get("seq"),
            "event_name": newest.get("event_name"),
            "role": newest.get("role"),
            "label": "last canonical transcript event",
        }
    messages = [e for e in ordered
                if e.get("event_name") in ("message.user",
                                           "message.assistant")]
    last_user = next(
        (e for e in reversed(messages)
         if e.get("event_name") == "message.user"), None)
    last_assistant = next(
        (e for e in reversed(messages)
         if e.get("event_name") == "message.assistant"), None)
    state = None
    if messages:
        newest_msg = messages[-1]
        if newest_msg.get("event_name") == "message.user":
            state = "awaiting_assistant_response"
        else:
            payload = newest_msg.get("payload")
            status = (payload.get("status")
                      if isinstance(payload, dict) else None)
            if status in ("failed", "interrupted"):
                state = "assistant_attempt_incomplete"
            else:
                state = "awaiting_user_next_message"
    return {
        "last_activity": last_activity,
        "last_user_message": (
            _message_block(last_user, include_text, False)
            if last_user else None),
        "last_assistant_message": (
            _message_block(last_assistant, include_text, True)
            if last_assistant else None),
        "history_turn_state": state,
    }


def _live_from_events(events: list[dict], chat_id: str) -> dict:
    """Build the live block from one chat's subscription events.

    agent.status / task.status are attributed only when their
    payload session_id matches this chat (or is absent, which the
    per-chat subscription makes this chat's own). approvals.
    snapshot carries no session_id: it is emitted on this chat's
    own subscription and is the only source for the pending
    count, which stays null, never a guessed 0, when no snapshot
    arrived. tasks.updated is never consulted: it carries no
    session_id and is a broad task/run notification, not chat
    state.
    """
    agent_code = None
    agent_text = None
    task_status = None
    subagents = None
    pending = None
    snapshot_received = False
    for ev in events:
        name = ev.get("event")
        payload = ev.get("payload")
        if not isinstance(payload, dict):
            continue
        sid = payload.get("session_id")
        if name == "agent.status" and (sid is None or sid == chat_id):
            snapshot_received = True
            agent_code = payload.get("activity_code")
            agent_text = payload.get("activity_text")
        elif name == "task.status" and (
                sid is None or sid == chat_id):
            snapshot_received = True
            task_status = payload.get("status")
            subagents = payload.get("active_subagent_count")
        elif name == "approvals.snapshot":
            snapshot_received = True
            approvals = payload.get("pending_approvals")
            pending = (len(approvals)
                       if isinstance(approvals, list) else None)
    return {
        "agent_activity_code": agent_code,
        "agent_activity_text": agent_text,
        "task_status": task_status,
        "active_subagent_count": subagents,
        "pending_approval_count": pending,
        "snapshot_received": snapshot_received,
    }


def _live_turn_state(live: dict | None) -> str | None:
    if not live or not live.get("snapshot_received"):
        return None
    if live.get("pending_approval_count"):
        return "waiting_for_approval"
    if (live.get("task_status") in _RUNNING_TASK_STATUSES
            or (live.get("active_subagent_count") or 0) > 0
            or live.get("agent_activity_code")
            in _WORKING_ACTIVITY_CODES):
        return "agent_working"
    return None


def _recent_audit(chat_id: str, limit: int = 5) -> list[dict]:
    recent: list[dict] = []
    try:
        lines = AUDIT_LOG.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return recent
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except Exception:
            continue
        if record.get("chat_id") == chat_id:
            recent.append(record)
        if len(recent) >= limit:
            break
    return recent


def _status_metadata_only(target: dict, *, registered: bool,
                          archived: bool, caller: str) -> dict:
    """Metadata-only status for drafts and archived chats.

    No Link calls are made: no history, no events, no existence
    check. Enumeration by the registry is not authorization, and
    an archived chat is not a live target.
    """
    registry_entry = _registry_entry(target["chat_id"])
    _registry, registry_status = _load_registry()
    chat_block = {
        "chat_id": target["chat_id"],
        "name": target.get("name"),
        "kind": target.get("kind"),
        "purpose": target.get("purpose"),
    }
    _audit({"tool": "muse_chat_status", "from": caller,
            "chat": chat_block["name"], "chat_id": target["chat_id"],
            "ok": True, "metadata_only": True,
            "registered": registered, "archived": archived})
    return {
        "ok": True,
        "chat": chat_block,
        "registered": registered,
        "addressable": False,
        "archived": archived,
        "exists": None,
        "exists_note": (
            "not checked: metadata-only status for "
            + ("an archived chat" if archived
               else "an unregistered draft")),
        "last_activity": None,
        "last_user_message": None,
        "last_assistant_message": None,
        "history_turn_state": None,
        "live_turn_state": None,
        "turn_state": None,
        "turn_state_source": None,
        "live": None,
        "recent_audit": _recent_audit(target["chat_id"]),
        "registry_entry": registry_entry,
        "registry_status": registry_status,
        "registry_revision": registry_status.get("registry_revision"),
        "registry_synced_at": registry_status.get(
            "registry_synced_at"),
        "unread": None,
        "note": _UNREAD_NOTE,
    }


@mcp.tool()
async def muse_chat_status(
        chat: str,
        from_: str = Field(validation_alias="from"),
        history_limit: int = 50,
        snapshot_seconds: int = 3,
        include_text: bool = False) -> dict:
    """Composite status v2 for one chat: history plus live snapshot.

    Purpose: answer what a chat's conversational state actually
    is — who spoke last, whether the last answer completed, who
    the conversation is waiting on, whether work is running right
    now, and whether an approval is pending — by combining the
    two surfaces that can each answer part of it.

    Args:
      chat: directory chat ID or exact name; registry drafts and
        archived chats are answered with metadata only (below).
      from: required caller identity; impersonation-refused.
      history_limit: newest history events to read, default 50,
        clamped 1..50.
      snapshot_seconds: live subscription window for the state
        snapshot, default 3, clamped 0..10. Zero skips the
        subscription entirely (a forced no-snapshot run).
      include_text: when true, last user/assistant blocks carry a
        bounded text_excerpt.

    Returns for a registered, non-archived chat: identity and
    registered/addressable/archived flags; exists (+exists_error);
    last_activity labelled as the last canonical transcript
    event; last_user_message and last_assistant_message
    {message_id, occurred_at_ms, seq, status for the assistant};
    history_turn_state and live_turn_state separately, plus the
    combined turn_state and turn_state_source — when history and
    the live snapshot disagree, both source states are returned
    rather than collapsed into a false certainty; the live block
    {agent_activity_code, agent_activity_text, task_status,
    active_subagent_count, pending_approval_count,
    snapshot_received}; recent_audit; registry state; unread
    (always null) and the standing unread note.

    Turn-state rules: newest message is a user message ->
    awaiting_assistant_response; newest assistant message
    completed -> awaiting_user_next_message; failed/interrupted
    -> assistant_attempt_incomplete; live running task, active
    subagents, or a working activity code -> agent_working;
    non-empty pending approvals -> waiting_for_approval.

    Guardrails and hard limits: drafts (enumerated but not
    classified/registered) and registry-archived chats receive
    metadata only — no history, events, or existence calls.
    pending_approval_count is null when no approvals.snapshot
    arrived in the window, never a guessed 0. tasks.updated is
    never attributed to the chat (it carries no session_id).
    History returns only canonical transcript events
    (message.user, message.assistant, delta.presentation), and
    delta.presentation is excluded from last-message derivation.
    The app's unread state is not exposed by the Link API; no
    field in either surface records the operator reading, and
    this tool never presents derived state as unread state.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    history_limit = max(1, min(int(history_limit), 50))
    snapshot_seconds = max(0, min(int(snapshot_seconds), 10))
    # Resolve against the raw directory first (bypassing the
    # registry-archived suspension in _resolve_chat) so an
    # archived chat reports metadata instead of a refusal.
    directory_entry = next(
        (e for e in _load_directory()
         if chat == e["chat_id"] or chat == e["name"]), None)
    if directory_entry is not None:
        registry_entry = _registry_entry(directory_entry["chat_id"])
        if registry_entry and registry_entry.get("archived"):
            return _status_metadata_only(
                directory_entry, registered=True, archived=True,
                caller=caller)
        target = directory_entry
    else:
        draft = _registry_entry(chat)
        if draft is not None:
            return _status_metadata_only(
                draft, registered=False,
                archived=bool(draft.get("archived")), caller=caller)
        return _refuse_unlisted("muse_chat_status", caller, chat)
    chat_id = target["chat_id"]
    events: list[dict] = []
    stream_events: list[dict] = []
    exists = None
    exists_error = None
    history_error = None
    try:
        async with _link_session() as session:
            async def _pump():
                try:
                    async for event in session.subscribe_chat(chat_id):
                        stream_events.append(event)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass

            pump = None
            started = time.monotonic()
            if snapshot_seconds > 0:
                pump = asyncio.create_task(_pump())
                try:
                    await asyncio.wait_for(
                        session.chat_subscribed.wait(), 10)
                except asyncio.TimeoutError:
                    pass
            try:
                result = await _tunnel_request(
                    session, "GET",
                    f"/chat/history?limit={history_limit}"
                    f"&session_id={chat_id}")
                if result.get("ok"):
                    events = _history_events_from(result)
                else:
                    history_error = (
                        f"history status {result.get('status')}")
            except Exception as e:
                history_error = f"{type(e).__name__}: {e}"
            try:
                side = await session.open_side_chat(chat_id)
                exists = bool(side.exists)
            except Exception as e:
                exists_error = f"{type(e).__name__}: {e}"
            if pump is not None:
                remaining = snapshot_seconds - (
                    time.monotonic() - started)
                if remaining > 0:
                    await asyncio.sleep(remaining)
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
    except Exception as e:
        _audit({"tool": "muse_chat_status", "from": caller,
                "chat": target["name"], "chat_id": chat_id,
                "ok": False, "error": f"{type(e).__name__}: {e}"})
        return {"ok": False, "chat": target["name"],
                "chat_id": chat_id,
                "error": f"status failed: {type(e).__name__}: {e}"}
    facts = _history_facts(events, include_text)
    live = _live_from_events(stream_events, chat_id)
    live_state = _live_turn_state(live)
    history_state = facts["history_turn_state"]
    if live_state is not None:
        turn_state, turn_source = live_state, "live_snapshot"
    elif history_state is not None:
        turn_state, turn_source = history_state, "history"
    else:
        turn_state, turn_source = None, None
    registry_entry = _registry_entry(chat_id)
    _registry, registry_status = _load_registry()
    _audit({"tool": "muse_chat_status", "from": caller,
            "chat": target["name"], "chat_id": chat_id, "ok": True,
            "exists": exists, "turn_state": turn_state,
            "snapshot_received": live["snapshot_received"],
            "pending_approval_count": live["pending_approval_count"]})
    return {
        "ok": True,
        "chat": target,
        "registered": True,
        "addressable": True,
        "archived": False,
        "exists": exists,
        "exists_error": exists_error,
        "last_activity": facts["last_activity"],
        "last_user_message": facts["last_user_message"],
        "last_assistant_message": facts["last_assistant_message"],
        "history_turn_state": history_state,
        "live_turn_state": live_state,
        "turn_state": turn_state,
        "turn_state_source": turn_source,
        "history_error": history_error,
        "live": live,
        "recent_audit": _recent_audit(chat_id),
        "registry_entry": registry_entry,
        "registry_status": registry_status,
        "registry_revision": registry_status.get("registry_revision"),
        "registry_synced_at": registry_status.get(
            "registry_synced_at"),
        "unread": None,
        "note": _UNREAD_NOTE,
    }


async def _watch_one(target: dict, seconds: int,
                     include_initial_snapshot: bool) -> dict:
    """One chat's watch digest: subscribe, attribute, summarise."""
    chat_id = target["chat_id"]
    raw: list[dict] = []
    error = None
    try:
        async with _link_session() as session:
            async def _pump():
                async for event in session.subscribe_chat(chat_id):
                    raw.append(event)

            pump = asyncio.create_task(_pump())
            try:
                await asyncio.wait_for(pump, seconds)
            except asyncio.TimeoutError:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    # Strict attribution. A per-chat subscription also carries
    # global traffic: sessions.updated names other chats outright,
    # and tasks.updated has no session_id and is a broad task/run
    # notification, so both are excluded rather than claimed.
    # Everything else must carry this chat's session_id when it
    # carries one at all; approvals.snapshot carries none and is
    # emitted on this chat's own subscription.
    attributed: list[dict] = []
    for ev in raw:
        name = ev.get("event") or ""
        payload = ev.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        if name in ("tasks.updated", "sessions.updated") \
                or name.startswith("node."):
            continue
        sid = payload.get("session_id")
        if sid is not None and sid != chat_id:
            continue
        attributed.append(ev)
    seen: set = set()
    deduped: list[dict] = []
    for ev in attributed:
        seq = ev.get("seq")
        if seq is not None:
            if seq in seen:
                continue
            seen.add(seq)
        deduped.append(ev)
    counts = dict(Counter(
        (ev.get("event") or "?") for ev in deduped))
    # The inferred baseline: the first agent.status, task.status,
    # and approvals.snapshot on the subscription are the state
    # bundle the stream emits at subscribe time. It is an
    # empirical pattern, not a documented guarantee, and the
    # digest labels it as inferred.
    baseline: dict[str, dict | None] = {
        "agent_status": None, "task_status": None, "approvals": None}
    baseline_idx: set[int] = set()
    for idx, ev in enumerate(deduped):
        key = {"agent.status": "agent_status",
               "task.status": "task_status",
               "approvals.snapshot": "approvals"}.get(ev.get("event"))
        if key and baseline[key] is None:
            baseline[key] = ev
            baseline_idx.add(idx)
    changes = [ev for idx, ev in enumerate(deduped)
               if idx not in baseline_idx]
    delta_appends = [ev for ev in deduped
                     if ev.get("event") == "delta.text_append"]
    delta_chars = 0
    for ev in delta_appends:
        payload = ev.get("payload") or {}
        for key in ("text", "delta", "content"):
            value = payload.get(key)
            if isinstance(value, str):
                delta_chars += len(value)
                break
    reply_ids: list[str] = []
    for ev in changes:
        if ev.get("event") in ("message.assistant",
                               "delta.message_done"):
            payload = ev.get("payload") or {}
            mid = payload.get("message_id") or ev.get("message_id")
            if mid and mid not in reply_ids:
                reply_ids.append(mid)

    def last_of(name):
        found = [ev for ev in deduped if ev.get("event") == name]
        return found[-1] if found else None

    def pending_of(ev):
        if ev is None:
            return None
        approvals = (ev.get("payload") or {}).get("pending_approvals")
        return len(approvals) if isinstance(approvals, list) else None

    last_agent = last_of("agent.status")
    last_task = last_of("task.status")
    approvals_events = [ev for ev in deduped
                        if ev.get("event") == "approvals.snapshot"]
    snapshot_block = None
    if include_initial_snapshot:
        b_agent = (baseline["agent_status"] or {}).get(
            "payload") or {}
        b_task = (baseline["task_status"] or {}).get("payload") or {}
        snapshot_block = {
            "label": "inferred_baseline",
            "note": ("the subscribe-time state bundle is an "
                     "empirical pattern, not a documented "
                     "guarantee; this is the first agent.status / "
                     "task.status / approvals.snapshot seen on "
                     "this chat's subscription"),
            "received": any(
                v is not None for v in baseline.values()),
            "agent_status": (
                {"activity_code": b_agent.get("activity_code"),
                 "activity_text": b_agent.get("activity_text")}
                if baseline["agent_status"] else None),
            "task_status": (
                {"status": b_task.get("status"),
                 "active_subagent_count":
                     b_task.get("active_subagent_count")}
                if baseline["task_status"] else None),
            "approvals": (
                {"pending_approval_count":
                     pending_of(baseline["approvals"])}
                if baseline["approvals"] else None),
        }
    return {
        "chat": target["name"],
        "chat_id": chat_id,
        "registered": True,
        "addressable": True,
        "error": error,
        "initial_snapshot": snapshot_block,
        "counts": counts,
        "event_count": len(deduped),
        "changes_after_baseline": len(changes),
        "change_events": [
            _truncate_event(ev) for ev in changes
            if ev.get("event") != "delta.text_append"][:50],
        "delta_summary": {
            "text_append_events": len(delta_appends),
            "text_chars": delta_chars,
            "note": "delta text is counted, not returned",
        },
        "latest_agent_activity": (
            {"activity_code":
                 (last_agent.get("payload") or {}).get(
                     "activity_code"),
             "activity_text":
                 (last_agent.get("payload") or {}).get(
                     "activity_text")}
            if last_agent else None),
        "latest_task_status": (
            {"status": (last_task.get("payload") or {}).get("status"),
             "active_subagent_count":
                 (last_task.get("payload") or {}).get(
                     "active_subagent_count")}
            if last_task else None),
        "pending_approval_count_start": pending_of(
            baseline["approvals"]),
        "pending_approval_count_end": (
            pending_of(approvals_events[-1])
            if approvals_events else None),
        "reply_message_ids_completed": reply_ids,
        "quiet_after_snapshot": not changes,
    }


@mcp.tool()
async def muse_watch_digest(
        chats: list[str], seconds: int = 20,
        include_initial_snapshot: bool = True,
        from_: str = Field(validation_alias="from")) -> dict:
    """Watch several classified chats at once and digest the window.

    Purpose (prototype): answer what changed across a small set
    of chats during one bounded watch — who started working, who
    replied, who finished, where an approval appeared — which no
    single-chat tool can see. Subscriptions run in parallel.

    Args:
      chats: 1 to 5 directory chat IDs or exact names. Registered
        (classified, non-archived) chats only; drafts are refused.
      seconds: watch window per chat, default 20, clamped 1..30.
      include_initial_snapshot: include each chat's inferred
        baseline snapshot block (default true).
      from: required caller identity; impersonation-refused.

    Returns: {ok, seconds, chat_count, digests, note}. Each digest
    carries the initial snapshot (labelled inferred_baseline: the
    subscribe-time state bundle is an empirical pattern, not a
    documented guarantee), counts by event name, changes after
    the baseline (delta text counted in delta_summary, never
    returned chunk by chunk), latest agent activity and task
    status, pending approval counts at start and end when
    snapshots are available (null when none arrived), reply
    message IDs completed during the window, and
    quiet_after_snapshot.

    Guardrails and hard limits: there is no backlog — a
    subscription sees only what happens while it is open, plus
    the inferred baseline bundle. Attribution is strict: an event
    enters a chat's digest only when its payload session_id
    matches that chat (or it is the chat's own approvals
    snapshot); tasks.updated and sessions.updated are global
    traffic and are never attributed to any chat. Events are
    deduplicated by (chat, stream seq). Per-chat subscription
    failures are reported in that chat's digest error field
    rather than failing the whole call.
    """
    caller, refusal = _validate_from(from_)
    if refusal:
        return refusal
    if not chats or len(chats) > 5:
        return {"ok": False,
                "error": "'chats' must name 1 to 5 chats"}
    seconds = max(1, min(int(seconds), 30))
    targets = []
    for chat in chats:
        target = _resolve_chat(chat)
        if target is None:
            return _refuse_unlisted("muse_watch_digest", caller, chat)
        targets.append(target)
    digests = await asyncio.gather(*(
        _watch_one(target, seconds, include_initial_snapshot)
        for target in targets))
    ok = all(d["error"] is None for d in digests)
    _audit({"tool": "muse_watch_digest", "from": caller,
            "chats": [t["name"] for t in targets],
            "chat_ids": [t["chat_id"] for t in targets],
            "ok": ok, "seconds": seconds,
            "event_counts": {d["chat_id"]: d["event_count"]
                             for d in digests}})
    return {"ok": ok, "seconds": seconds,
            "chat_count": len(digests), "digests": digests,
            "note": ("Subscriptions see only what happens while "
                     "open (plus the inferred baseline bundle); "
                     "there is no backlog. Session-less global "
                     "events are never attributed to a chat.")}


@mcp.custom_route("/health", methods=["GET"])
async def _health(_request):
    """Unauthenticated liveness probe (tunnel + uptime checks), matching
    the house origin pattern: /health is exempt from the origin guard."""
    return JSONResponse({"status": "ok", "server": "muse-mcp",
                         "version": SERVICE_VERSION})


class _OriginSecretGuard:
    """ASGI middleware implementing the X-Origin-Secret origin guard.

    Every HTTP request except GET /health must present
    ORIGIN_SHARED_SECRET as the x-origin-secret header. The only
    legitimate callers are clients that hold the secret (a local
    MCP client, or a gateway you operate); they read the same value
    from this service's env file. The guard fails closed: __main__
    refuses to start the HTTP transport when the secret is unset or
    too short. Websocket scopes get the same check (rejected with a
    4401 close before accept).

    The middleware also aliases the root path to the MCP endpoint
    (/mcp): public-door clients address the server at its root URL,
    while the local client posts to /mcp.
    """

    def __init__(self, app, secret: str | None):
        self.app = app
        # compare_digest needs matching types: headers arrive as
        # bytes, the env secret is str. (Latent v1 bug: any guarded
        # request 500'd on the type mismatch; fixed in 0.2.0.)
        self._secret = (secret.encode("utf-8")
                        if isinstance(secret, str) else secret)

    def _authorized(self, scope) -> bool:
        if self._secret is None:
            return False
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        presented = headers.get(b"x-origin-secret", b"")
        return hmac.compare_digest(presented, self._secret)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            if not self._authorized(scope):
                await send({"type": "websocket.close", "code": 4401})
                return
        elif scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/":
                scope = {**scope, "path": "/mcp", "raw_path": b"/mcp"}
                path = "/mcp"
            is_health = scope.get("method") == "GET" and path in (
                "/health",
                "/health/",
            )
            if not is_health and not self._authorized(scope):
                await JSONResponse(
                    {"error": "unauthorized"}, status_code=401
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    transport = os.environ.get("TRANSPORT", "stdio")
    if transport == "http":
        # Streamable-HTTP for a standalone service (bind address
        # from the env; reached by clients presenting the origin
        # secret).
        import uvicorn

        host = os.environ.get("BIND_HOST", "127.0.0.1")
        port = int(os.environ.get("PORT", "7899"))
        origin_secret = os.environ.get("ORIGIN_SHARED_SECRET")
        if origin_secret is None or len(origin_secret) < 32:
            # Fail closed: an unguarded HTTP origin would serve
            # every write tool to anyone who can reach it, and a
            # short secret is barely a guard.
            # stdio transport remains available for local development.
            raise SystemExit(
                "muse-mcp: ORIGIN_SHARED_SECRET (>= 32 chars) is "
                "required for http transport; refusing to serve unguarded"
            )
        print("muse-mcp: X-Origin-Secret guard enabled")
        app = _OriginSecretGuard(
            mcp.streamable_http_app(
                transport_security=TRANSPORT_SECURITY, host=host),
            origin_secret)
        uvicorn.run(app, host=host, port=port)
    else:
        mcp.run()
