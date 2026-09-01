"""Agent-generated visual artifacts — P1 of the multi-agent canvas.

An agent emits a fenced ```artifact block in its reply describing something
renderable (an image it wrote to the chat workspace, an inline SVG/HTML widget,
a chart, a short code listing, a link). On ingest we parse those blocks, persist
each artifact's bytes into a per-chat store, and return lightweight artifact
records that get attached to the message — so the UI can show a chip and render
the full thing in the sandboxed canvas panel.

Kept deliberately small and dependency-free so it is easy to review and so the
v5.13 -> v5.14 cutover adds no new runtime requirements.

Fence grammar (both forms supported):

    ```artifact
    {"kind": "svg", "title": "Q2 leads"}
    <svg ...>...</svg>
    ```

  - First line MAY be a JSON header. Everything after it is the raw body.
  - If there is no body, the header may carry "path" (a file in the chat
    workspace) or "content" (inline string), or "url" (external link).
  - If there is no JSON header at all, the kind is sniffed from the body.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
import shutil
from pathlib import Path
from typing import Optional, Tuple, List, Dict

ARTIFACTS_DIRNAME = "artifacts_store"
MAX_INLINE_BYTES = 3_000_000  # guard against a runaway inline blob

# kind -> (default extension, default mime, client render mode)
#   render mode: "image" | "frame" (sandboxed iframe) | "video" | "text"
#               (fetched + rendered in the canvas: markdown/json/code/office) |
#               "link" | "download"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
KIND_SPECS: Dict[str, Tuple[str, str, str]] = {
    "image":    ("png", "image/png", "image"),
    "svg":      ("svg", "image/svg+xml", "frame"),
    "html":     ("html", "text/html", "frame"),
    "chart":    ("svg", "image/svg+xml", "frame"),
    "pdf":      ("pdf", "application/pdf", "frame"),
    "video":    ("mp4", "video/mp4", "video"),
    "md":       ("md", "text/markdown", "text"),
    "markdown": ("md", "text/markdown", "text"),
    "json":     ("json", "application/json", "text"),
    "csv":      ("csv", "text/csv", "text"),
    "doc":      ("docx", _DOCX_MIME, "text"),
    "docx":     ("docx", _DOCX_MIME, "text"),
    "sheet":    ("xlsx", _XLSX_MIME, "text"),
    "slides":   ("pptx", _PPTX_MIME, "text"),
    "code":     ("txt", "text/plain", "text"),
    "diff":     ("diff", "text/plain", "text"),
    "url":      ("url", "text/uri-list", "link"),
}

EXT_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml",
    "html": "text/html", "htm": "text/html", "mp4": "video/mp4",
    "webm": "video/webm", "mov": "video/quicktime", "pdf": "application/pdf",
    "json": "application/json", "txt": "text/plain", "md": "text/markdown",
    "markdown": "text/markdown",
    "csv": "text/csv", "diff": "text/plain", "py": "text/plain",
    "js": "text/plain", "ts": "text/plain", "tsx": "text/plain",
    "jsx": "text/plain", "css": "text/plain", "yml": "text/plain",
    "yaml": "text/plain", "toml": "text/plain", "sh": "text/plain",
    "sql": "text/plain", "xml": "text/plain", "log": "text/plain",
    "docx": _DOCX_MIME, "xlsx": _XLSX_MIME, "pptx": _PPTX_MIME,
}

_FENCE_RE = re.compile(r"```artifact[^\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_HEX_RE = re.compile(r"^[0-9a-f]{8,40}$")


def _store_dir(base_dir: Path, cid: int) -> Path:
    d = Path(base_dir) / ARTIFACTS_DIRNAME / str(int(cid))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_workspace_file(workspace_dir: Optional[str], rel: str) -> Optional[Path]:
    """Resolve `rel` inside the chat workspace, refusing anything that escapes it."""
    if not workspace_dir or not rel:
        return None
    try:
        root = Path(workspace_dir).resolve()
        target = (root / rel).resolve()
    except Exception:
        return None
    try:
        target.relative_to(root)
    except ValueError:
        return None  # path traversal attempt
    return target if target.is_file() else None


_OFFICE_MIMES = (_DOCX_MIME, _XLSX_MIME, _PPTX_MIME)


def _render_mode(kind: str, mime: str) -> str:
    if kind in KIND_SPECS:
        return KIND_SPECS[kind][2]
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime in ("text/html", "image/svg+xml", "application/pdf"):
        return "frame"
    if mime.startswith("text/") or mime in ("application/json",) or mime in _OFFICE_MIMES:
        return "text"
    return "download"


# Matches a lone <media>/<img>/<iframe>/<a> reference — the "I pointed at a file
# instead of inlining it" failure mode an agent occasionally produces.
_REF_TAG_RE = re.compile(
    r"""<\s*(?:media|img|iframe|embed|object|source|a)\b[^>]*?"""
    r"""\s(?:src|href|data)\s*=\s*["']([^"']+)["'][^>]*?>"""
    r"""(?:\s*</\s*(?:a|iframe|object)\s*>)?""",
    re.IGNORECASE | re.DOTALL,
)


def _resolve_ref_file(workspace_dir: Optional[str], ref: str) -> Optional[Path]:
    """Resolve a src/href that points at a file, but ONLY if it lands inside the
    chat workspace (handles both relative and absolute-within-workspace paths)."""
    if not ref or not workspace_dir:
        return None
    ref = ref.strip()
    if ref.startswith("file://"):
        ref = ref[7:]
    if ref.startswith(("http://", "https://", "data:", "//")):
        return None
    hit = _safe_workspace_file(workspace_dir, ref)
    if hit is not None:
        return hit
    if os.path.isabs(ref):
        try:
            root = Path(workspace_dir).resolve()
            target = Path(ref).resolve()
            target.relative_to(root)  # refuse anything escaping the workspace
        except (ValueError, OSError):
            return None
        return target if target.is_file() else None
    return None


def _is_bare_ref_stub(body: str) -> Tuple[bool, str]:
    """A renderable artifact whose body is essentially just a media/file reference
    (e.g. `<media src="/abs/report.html"/>`) renders blank. Detect that case and
    return (is_stub, referenced_path). Conservative: only fires on short bodies
    that contain a ref tag and nothing else meaningful, so real inline HTML is
    never misclassified."""
    t = (body or "").strip()
    if not t or len(t) > 600:
        return False, ""
    m = _REF_TAG_RE.search(t)
    if not m:
        return False, ""
    remainder = _REF_TAG_RE.sub("", t).strip()
    # Strip any leftover bare tags; if nothing renderable remains it's a stub.
    remainder = re.sub(r"<[^>]+>", "", remainder).strip()
    return (remainder == "", m.group(1))


def _sniff_kind(body: str) -> str:
    head = body.lstrip()[:200].lower()
    if head.startswith("<svg"):
        return "svg"
    if head.startswith(("<!doctype", "<html", "<div", "<section", "<main", "<style")):
        return "html"
    return "code"


def _write_meta(store: Path, art_id: str, meta: dict) -> None:
    (store / f"{art_id}.meta.json").write_text(json.dumps(meta))


def artifact_paths(base_dir: Path, cid: int, art_id: str) -> Tuple[Optional[Path], Optional[Path]]:
    """Return (bytes_path, meta_path) for a stored artifact, or (None, None)."""
    if not _HEX_RE.match(art_id or ""):
        return None, None
    store = Path(base_dir) / ARTIFACTS_DIRNAME / str(int(cid))
    meta_path = store / f"{art_id}.meta.json"
    if not meta_path.is_file():
        return None, None
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return None, None
    bytes_path = store / meta.get("file", "")
    return (bytes_path if bytes_path.is_file() else None), meta_path


def _persist(
    store: Path, art_id: str, *, kind: str, ext: str, content: Optional[str],
    src_file: Optional[Path],
) -> Optional[Tuple[str, int]]:
    """Write the artifact bytes into the store. Returns (filename, size) or None."""
    fname = f"{art_id}.{ext}"
    dest = store / fname
    try:
        if src_file is not None:
            shutil.copyfile(src_file, dest)
        elif content is not None:
            data = content.encode("utf-8")
            if len(data) > MAX_INLINE_BYTES:
                return None
            dest.write_bytes(data)
        else:
            return None
    except Exception:
        return None
    return fname, dest.stat().st_size


def extract_artifacts(
    text: str,
    *,
    base_dir: Path,
    cid: int,
    agent_id: Optional[str],
    workspace_dir: Optional[str] = None,
) -> Tuple[str, List[dict]]:
    """Parse ```artifact fences out of `text`. Returns (clean_text, artifacts).

    `clean_text` has the fences removed (the chip + canvas replace them). The
    artifact records are JSON-safe dicts ready to attach to a message and
    broadcast over SSE.
    """
    if not text or "```artifact" not in text.lower():
        return text, []

    store = _store_dir(base_dir, cid)
    artifacts: List[dict] = []

    def _replace(match: "re.Match") -> str:
        block = match.group(1)
        meta: dict = {}
        body = block

        first, _, rest = block.partition("\n")
        first_stripped = first.strip()
        if first_stripped.startswith("{"):
            try:
                meta = json.loads(first_stripped)
                body = rest
            except json.JSONDecodeError:
                # Maybe the whole block is one multi-line JSON object.
                try:
                    meta = json.loads(block.strip())
                    body = ""
                except json.JSONDecodeError:
                    meta = {}
                    body = block
        body = body.strip("\n")

        kind = str(meta.get("kind") or "").strip().lower()
        url = str(meta.get("url") or "").strip()
        path = str(meta.get("path") or "").strip()
        content = meta.get("content")
        if isinstance(content, (dict, list)):
            content = json.dumps(content)
        if body and not content and not path:
            content = body
        if not kind:
            kind = "url" if url else (_sniff_kind(content) if content else "code")

        art_id = uuid.uuid4().hex[:16]
        title = str(meta.get("title") or kind.upper()).strip()[:160]

        # --- URL artifacts: no bytes, just a link record -----------------
        if kind == "url" or (url and not content and not path):
            if not url:
                return ""  # nothing usable
            rec = {
                "id": art_id, "kind": "url", "title": title or url,
                "render": "link", "external_url": url, "mime": "text/uri-list",
                "agent_id": agent_id, "ts": int(time.time() * 1000), "version": 1,
            }
            _write_meta(store, art_id, {**rec, "file": ""})
            artifacts.append(rec)
            return ""

        # --- File- or content-backed artifacts ---------------------------
        src_file = _resolve_ref_file(workspace_dir, path) if path else None

        # Repair: agents sometimes "show" a file by emitting a bare reference tag
        # (e.g. <media src="/abs/report.html"/>) instead of inlining the content or
        # using the "path" header. That persists as a blank canvas card. Pull the
        # referenced file if it lives in the workspace; otherwise drop the artifact
        # rather than render an empty panel.
        if src_file is None and content and kind in ("html", "svg", "chart"):
            is_stub, ref = _is_bare_ref_stub(content)
            if is_stub:
                resolved = _resolve_ref_file(workspace_dir, ref)
                if resolved is not None:
                    src_file = resolved
                    content = None
                    new_ext = resolved.suffix.lstrip(".").lower()
                    if new_ext:
                        kind = _kind_for_ext(new_ext)
                else:
                    return ""  # referenced file missing, nothing inlined -> drop

        ext = ""
        if src_file is not None:
            ext = src_file.suffix.lstrip(".").lower()
        if not ext:
            ext = KIND_SPECS.get(kind, ("bin", "application/octet-stream", "download"))[0]

        mime = str(meta.get("mime") or "").strip() or EXT_MIME.get(ext) \
            or KIND_SPECS.get(kind, ("", "application/octet-stream", ""))[1]

        persisted = _persist(
            store, art_id, kind=kind, ext=ext,
            content=content if src_file is None else None, src_file=src_file,
        )
        if not persisted:
            return ""  # drop a malformed artifact rather than break the message
        fname, size = persisted

        rec = {
            "id": art_id,
            "kind": kind,
            "title": title,
            "mime": mime,
            "render": _render_mode(kind, mime),
            "size": size,
            "agent_id": agent_id,
            "ts": int(time.time() * 1000),
            "version": 1,
            "url": f"/artifacts/{int(cid)}/{art_id}",
        }
        if src_file is not None:
            # Preserve the real workspace filename.  The orchestrator uses this
            # to recognize that a path-backed fence and its post-turn workspace
            # scan are the same file instead of attaching it twice.
            rec["source_name"] = src_file.name
        for opt in ("width", "height", "interactive"):
            if opt in meta:
                rec[opt] = meta[opt]
        _write_meta(store, art_id, {**rec, "file": fname})
        artifacts.append(rec)
        return ""

    clean = _FENCE_RE.sub(_replace, text)
    # Tidy up blank lines left where fences were removed.
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, artifacts


