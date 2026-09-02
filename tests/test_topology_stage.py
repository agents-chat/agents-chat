import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
ATLAS = (ROOT / "templates" / "atlas.html").read_text(encoding="utf-8")
RUNTIME = (ROOT / "static" / "app-runtime.js").read_text(encoding="utf-8")
TOPOLOGY = (ROOT / "static" / "topology-stage.js").read_text(encoding="utf-8")
THREE_MODULE = ROOT / "static" / "vendor" / "three.module.min.js"
THREE_CORE = ROOT / "static" / "vendor" / "three.core.min.js"


def test_topology_javascript_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable for the JavaScript syntax gate")
    for path in (ROOT / "static" / "topology-stage.js", ROOT / "static" / "app-runtime.js"):
        result = subprocess.run(
            [node, "--check", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr
    scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", ATLAS)
    assert scripts
    for script in scripts:
        result = subprocess.run(
            [node, "--check", "--input-type=module"],
            input=script,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr


def test_three_is_local_and_not_in_the_initial_shell():
    initial_scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)', INDEX)

    assert not any("three" in src or "topology-stage" in src for src in initial_scripts)
    assert 'src="/static/atlas.html' not in INDEX
    assert "import(missionTopologyModuleUrl())" in RUNTIME
    assert "import(atlasTopologyModuleUrl())" in ATLAS
    assert "import * as THREE from './vendor/three.module.min.js';" in TOPOLOGY
    assert "https://" not in TOPOLOGY


def test_official_three_split_build_is_complete_and_licensed():
    module_source = THREE_MODULE.read_text(encoding="utf-8")
    core_source = THREE_CORE.read_text(encoding="utf-8")
    license_text = (ROOT / "licenses" / "THREE-MIT.txt").read_text(encoding="utf-8")
    dependency = re.search(r'from["\'](\./three\.core\.min\.js)["\']', module_source)

    assert dependency, "The official Three.js module build must declare its matching core file"
    assert (THREE_MODULE.parent / dependency.group(1)).resolve() == THREE_CORE.resolve()
    assert THREE_CORE.is_file(), "Do not ship the module half of Three.js without three.core.min.js"
    assert "Copyright 2010-2026 Three.js Authors" in module_source[:200]
    assert "SPDX-License-Identifier: MIT" in module_source[:200]
    assert "SPDX-License-Identifier: MIT" in core_source[:200]
    assert "The MIT License" in license_text
    assert "three.js authors" in license_text


def test_topology_stage_obeys_motion_data_and_rendering_budgets():
    assert "const MAX_DPR = 1.5" in TOPOLOGY
    assert "renderer.setPixelRatio(Math.min(MAX_DPR" in TOPOLOGY
    assert "prefers-reduced-motion: reduce" in TOPOLOGY
    assert "navigator?.connection?.saveData" in TOPOLOGY
    assert "nodes.slice(0, 64)" in TOPOLOGY
    assert "powerPreference: 'low-power'" in TOPOLOGY
    assert (ROOT / "static" / "topology-stage.js").stat().st_size < 24_000
    assert THREE_MODULE.stat().st_size + THREE_CORE.stat().st_size < 800_000


def test_topology_stage_uses_product_look_not_debug_helpers():
    assert "GridHelper" not in TOPOLOGY
    assert "IcosahedronGeometry" not in TOPOLOGY
    assert "CircleGeometry" not in TOPOLOGY
    assert "FogExp2" not in TOPOLOGY
    assert "LineBasicMaterial" not in TOPOLOGY
    assert "LineSegments" not in TOPOLOGY
    assert "ac-topology-hint" not in TOPOLOGY
    assert "iridescence" not in TOPOLOGY
    assert "MeshPhysicalMaterial" in TOPOLOGY
    assert "SphereGeometry" in TOPOLOGY
    assert "TorusGeometry" in TOPOLOGY
    assert "makeRibbonGeometry" in TOPOLOGY
    assert "vertexColors: true" in TOPOLOGY
    assert "--gold" in TOPOLOGY
    assert "--brass" in TOPOLOGY
    assert "three/addons" not in TOPOLOGY
    assert "UnrealBloom" not in TOPOLOGY
    assert "from 'three/" not in TOPOLOGY


def test_topology_stage_pauses_and_frees_gpu_resources():
    for contract in (
        "document.hidden",
        "visibilitychange",
        "new IntersectionObserver",
        "cancelAnimationFrame(frame)",
        "resizeObserver?.disconnect()",
        "intersectionObserver?.disconnect()",
        "nodeGeometry.dispose()",
        "ringGeometry.dispose()",
        "materials.forEach(material => material.dispose())",
        "renderer.dispose()",
        "renderer.forceContextLoss?.()",
    ):
        assert contract in TOPOLOGY

    assert "disposeMissionTopology()" in RUNTIME
    assert "frame?.contentWindow?.postMessage({ type:'atlas-visibility', visible:false }" in RUNTIME
    assert "addEventListener('pagehide',disposeAtlasTopology)" in ATLAS
    assert "event.data?.type!=='atlas-visibility'" in ATLAS


def test_3d_is_an_opt_in_layer_over_complete_dom_fallbacks():
    assert "data-mm-topology" in RUNTIME
    assert "renderMissionTopology(data)" in RUNTIME
    assert "renderMissionJourney(data)" in RUNTIME
    assert RUNTIME.index("${renderMissionTopology(data)}") < RUNTIME.index("${renderMissionJourney(data)}")
    assert "The complete 2D reading path remains directly below" in RUNTIME
    assert 'id="topologyToggle"' in ATLAS
    assert 'id="canvasStage"' in ATLAS
    assert 'id="nodeList"' in ATLAS
    assert "complete keyboard list below" in ATLAS
    assert "canvasStage.hidden=Topology3D.open" in ATLAS
    assert "topologyHost.hidden=!Topology3D.open" in ATLAS


def test_canvas_is_decorative_and_every_node_has_a_keyboard_label():
    assert "canvas.setAttribute('aria-hidden', 'true')" in TOPOLOGY
    assert "const label = document.createElement('button')" in TOPOLOGY
    assert "label.setAttribute('aria-label'" in TOPOLOGY
    for key in ("ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"):
        assert key in TOPOLOGY


def test_community_export_includes_topology_runtime_vendor_pair_and_license():
    manifest = json.loads((ROOT / "community" / "release_manifest.json").read_text(encoding="utf-8"))
    includes = set(manifest["include"])
    three_license = (ROOT / "licenses" / "THREE-MIT.txt").read_text(encoding="utf-8")

    assert "static/topology-stage.js" in includes
    assert "static/vendor/*" in includes
    assert "licenses/*" in includes
    assert "tests/test_topology_stage.py" in includes
    assert "MIT License" in three_license
    assert "three.js" in three_license.lower()
