#!/usr/bin/env python3
import hashlib
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("generate.py")
spec = importlib.util.spec_from_file_location("host_brew_generate", MODULE_PATH)
assert spec is not None and spec.loader is not None
generate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = generate
spec.loader.exec_module(generate)


class FakeFetcher:
    def __init__(self, blobs=None, documents=None):
        self.blobs = blobs or {}
        self.documents = documents or {}
        self.requested = []

    def bytes(self, url):
        self.requested.append(url)
        return self.blobs[url]

    def json(self, url):
        self.requested.append(url)
        return self.documents[url]


class GenerateTests(unittest.TestCase):
    def _repo(self, package, template):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "host/brew/templates").mkdir(parents=True)
        (root / "Formula").mkdir()
        (root / "host/brew/templates" / f"{package['name']}.rb.in").write_text(template)
        manifest = {
            "schemaVersion": 1,
            "tap": {"name": "vicegerent/packages", "url": "git@example/repo.git"},
            "packages": [package],
        }
        manifest_path = root / "host/brew/packages.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        return tmp, root, manifest_path

    def test_github_archive_generates_new_formula_and_updates_manifest(self):
        package = {
            "name": "rclone",
            "source": "rclone/rclone",
            "formula": "vicegerent/packages/rclone@1.75.0",
            "version": "1.76.0",
            "generator": {"type": "github-archive", "tagPrefix": "v"},
        }
        template = "class @CLASS_NAME@ < Formula\n  url \"@URL@\"\n  version \"@VERSION@\"\n  sha256 \"@SHA256@\"\nend\n"
        tmp, root, manifest_path = self._repo(package, template)
        self.addCleanup(tmp.cleanup)
        old_formula = root / "Formula/rclone@1.75.0.rb"
        old_formula.write_text("old formula\n")
        url = "https://github.com/rclone/rclone/archive/refs/tags/v1.76.0.tar.gz"
        fetcher = FakeFetcher(blobs={url: b"rclone archive"})
        original_manifest = manifest_path.read_text()

        changed = generate.generate_updates(manifest_path, root, fetcher=fetcher)

        expected_sha = hashlib.sha256(b"rclone archive").hexdigest()
        new_formula = root / "Formula/rclone@1.76.0.rb"
        self.assertEqual(changed, [new_formula, manifest_path])
        self.assertIn("class RcloneAT1760 < Formula", new_formula.read_text())
        self.assertIn(expected_sha, new_formula.read_text())
        self.assertTrue(old_formula.is_file())
        updated = json.loads(manifest_path.read_text())
        self.assertEqual(updated["packages"][0]["formula"], "vicegerent/packages/rclone@1.76.0")
        self.assertEqual(
            manifest_path.read_text(),
            original_manifest.replace(
                '"formula": "vicegerent/packages/rclone@1.75.0"',
                '"formula": "vicegerent/packages/rclone@1.76.0"',
            ),
        )

    def test_existing_versioned_formula_is_never_overwritten(self):
        package = {
            "name": "rclone",
            "source": "rclone/rclone",
            "formula": "vicegerent/packages/rclone@1.75.0",
            "version": "1.76.0",
            "generator": {"type": "github-archive", "tagPrefix": "v"},
        }
        template = "@URL@ @SHA256@\n"
        tmp, root, manifest_path = self._repo(package, template)
        self.addCleanup(tmp.cleanup)
        target = root / "Formula/rclone@1.76.0.rb"
        target.write_text("immutable historical formula\n")
        url = "https://github.com/rclone/rclone/archive/refs/tags/v1.76.0.tar.gz"
        fetcher = FakeFetcher(blobs={url: b"different archive"})

        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            generate.generate_updates(manifest_path, root, fetcher=fetcher)

        self.assertEqual(target.read_text(), "immutable historical formula\n")

    def test_github_release_assets_generate_each_url_and_checksum(self):
        package = {
            "name": "toolhive",
            "source": "stacklok/toolhive",
            "formula": "vicegerent/packages/thv@0.42.0",
            "version": "0.43.0",
            "generator": {
                "type": "github-release-assets",
                "tagPrefix": "v",
                "assets": {
                    "DARWIN_ARM64": "toolhive_{version}_darwin_arm64.tar.gz",
                    "LINUX_AMD64": "toolhive_{version}_linux_amd64.tar.gz"
                }
            },
        }
        template = "@URL_DARWIN_ARM64@ @SHA_DARWIN_ARM64@\n@URL_LINUX_AMD64@ @SHA_LINUX_AMD64@\n"
        tmp, root, manifest_path = self._repo(package, template)
        self.addCleanup(tmp.cleanup)
        base = "https://github.com/stacklok/toolhive/releases/download/v0.43.0"
        blobs = {
            f"{base}/toolhive_0.43.0_darwin_arm64.tar.gz": b"darwin",
            f"{base}/toolhive_0.43.0_linux_amd64.tar.gz": b"linux",
        }
        fetcher = FakeFetcher(blobs=blobs)

        generate.generate_updates(manifest_path, root, fetcher=fetcher)

        content = (root / "Formula/thv@0.43.0.rb").read_text()
        for url, blob in blobs.items():
            self.assertIn(url, content)
            self.assertIn(hashlib.sha256(blob).hexdigest(), content)

    def test_pypi_sdist_uses_published_url_and_digest(self):
        package = {
            "name": "supervisor",
            "source": "Supervisor/supervisor",
            "formula": "vicegerent/packages/supervisor@4.3.0",
            "version": "4.3.1",
            "generator": {"type": "pypi-sdist", "project": "supervisor"},
        }
        template = "@URL@ @SHA256@ @VERSION@ @CLASS_NAME@\n"
        tmp, root, manifest_path = self._repo(package, template)
        self.addCleanup(tmp.cleanup)
        api_url = "https://pypi.org/pypi/supervisor/4.3.1/json"
        sdist = b"supervisor sdist"
        digest = hashlib.sha256(sdist).hexdigest()
        sdist_url = "https://files.pythonhosted.org/packages/supervisor-4.3.1.tar.gz"
        fetcher = FakeFetcher(documents={api_url: {
            "urls": [{
                "packagetype": "sdist",
                "url": sdist_url,
                "digests": {"sha256": digest},
            }]
        }}, blobs={sdist_url: sdist})

        generate.generate_updates(manifest_path, root, fetcher=fetcher)

        content = (root / "Formula/supervisor@4.3.1.rb").read_text()
        self.assertEqual(
            content,
            f"https://files.pythonhosted.org/packages/supervisor-4.3.1.tar.gz {digest} 4.3.1 SupervisorAT431\n",
        )

    def test_pypi_sdist_rejects_non_pythonhosted_url(self):
        package = {
            "name": "supervisor",
            "source": "Supervisor/supervisor",
            "formula": "vicegerent/packages/supervisor@4.3.0",
            "version": "4.3.1",
            "generator": {"type": "pypi-sdist", "project": "supervisor"},
        }
        tmp, root, manifest_path = self._repo(package, "@URL@ @SHA256@\n")
        self.addCleanup(tmp.cleanup)
        content = b"not a release"
        api_url = "https://pypi.org/pypi/supervisor/4.3.1/json"
        fetcher = FakeFetcher(
            documents={
                api_url: {
                    "urls": [
                        {
                            "packagetype": "sdist",
                            "url": "file:///etc/passwd",
                            "digests": {"sha256": hashlib.sha256(content).hexdigest()},
                        }
                    ]
                }
            },
            blobs={"file:///etc/passwd": content},
        )

        with self.assertRaisesRegex(ValueError, "invalid PyPI sdist URL"):
            generate.generate_updates(manifest_path, root, fetcher=fetcher)
        self.assertNotIn("file:///etc/passwd", fetcher.requested)

    def test_verify_redownloads_artifact_and_rejects_formula_drift(self):
        package = {
            "name": "rclone",
            "source": "rclone/rclone",
            "formula": "vicegerent/packages/rclone@1.76.0",
            "version": "1.76.0",
            "generator": {"type": "github-archive", "tagPrefix": "v"},
        }
        template = "@URL@ @SHA256@\n"
        tmp, root, manifest_path = self._repo(package, template)
        self.addCleanup(tmp.cleanup)
        (root / "Formula/rclone@1.76.0.rb").write_text("tampered\n")
        url = "https://github.com/rclone/rclone/archive/refs/tags/v1.76.0.tar.gz"
        fetcher = FakeFetcher(blobs={url: b"rclone archive"})

        with self.assertRaisesRegex(ValueError, "does not match regenerated content"):
            generate.generate_updates(manifest_path, root, fetcher=fetcher, verify=True)

        self.assertEqual(fetcher.requested, [url])

    def test_aligned_manifest_is_a_noop_without_network(self):
        package = {
            "name": "rclone",
            "source": "rclone/rclone",
            "formula": "vicegerent/packages/rclone@1.76.0",
            "version": "1.76.0",
            "generator": {"type": "github-archive", "tagPrefix": "v"},
        }
        tmp, root, manifest_path = self._repo(package, "unused")
        self.addCleanup(tmp.cleanup)
        (root / "Formula/rclone@1.76.0.rb").write_text("existing\n")
        fetcher = FakeFetcher()

        changed = generate.generate_updates(manifest_path, root, fetcher=fetcher)

        self.assertEqual(changed, [])
        self.assertEqual(fetcher.requested, [])

    def test_rejects_unsafe_formula_path(self):
        package = {
            "name": "rclone",
            "source": "rclone/rclone",
            "formula": "vicegerent/packages/../../rclone@1.75.0",
            "version": "1.76.0",
            "generator": {"type": "github-archive", "tagPrefix": "v"},
        }
        tmp, root, manifest_path = self._repo(package, "unused")
        self.addCleanup(tmp.cleanup)

        with self.assertRaisesRegex(ValueError, "invalid formula reference"):
            generate.generate_updates(manifest_path, root, fetcher=FakeFetcher())

    def test_rejects_formula_class_collision_with_retained_version(self):
        package = {
            "name": "rclone",
            "source": "rclone/rclone",
            "formula": "vicegerent/packages/rclone@1.75.0",
            "version": "1.76.0",
            "generator": {"type": "github-archive", "tagPrefix": "v"},
        }
        tmp, root, manifest_path = self._repo(package, "unused")
        self.addCleanup(tmp.cleanup)
        (root / "Formula/rclone@1.7.60.rb").write_text("class RcloneAT1760 < Formula\nend\n")

        with self.assertRaisesRegex(ValueError, "formula class RcloneAT1760.*collides"):
            generate.generate_updates(manifest_path, root, fetcher=FakeFetcher())

    def test_rejects_renovate_datasource_that_disagrees_with_generator(self):
        package = {
            "name": "supervisor",
            "source": "Supervisor/supervisor",
            "renovateDatasource": "github-releases",
            "renovateDependency": "Supervisor/supervisor",
            "formula": "vicegerent/packages/supervisor@4.3.0",
            "version": "4.3.1",
            "generator": {"type": "pypi-sdist", "project": "supervisor"},
        }
        tmp, root, manifest_path = self._repo(package, "unused")
        self.addCleanup(tmp.cleanup)

        with self.assertRaisesRegex(ValueError, "must use pypi:supervisor"):
            generate.generate_updates(manifest_path, root, fetcher=FakeFetcher())

    def test_rejects_formula_changes_outside_current_manifest_formulae(self):
        manifest = {
            "packages": [
                {"formula": "vicegerent/packages/rclone@1.76.0"},
                {"formula": "vicegerent/packages/thv@0.43.0"},
            ]
        }
        generate.validate_formula_change_scope(
            manifest,
            ["Formula/rclone@1.76.0.rb", "Formula/thv@0.43.0.rb"],
        )

        with self.assertRaisesRegex(ValueError, "Formula/rogue@1.0.0.rb"):
            generate.validate_formula_change_scope(
                manifest,
                ["Formula/rclone@1.76.0.rb", "Formula/rogue@1.0.0.rb"],
            )
    def test_checked_in_templates_reproduce_current_formulae(self):
        root = Path(__file__).resolve().parents[2]
        manifest = json.loads((root / "host/brew/packages.json").read_text())
        for package in manifest["packages"]:
            with self.subTest(package=package["name"]):
                short_formula = package["formula"].rsplit("/", 1)[1]
                formula = (root / "Formula" / f"{short_formula}.rb").read_text()
                rendered = (root / "host/brew/templates" / f"{package['name']}.rb.in").read_text()
                tokens = {
                    "VERSION": package["version"],
                    "CLASS_NAME": generate._formula_class_name(short_formula),
                }
                urls = re.findall(r'^\s*url "([^"]+)"', formula, re.MULTILINE)
                checksums = re.findall(r'^\s*sha256 "([0-9a-f]{64})"', formula, re.MULTILINE)
                if package["generator"]["type"] == "github-release-assets":
                    keys = list(package["generator"]["assets"])
                    self.assertEqual(len(urls), len(keys))
                    self.assertEqual(len(checksums), len(keys))
                    for key, url, checksum in zip(keys, urls, checksums, strict=True):
                        tokens[f"URL_{key}"] = url
                        tokens[f"SHA_{key}"] = checksum
                else:
                    self.assertEqual(len(urls), 1)
                    self.assertEqual(len(checksums), 1)
                    tokens.update(URL=urls[0], SHA256=checksums[0])
                for key, value in tokens.items():
                    rendered = rendered.replace(f"@{key}@", value)
                self.assertEqual(rendered, formula)


if __name__ == "__main__":
    unittest.main()