def _kind_for_ext(ext: str) -> str:
    """Best-effort canvas kind from a file extension (used when pulling a raw
    workspace file into the canvas, where there is no agent-supplied header)."""
    ext = (ext or "").lower().lstrip(".")
    if ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "ico"):
        return "image"
    if ext == "svg":
        return "svg"
    if ext in ("html", "htm"):
        return "html"
    if ext == "pdf":
        return "pdf"
    if ext in ("mp4", "webm", "mov", "m4v"):
        return "video"
    if ext in ("md", "markdown"):
        return "md"
    if ext == "json":
        return "json"
    if ext == "csv":
        return "csv"
    if ext == "docx":
        return "doc"
    if ext == "xlsx":
        return "sheet"
    if ext == "pptx":
        return "slides"
    if ext in ("txt", "py", "js", "ts", "tsx",
               "jsx", "css", "yml", "yaml", "toml", "sh", "sql", "xml", "log"):
        return "code"
    if ext == "diff":
        return "diff"
    return "code"


def register_file_artifact(
    base_dir: Path, cid: int, agent_id: Optional[str], src_path,
    title: Optional[str] = None, *, source_name: Optional[str] = None,
    source_attachment_id: Optional[str] = None,
) -> Optional[dict]:
    """Persist an arbitrary file from the chat workspace into the canvas store as
    an artifact, inferring kind/mime from its extension. Returns the artifact
    record (ready to attach to a message + broadcast over SSE), or None on
    failure. Unlike extract_artifacts() this takes a concrete file rather than a
    ```artifact fence — it backs the "pull what the agent created into the chat"
    button, where the agent forgot to emit a fence."""
    src = Path(src_path)
    try:
        if not src.is_file():
            return None
    except OSError:
        return None
    display_name = (source_name or src.name).strip() or src.name
    ext = Path(display_name).suffix.lstrip(".").lower() or src.suffix.lstrip(".").lower() or "bin"
    kind = _kind_for_ext(ext)
    mime = EXT_MIME.get(ext) or KIND_SPECS.get(kind, ("", "application/octet-stream", ""))[1]
    store = _store_dir(base_dir, cid)
    art_id = uuid.uuid4().hex[:16]
    persisted = _persist(store, art_id, kind=kind, ext=ext, content=None, src_file=src)
    if not persisted:
        return None
    fname, size = persisted
    rec = {
        "id": art_id,
        "kind": kind,
        "title": (title or display_name)[:160],
        "mime": mime,
        "render": _render_mode(kind, mime),
        "size": size,
        "agent_id": agent_id,
        "ts": int(time.time() * 1000),
        "version": 1,
        "url": f"/artifacts/{int(cid)}/{art_id}",
        "source_name": display_name,
    }
    if source_attachment_id:
        rec["source_attachment_id"] = str(source_attachment_id)
    _write_meta(store, art_id, {**rec, "file": fname})
    return rec


