#!/usr/bin/env python3
"""Regression tests for validate-agentgateway-crds.py."""

from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


SCRIPT = Path(__file__).with_name("validate-agentgateway-crds.py")
CRD = textwrap.dedent(
    """
    apiVersion: apiextensions.k8s.io/v1
    kind: CustomResourceDefinition
    metadata:
      name: agentgatewaybackends.agentgateway.dev
    spec:
      group: agentgateway.dev
      names:
        kind: AgentgatewayBackend
      versions:
        - name: v1alpha1
          schema:
            openAPIV3Schema:
              type: object
              properties:
                apiVersion:
                  type: string
                kind:
                  type: string
                metadata:
                  type: object
                  x-kubernetes-preserve-unknown-fields: true
                spec:
                  type: object
                  properties:
                    promptGuard:
                      type: object
                      properties:
                        response:
                          type: array
                          items:
                            type: object
                            properties:
                              regex:
                                type: object
                                x-kubernetes-preserve-unknown-fields: true
                    routes:
                      type: object
                      additionalProperties:
                        type: string
    """
)


class ValidateAgentgatewayCRDsTest(unittest.TestCase):
    def run_validator(self, rendered: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            crd_path = root / "crd.yaml"
            rendered_path = root / "rendered.yaml"
            crd_path.write_text(CRD)
            rendered_path.write_text(textwrap.dedent(rendered))
            return subprocess.run(
                [sys.executable, str(SCRIPT), str(crd_path), str(rendered_path)],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_rejects_field_not_declared_in_structural_schema(self) -> None:
        result = self.run_validator(
            """
            apiVersion: agentgateway.dev/v1alpha1
            kind: AgentgatewayBackend
            metadata:
              name: openai
            spec:
              promptGuard:
                response:
                  - regex: {}
                    rejection:
                      status: 403
                      body: blocked
            """
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("rejection", result.stderr)
        self.assertIn("Additional properties are not allowed", result.stderr)

    def test_accepts_declared_fields_maps_and_preserved_subtrees(self) -> None:
        result = self.run_validator(
            """
            apiVersion: agentgateway.dev/v1alpha1
            kind: AgentgatewayBackend
            metadata:
              name: openai
            spec:
              promptGuard:
                response:
                  - regex:
                      vendorExtension: allowed
              routes:
                /v1/responses: Responses
            """
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
