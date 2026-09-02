"""Global test isolation floor.

`app.DB_PATH` is resolved from `STORAGE_DB` at import time. pytest loads this
conftest before any test module, so forcing a throwaway `STORAGE_DB` here means
the suite can NEVER read or write the live database — even a test that forgets to
override `DB_PATH` in setUp, or any import-time DB access. (The live-DB writes
behind the `bruteforce@test.local` / `victim@x.local` audit rows came from
exactly this gap while the auth-hardening test was being written.)

Individual tests may still set their own `STORAGE_DB`/`DB_PATH` on top — this is
only a floor that guarantees the default is never the production DB.
"""
import os
import tempfile
from pathlib import Path


def read_frontend_source() -> str:
    """Combined shell + cacheable CSS/runtime used by source-contract tests.

    Production keeps the large stylesheet and JavaScript cacheable under
    ``/static``; tests still inspect one logical frontend source so moving code
    across those cache boundaries cannot silently weaken existing UI contracts.
    """
    root = Path(__file__).resolve().parents[1]
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            root / "templates" / "index.html",
            root / "static" / "shell-bootstrap.js",
            root / "static" / "login-boot.js",
            root / "static" / "app-shell.css",
            root / "static" / "app-runtime.js",
        )
    )

_FLOOR_DB = str(
    Path(tempfile.gettempdir()) / f"agentchat_test_floor_{os.getpid()}.sqlite3"
)
_MIRROR_DIR = Path(tempfile.gettempdir()) / f"agentchat_test_mirrors_{os.getpid()}"
_MIRROR_DIR.mkdir(parents=True, exist_ok=True)

# app resolves every mutable mirror path from DATA_DIR and immediately loads the
# chat index (including legacy JSON/thread migration) during import.  Set this
# floor before importing app; rebinding INDEX_STORE/THREADS_DIR afterwards is too
# late because live mirror rows may already be present in CHAT_INDEX/the floor DB.
os.environ["DATA_DIR"] = str(_MIRROR_DIR)


def _floor_storage_db() -> None:
    """Force STORAGE_DB off any live agent-chat DB. Called at load AND before
    every test: importing a bridge module (scripts/_bridge_common.load_env_files
    with overwrite=True) re-injects the repo's real .env — including the live
    STORAGE_DB — into os.environ mid-suite, which on 2026-08-07 let every test
    module after test_minimax_opencode_mode write the production DB (junk skills
    145-173 + hourly schedules that spammed real chats). The floor must therefore
    be re-asserted continuously, not just once at conftest load."""
    cur = os.environ.get("STORAGE_DB", "")
    if not cur or "agent-chat" in Path(cur).name:
        os.environ["STORAGE_DB"] = _FLOOR_DB


_floor_storage_db()
os.environ["REQUIRE_AUTH"] = "0"

# Same floor for the projects root: creating a project auto-creates and binds
# its workspace folder, so an unfloored root would let any test that POSTs
# /api/projects mint real directories under the live ~/AGENTS/projects.
_proj_root = os.environ.get("PROJECTS_FOLDER_ROOT", "")
if not _proj_root or "AGENTS" in _proj_root:
    os.environ["PROJECTS_FOLDER_ROOT"] = str(
        Path(tempfile.gettempdir()) / f"agentchat_test_projects_{os.getpid()}"
    )

# Mark the process as a test run. app._is_production_db() checks this to skip the
# JSON→SQLite migration backup, which otherwise copies the live ~27MB of JSON
# storage into data/storage_backups/ on every fresh-temp-DB import (i.e. every
# pytest run). Belt-and-suspenders next to the DATA_DIR-containment check.
os.environ["AGENTCHAT_TEST"] = "1"

# Same floor for the multi-calendar store (calendar_ics reads CALENDARS_STORE at
# import). Without this, tests would read the live `calendars.json` in the repo —
# which exists in production once the primary feed migrates — and calendar tests
# that expect an empty/unconfigured store would fail. Point it at a throwaway path.
os.environ.setdefault(
    "CALENDARS_STORE",
    str(Path(tempfile.gettempdir()) / f"agentchat_test_cals_{os.getpid()}.json"),
)

# Same floor for the connected-agents store. app._load_custom_agents() merges
# the checkout-local legacy custom_agents.json (real connected agents, e.g. the
# Kimi bridge) into app.AGENTS at import time unless the store is explicitly
# overridden, so without a floor every in-process test — and every subprocess
# test that copies os.environ — inherits the operator's live roster.
os.environ.setdefault(
    "CUSTOM_AGENTS_FILE",
    str(Path(tempfile.gettempdir()) / f"agentchat_test_custom_agents_{os.getpid()}.json"),
)

# Same floor for the Jarvis foreman config. The control-room API tests POST to
# /api/control-room/{foreman,control-chat,senior}, which call save_config() — so
# without a floor here they rewrite the LIVE data/jarvis_foreman.json. That is not
# hypothetical: on 2026-07-10 a test run replaced the operator's pinned control
# chat with the fixture id 1001 and switched the foreman off. Setting the env here
# (before any test module imports jarvis_foreman.config) makes the isolation
# independent of module import order. Tests that need the path should read this
# env var rather than minting their own directory.
_FOREMAN_DIR = Path(tempfile.gettempdir()) / f"agentchat_test_foreman_{os.getpid()}"
_FOREMAN_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault(
    "JARVIS_FOREMAN_CONFIG", str(_FOREMAN_DIR / "jarvis_foreman.json")
)


import pytest
import app

# Same floor for every on-disk store that sits BESIDE the DB rather than inside it.
# STORAGE_DB does not cover any of these, so a test that calls append_thread() or
# _save_index() writes the operator's live data even though its rows land in a
# throwaway sqlite file:
#   INDEX_STORE / SESSIONS_STORE  — the disaster-recovery JSON mirrors
#   THREADS_DIR                   — per-chat JSONL mirror (append_thread writes it)
#   ATTACHMENTS_DIR               — uploaded files
# DATA_DIR was floored before `import app`, so import-time migration/loading and
# all later writes use this throwaway directory. The explicit assignments keep
# the test floor obvious and preserve the globals individual fixtures restore.
app.INDEX_STORE = _MIRROR_DIR / "chats_index.json"
app.SESSIONS_STORE = _MIRROR_DIR / "sessions.json"
app.THREADS_DIR = _MIRROR_DIR / "threads"
app.THREADS_DIR.mkdir(parents=True, exist_ok=True)
app.ATTACHMENTS_DIR = _MIRROR_DIR / "attachments"
app.ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def setup_test_db():
    """Ensure app.DB_PATH always reflects the current STORAGE_DB env var and is initialized.

    Re-floors STORAGE_DB first — the env var is NOT trustworthy mid-suite (see
    _floor_storage_db) — and hard-fails rather than ever init/write a live DB."""
    _floor_storage_db()
    current_db = os.environ.get("STORAGE_DB")
    if current_db:
        app.DB_PATH = Path(current_db)
    if "agent-chat" in Path(app.DB_PATH).name:
        raise RuntimeError(
            f"test isolation breach: app.DB_PATH points at a live DB ({app.DB_PATH})"
        )
    app.REQUIRE_AUTH = False
    app.ACCESS_TOKEN = ""
    app._init_db()