def register_bytes_artifact(
    base_dir: Path, cid: int, agent_id: Optional[str], data: bytes, *,
    source_name: str, mime: Optional[str] = None, title: Optional[str] = None,
    source_attachment_id: Optional[str] = None,
) -> Optional[dict]:
    """Persist already-received bytes as a sandboxed canvas artifact.

    This is the safe ingress for agent-authored HTML/SVG (which must never be
    served from the ordinary attachment route) and lets attachment uploads gain
    a Canvas overlay without staging a temporary workspace file first.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None
    display_name = (source_name or "artifact.bin").strip() or "artifact.bin"
    ext = Path(display_name).suffix.lstrip(".").lower() or "bin"
    kind = _kind_for_ext(ext)
    declared_mime = (mime or "").strip().lower()
    resolved_mime = (
        EXT_MIME.get(ext)
        if declared_mime in ("", "application/octet-stream") and EXT_MIME.get(ext)
        else declared_mime
    ) or KIND_SPECS.get(kind, ("", "application/octet-stream", ""))[1]
    store = _store_dir(base_dir, cid)
    art_id = uuid.uuid4().hex[:16]
    fname = f"{art_id}.{ext}"
    dest = store / fname
    try:
        dest.write_bytes(bytes(data))
        size = dest.stat().st_size
    except Exception:
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        return None
    rec = {
        "id": art_id,
        "kind": kind,
        "title": (title or display_name)[:160],
        "mime": resolved_mime,
        "render": _render_mode(kind, resolved_mime),
        "size": size,
        "agent_id": agent_id,
        "ts": int(time.time() * 1000),
        "version": 1,
        "url": f"/artifacts/{int(cid)}/{art_id}",
        "source_name": display_name,
    }
    if source_attachment_id:
        rec["source_attachment_id"] = str(source_attachment_id)
    _write_meta(store, art_id, {**rec, "file": fname})
    return rec


def read_artifact(base_dir: Path, cid: int, art_id: str, limit: int = 8000) -> Optional[dict]:
    """Load a stored artifact's metadata and (for text kinds) its content, so it
    can be handed to another agent as context. Returns None if not found."""
    bytes_path, meta_path = artifact_paths(base_dir, cid, art_id)
    if meta_path is None:
        return None
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return None
    out = dict(meta)
    out["content"] = None
    mime = meta.get("mime") or ""
    is_text = (
        mime.startswith("text/") or mime in ("image/svg+xml", "application/json")
        or meta.get("kind") in ("svg", "html", "chart", "code", "diff")
    )
    if bytes_path is not None and is_text:
        try:
            txt = bytes_path.read_text(errors="replace")
            out["content"] = txt[:limit]
            out["truncated"] = len(txt) > limit
        except Exception:
            out["content"] = None
    return out


# The instruction appended to agent prompts so they know how to emit a visual.
ARTIFACT_PROMPT = (
    "\n\n## Showing visuals (the Chat Canvas)\n"
    "This chat has a shared **Chat Canvas** — a side panel, opened from the Chat "
    "Canvas icon in the left rail, where everyone in the room sees the files and "
    "visuals you produce. Whenever your work produces anything viewable — a chart, "
    "diagram, table, image, rendered HTML/SVG, mockup, dashboard, a report, a "
    "Markdown/JSON/CSV/code file, a PDF, or a Word (.docx) document — you are "
    "expected to actually share it on the Chat Canvas as part of the work, not just "
    "describe it in prose or promise it for later. Build it and post it now.\n\n"
    "**Always save the real file first in the delivery location named in this "
    "turn's Attached chat folder / Chat file delivery note.** For a host CLI agent "
    "that is the exact per-chat workspace (and Agent Chat auto-captures it); for a "
    "container agent it is the provided outbox or attachment_save route. Never leave "
    "the only copy in an unrelated/private folder. A fenced block can additionally "
    "give the Canvas a custom title or inline content. There are exactly two correct "
    "fence forms:\n"
    "```artifact\n"
    '{\"kind\": \"html|svg|chart|image|pdf|md|json|csv|code|docx|url\", \"title\": \"Short title\"}\n'
    "<EITHER inline the raw content directly here (raw HTML/SVG/Markdown/JSON/text)>\n"
    "```\n"
    "or, to surface a file you already wrote, omit the body and pass a **path "
    "relative to your workspace** in the header instead:\n"
    "```artifact\n"
    '{\"kind\": \"html\", \"title\": \"Daily Audit\", \"path\": \"daily-audit.html\"}\n'
    "```\n"
    "(or pass \"url\" for an external link).\n\n"
    "**Hard rules — these break the Canvas if you ignore them:**\n"
    "- Do NOT reference a file with a `<media>`, `<img>`, `<iframe>`, or `<a>` tag "
    "pointing at a path — that renders a blank panel. Inline the content, or use the "
    "`\"path\"` header.\n"
    "- Do NOT use absolute filesystem paths (e.g. /Users/...). Use a path relative to "
    "your workspace.\n"
    "- Only reference files that actually exist — write the file before you emit the "
    "fence.\n"
    "The Canvas renders HTML, SVG, images, PDF, Markdown, JSON, CSV, code, and Word "
    "(.docx) files directly. Keep your normal text answer too — the Canvas supplements "
    "it. Self-contain SVG/HTML (inline styles; external scripts only from "
    "cdnjs/jsdelivr/unpkg/esm.sh).\n\n"
    "### Making a widget talk back (control surface)\n"
    "An HTML artifact can hand a follow-up back into the chat. From inside the widget call:\n"
    "  parent.postMessage({type: 'canvas:prompt', text: 'the message to send'}, '*')\n"
    "or, for a structured action, "
    "parent.postMessage({type: 'canvas:action', action: 'name', data: {...}}, '*').\n"
    "Optionally add agent: '<handle>' to aim it at one agent. The user always sees a "
    "confirm bar and approves before anything is sent, so wire these to buttons, not to page load."
)
