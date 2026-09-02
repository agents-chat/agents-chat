import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import read_frontend_source


ROOT = Path(__file__).resolve().parents[1]

# Gating CI sets this so a missing release marker becomes a loud failure instead
# of a green run full of skips. Left unset, a development tree still skips.
REQUIRE_ENV = "AGENT_CHAT_REQUIRE_RELEASE_CONTRACT"
_FALSEY = {"", "0", "false", "no", "off"}


def _public_tree_only():
    if (ROOT / ".community-release.json").is_file():
        return
    if os.environ.get(REQUIRE_ENV, "").strip().lower() not in _FALSEY:
        pytest.fail(
            f"{REQUIRE_ENV} is set, so the public release contract must run for real, "
            f"but {ROOT} has no .community-release.json release marker. These checks "
            "only prove anything inside a built Community Edition tree, so without the "
            "marker they would skip and report a false green. Build a tree with the "
            "release builder (scripts/community/build_release.py --output <dir>) and "
            "run pytest from inside that directory, or unset "
            f"{REQUIRE_ENV} to allow the development-tree skip.",
            pytrace=False,
        )
    pytest.skip("public release contract runs against a built Community tree")


def test_public_tree_contains_the_generic_connector_and_no_private_builder():
    _public_tree_only()
    manifest = json.loads((ROOT / "community/release_manifest.json").read_text(encoding="utf-8"))
    includes = set(manifest["include"])

    assert "scripts/openai_compatible_agent_bridge.py" in includes
    assert (ROOT / "scripts/openai_compatible_agent_bridge.py").is_file()
    assert "tests/conftest.py" in includes
    assert "tests/test_public_release_contract.py" in includes
    assert "scripts/community/build_release.py" not in includes
    assert "tests/test_community_release_builder.py" not in includes
    assert "community/private_release_policy.json" not in includes
    assert not (ROOT / "scripts/community/build_release.py").exists()
    assert not (ROOT / "community/private_release_policy.json").exists()
    assert (ROOT / ".gitleaks.toml").is_file()
    assert (ROOT / "Install Agents Chat on Mac.command").is_file()
    assert (ROOT / "Install Agents Chat on Windows.cmd").is_file()
    assert (ROOT / "scripts/community/open_macos.sh").is_file()
    assert (ROOT / "scripts/community/open_windows.ps1").is_file()


def test_public_defaults_point_to_the_public_organization_and_use_no_numeric_identity_example():
    _public_tree_only()
    config = (ROOT / "config.community.env").read_text(encoding="utf-8")
    html = read_frontend_source()

    assert "AGENT_CHAT_GITHUB_REPO_URL=https://github.com/agents-chat/agents-chat" in config
    user_id_input = re.search(r'<input id="tgUserId"[^>]+>', html)
    assert user_id_input
    assert not re.search(r'placeholder="[^"]*\d{7,}', user_id_input.group(0))


def test_public_gitignore_covers_generated_private_material():
    _public_tree_only()
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for entry in ("push_state/", "*.pem", "*.key", "*.p12", "*.pfx"):
        assert entry in ignored


def test_public_source_verifier_passes_with_ci_tooling_present():
    _public_tree_only()
    result = subprocess.run(
        [sys.executable, "scripts/community/verify_release.py", "--source"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload == {"ok": True, "mode": "source", "findings": []}


def test_exported_persona_images_have_no_authoring_metadata():
    _public_tree_only()
    markers = (
        b"<x:" + b"xmpmeta",
        b"http://ns.adobe.com/" + b"xap/1.0/",
        b"Canva " + b"(Renderer)",
    )
    for image in sorted((ROOT / "static/agent-personas").glob("*.png")):
        raw = image.read_bytes()
        assert not any(marker in raw for marker in markers), image.name


def test_first_launch_uses_local_password_creation_and_premium_login():
    _public_tree_only()
    config = (ROOT / "config.community.env").read_text(encoding="utf-8")
    html = read_frontend_source()
    creator = (ROOT / "scripts/community/create_config.py").read_text(encoding="utf-8")

    assert "COMMUNITY_FIRST_RUN_SETUP=1" in config
    assert "/api/auth/setup/status" in html
    assert "/api/auth/setup" in html
    assert "No temporary password and nothing to hunt down" in html
    assert 'id="loginDisplayName"' in html
    assert "Choose a username or email" in html
    assert "identifier: loginChoice" in html
    assert 'body.get("identifier") or body.get("username") or body.get("email")' in (ROOT / "app.py").read_text(encoding="utf-8")
    assert "login-panel" in html
    assert "prefers-reduced-motion" in html
    assert "if (pending) password.value = '';" in html
    assert "if (pending) confirm.value = '';" in html
    assert 'data-lpignore="true"' in html
    assert ".login-control:-webkit-autofill" in html
    assert "-webkit-text-fill-color:#f3f0e8!important" in html
    assert "0 0 0 1000px #0a0f0b inset!important" in html
    assert "enterSignedOutState(loginSetupState.pending ? '' : 'Session expired — sign in again.');" in html
    assert "Temporary password:" not in creator
    assert "Account:" not in creator
    assert "your name, a username or email, and password" in creator


def test_chat_does_not_show_agent_cost_estimates_as_spend():
    _public_tree_only()
    html = read_frontend_source()
    turn_metadata = html[
        html.index("function renderTurnMetadata(msg)"):
        html.index("// \"Hold on a second!\"")
    ]
    assert "meta.cost_usd" not in turn_metadata
    assert "model ${meta.model}" in turn_metadata
    assert "tokens" in turn_metadata


def test_public_examples_do_not_expose_sanitizer_language_or_private_names():
    _public_tree_only()
    html = read_frontend_source()
    for awkward in (
        "Owner Miller", "e.g. owner", "like Lead/Research",
        "like Lead/Research", "Ship the auth feature", "Owner — steady US male",
    ):
        assert awkward not in html
    assert 'placeholder="e.g. Alex Morgan"' in html
    assert "Plan my week and turn these notes into a clear action list" in html


def test_windows_store_codex_is_reported_as_installed_with_a_clear_next_step():
    _public_tree_only()
    backend = (ROOT / "app.py").read_text(encoding="utf-8")
    html = read_frontend_source()

    assert "Get-AppxPackage -Name" in backend
    assert '"OpenAI.Codex"' in backend
    assert "_first_usable_cli_path" in backend
    assert '("login", "status")' in backend
    assert 'home / "AppData" / "Local" / "Microsoft" / "WindowsApps" / "codex.exe"' not in backend
    assert '"setup_kind": "codex_cli"' in backend
    assert 'state, detail = "setup_needed"' in backend
    assert "setup_needed:['monitor-check','#79b8ff','Finish connection']" in html
    assert "Let Codex finish its own connection" in html
    assert "Copy exact prompt for Codex" in html
    assert "Agents Chat is watching automatically" in html
    assert "only after the real CLI passes readiness checks" in html
    assert "createCommunityPairPrompt({ silent:true, agentId:'codex' })" in html
    assert '"setup_kind": str(detected.get("setup_kind") or "")' in backend
    assert "_community_codex_cli_setup_prompt" in backend
    assert "npm install --global @openai/codex" in backend
    assert 'aid == "codex-desktop"' in backend
    assert "_community_verify_live_turn" in backend
    assert "installAgentRoster(data.agents || [])" in html
    assert "filter(agent => agent?.state === 'connected')" in html
    assert "The agent keeps only the permissions you already gave it" in html
    assert "only after a real-answer test" in html
