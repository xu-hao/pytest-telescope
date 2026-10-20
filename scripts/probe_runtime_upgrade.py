"""Measure an explicit upgrade from an installed published runtime package.

Run with the candidate installed, --previous-python pointing to a separate environment
containing the previous published package, and an unused host-visible --project path.
The probe creates only synthetic inputs and removes its exact owned runtime images.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from pytest_telescope import controller, runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous-python", required=True)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    root = args.project.resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "pyproject.toml").write_text("[tool.pytest_telescope]\n")
    (root / "test_example.py").write_text("def test_example():\n    assert 2 + 2 == 4\n")
    previous_code = """
import json,sys
from pathlib import Path
from pytest_telescope import runtime
root=Path(sys.argv[1])
records=[]
for count in range(4):
    requirements=root/'requirements.txt'
    requirements.write_text('packaging==25.0\\n' * count)
    records.append(runtime.build_runtime(root, requirements))
print(json.dumps({'policy':runtime.POLICY,'records':records}))
"""
    report = {"candidate_policy": runtime.POLICY, "candidate_builds": 0, "candidate_removals": 0}
    original_command, original_capture = runtime.command, runtime._owned_capture
    project = runtime._project(root)
    try:
        previous = subprocess.run(
            [args.previous_python, "-c", previous_code, str(root)],
            capture_output=True,
            text=True,
            timeout=360,
            check=True,
        )
        prepared = json.loads(previous.stdout)
        report["previous_policy"] = prepared["policy"]
        assert prepared["policy"] in {"docker-nonroot-ro-none-v2", "docker-nonroot-ro-none-v3"}
        assert len({record["image"] for record in prepared["records"]}) == 4
        report["previous_images"] = len(prepared["records"])
        state = root / ".pytest-telescope"
        before = (state / "runtime.json").read_bytes()
        try:
            runtime.identity(root)
        except runtime.InfrastructureError:
            report["old_execution_refused"] = True
        else:
            raise AssertionError("historical image accepted for current execution")

        def command(argv, **kwargs):
            if argv[:3] == ["docker", "image", "rm"]:
                report["candidate_removals"] += 1
            return original_command(argv, **kwargs)

        def capture(directory, data, argv, *rest, **kwargs):
            if argv[:2] == ["docker", "build"]:
                report["candidate_builds"] += 1
            return original_capture(directory, data, argv, *rest, **kwargs)

        runtime.command, runtime._owned_capture = command, capture
        try:
            replacement = runtime.build_runtime(root)
        except runtime.InfrastructureError as exc:
            report["outcome"] = "infrastructure_error"
            report["error_type"] = type(exc).__name__
            report["previous_state_preserved"] = (state / "runtime.json").read_bytes() == before
            raise
        report["outcome"] = "replaced"
        assert replacement["policy"] == runtime.POLICY
        assert runtime.identity(root)["image"] == replacement["image"]
        result = controller.run(root)
        assert result["status"] == "passed" and result["counts"]["executed"] == 1
        report["fresh_passes"] = 1
    finally:
        runtime.command, runtime._owned_capture = original_command, original_capture
        registry = root / ".pytest-telescope/images.json"
        if registry.exists():
            images = json.loads(registry.read_bytes())
            assert isinstance(images, list) and len(images) <= 4
            for image in images:
                info = json.loads(original_command(["docker", "image", "inspect", image]))[0]
                labels = info["Config"]["Labels"]
                assert info["Id"] == image and labels[runtime.LABEL + "project"] == project
                assert labels[runtime.LABEL + "policy"] in {
                    "docker-nonroot-ro-none-v2",
                    "docker-nonroot-ro-none-v3",
                    runtime.POLICY,
                }
                original_command(["docker", "image", "rm", image])
        assert not original_command(
            ["docker", "ps", "-aq", "--filter", "label=" + runtime.LABEL + "project=" + project]
        ).strip()
        assert not original_command(
            ["docker", "images", "-q", "--filter", "label=" + runtime.LABEL + "project=" + project]
        ).strip()
        report["owned_resources_removed"] = True
        report["project_digest"] = hashlib.sha256(str(root).encode()).hexdigest()
        args.receipt.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))


if __name__ == "__main__":
    main()
