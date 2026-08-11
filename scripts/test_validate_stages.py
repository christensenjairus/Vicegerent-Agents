#!/usr/bin/env python3
"""Regression tests for the side-effect-free staged-install compiler."""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest
from collections.abc import Callable


REPO = pathlib.Path(__file__).resolve().parents[1]
VALIDATOR = REPO / "scripts" / "validate-stages.py"


def run_validator(stages: str, setup: Callable[[pathlib.Path], None] | None = None) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as temporary:
        root = pathlib.Path(temporary)
        stages_file = root / "stages" / "stages.yaml"
        stages_file.parent.mkdir()
        stages_file.write_text(textwrap.dedent(stages), encoding="utf-8")
        if setup is not None:
            setup(root)
        return subprocess.run(
            [sys.executable, str(VALIDATOR), "--stages", str(stages_file), "--static-only"],
            capture_output=True,
            text=True,
            check=False,
        )


class StageManifestValidationTests(unittest.TestCase):
    def test_accepts_every_current_pinned_stage_without_fetching(self) -> None:
        result = subprocess.run(
            [sys.executable, str(VALIDATOR), "--stages", "stages/stages.yaml", "--static-only"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installer_runs_stage_validation_before_cluster_access(self) -> None:
        installer = (REPO / "scripts" / "install" / "install.sh").read_text(encoding="utf-8")
        self.assertLess(
            installer.index("validate-stages.py"),
            installer.index("kc cluster-info"),
        )

    def test_validate_script_labels_yaml_syntax_check(self) -> None:
        validator = (REPO / "scripts" / "validate.sh").read_text(encoding="utf-8")
        self.assertLess(
            validator.index('echo "INFO - Validating YAML syntax"'),
            validator.index("git ls-files -z -- '*.yaml'"),
        )

    def test_rejects_unpinned_git_stage(self) -> None:
        result = run_validator(
            """
            stages:
              - name: controllers
                actions:
                  - name: bad
                    type: helm-git
                    gitRepo: https://github.com/example/controller
                    ref: main
                    chartPath: charts/controller
                    namespace: controller-system
            """
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("immutable tag", result.stderr)

    def test_rejects_digest_as_a_helm_version(self) -> None:
        result = run_validator(
            """
            stages:
              - name: controllers
                actions:
                  - name: bad
                    type: helm-oci
                    chart: oci://example.test/controller
                    version: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
                    namespace: controller-system
            """
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("semantic release", result.stderr)

    def test_rejects_local_action_without_a_values_mode(self) -> None:
        result = run_validator(
            """
            stages:
              - name: platform
                actions:
                  - name: platform
                    type: local
                    namespace: platform-system
            """
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("machineValues: full or forEach: agents", result.stderr)

    def test_rejects_helm_stage_with_unknown_field(self) -> None:
        result = run_validator(
            """
            stages:
              - name: controllers
                actions:
                  - name: bad
                    type: helm
                    repo: https://charts.example.test
                    chart: controller
                    version: 1.2.3
                    namespace: controller-system
                    unexpected: true
            """
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown fields", result.stderr)

    def test_rejects_rollout_gate_without_wait_targets(self) -> None:
        def create_kustomize_root(root: pathlib.Path) -> None:
            (root / "stages" / "kustomize" / "driver").mkdir(parents=True)

        for field, value, expected_error in (
            ("waitResource", None, "waitResource must be a non-empty string"),
            ("waitNamespace", None, "waitNamespace must be a non-empty string"),
        ):
            with self.subTest(field=field):
                targets: dict[str, str | None] = {
                    "waitResource": "deployment/driver",
                    "waitNamespace": "driver-system",
                }
                targets[field] = value
                stages = "\n".join(
                    (
                        "stages:",
                        "  - name: crds",
                        "    actions:",
                        "      - name: driver",
                        "        type: kubectl-k",
                        "        path: stages/kustomize/driver",
                        "        gate: rollout",
                        *(
                            f"        {target}: {target_value}"
                            for target, target_value in targets.items()
                            if target_value is not None
                        ),
                    )
                )
                result = run_validator(stages, create_kustomize_root)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)

    def test_rejects_kustomize_path_outside_the_repository(self) -> None:
        result = run_validator(
            """
            stages:
              - name: crds
                actions:
                  - name: escaped
                    type: kubectl-k
                    path: ../../etc
            """
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must stay under stages/kustomize", result.stderr)

    def test_rejects_kustomize_symlink_escape(self) -> None:
        def create_escape(root: pathlib.Path) -> None:
            target = root / "stages" / "kustomize"
            target.mkdir()
            (target / "escape").symlink_to("/etc")

        result = run_validator(
            """
            stages:
              - name: crds
                actions:
                  - name: escaped
                    type: kubectl-k
                    path: stages/kustomize/escape
            """,
            create_escape,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must stay under stages/kustomize", result.stderr)


if __name__ == "__main__":
    unittest.main()
