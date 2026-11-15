import json
import os
import shutil
import tempfile
import unittest

import vdf
from vdf.packages import (
    build_catalog,
    VDFPackageLoader,
    Schema,
    ManifestCycleError,
    MissingManifestError,
    MissingResourceError,
    BoundaryViolationError,
    VersionConflictError,
    PackageError,
)


class PackageTestBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, rel_path, content):
        path = os.path.join(self.root, rel_path)
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, "w") as fp:
            fp.write(content)
        return path

    def write_json(self, rel_path, data):
        return self.write(rel_path, json.dumps(data))

    def write_index(self, root_ref):
        return self.write("index.vdf", vdf.dumps({"root": root_ref}))

    def write_manifest(self, rel_path, data):
        return self.write(rel_path, vdf.dumps({"manifest": data}, pretty=True))

    def catalog(self, strict=False, **kwargs):
        return build_catalog(self.root, strict=strict, **kwargs)


class ExpansionTests(PackageTestBase):
    def _basic_package(self):
        self.write_index("root.vdf")
        self.write_manifest("root.vdf", {
            "version": "1.0",
            "manifests": {"common": "common.vdf"},
            "resources": {"root.txt": "1.0"},
        })
        self.write_manifest("common.vdf", {
            "version": "2.0",
            "resources": {"common.txt": "2.0"},
        })
        self.write("root.txt", "root")
        self.write("common.txt", "common")

    def test_flattens_manifests_and_resources(self):
        self._basic_package()
        catalog = self.catalog()

        self.assertTrue(catalog.ok)
        self.assertEqual(
            sorted(catalog.manifests), ["common.vdf", "root.vdf"])
        self.assertEqual(
            sorted(catalog.resources), ["common.txt", "root.txt"])
        self.assertEqual(catalog.resources["root.txt"].version, "1.0")
        self.assertEqual(catalog.resources["common.txt"].version, "2.0")
        self.assertTrue(catalog.resources["common.txt"].exists)
        self.assertEqual(catalog.manifests["common.vdf"].version, "2.0")

    def test_strict_returns_catalog_when_clean(self):
        self._basic_package()
        catalog = build_catalog(self.root, strict=True)
        self.assertTrue(catalog.ok)
        self.assertEqual(catalog.summary()["errors"], 0)

    def test_in_memory_index(self):
        self._basic_package()
        os.remove(os.path.join(self.root, "index.vdf"))
        catalog = build_catalog(
            self.root, index={"root": "root.vdf"}, strict=False)
        self.assertTrue(catalog.ok)
        self.assertIn("common.txt", catalog.resources)

    def test_json_index_and_manifest(self):
        self.write_json("index.json", {"root": "root.json"})
        self.write_json("root.json", {
            "version": "9",
            "manifests": [{"manifest": "child.json"}],
            "resources": [{"path": "data.bin", "version": "9"}],
        })
        self.write_json("child.json", {
            "resources": {"child.bin": "3"},
        })
        self.write("data.bin", "x")
        self.write("child.bin", "y")

        catalog = self.catalog()
        self.assertTrue(catalog.ok)
        self.assertEqual(
            sorted(catalog.resources), ["child.bin", "data.bin"])
        self.assertEqual(catalog.resources["data.bin"].version, "9")

    def test_json_resource_list_of_bare_strings(self):
        self.write_json("index.json", {"root": "root.json"})
        self.write_json("root.json", {"resources": ["plain.txt"]})
        self.write("plain.txt", "data")

        catalog = self.catalog()
        self.assertTrue(catalog.ok)
        entry = catalog.resources["plain.txt"]
        self.assertEqual(entry.version, None)
        self.assertTrue(entry.exists)

    def test_include_and_base_scalar_directives(self):
        self.write_index("root.vdf")
        self.write_manifest("root.vdf", {
            "#include": "inc.vdf",
            "#base": "base.vdf",
        })
        self.write_manifest("inc.vdf", {"resources": {"inc.txt": "1"}})
        self.write_manifest("base.vdf", {"resources": {"base.txt": "1"}})
        self.write("inc.txt", "")
        self.write("base.txt", "")

        catalog = self.catalog()
        self.assertTrue(catalog.ok)
        self.assertEqual(sorted(catalog.resources), ["base.txt", "inc.txt"])


class SharingTests(PackageTestBase):
    def _diamond_package(self):
        # root -> a -> shared
        # root -> b -> shared
        self.write_index("root.vdf")
        self.write_manifest("root.vdf", {
            "manifests": {"a": "a.vdf", "b": "b.vdf"},
        })
        self.write_manifest("a.vdf", {
            "manifests": {"s": "shared.vdf"},
        })
        self.write_manifest("b.vdf", {
            "manifests": {"s": "shared.vdf"},
        })
        self.write_manifest("shared.vdf", {
            "version": "7",
            "resources": {"shared.txt": "7"},
        })
        self.write("shared.txt", "data")

    def test_shared_manifest_parsed_once_but_visited_twice(self):
        self._diamond_package()
        catalog = self.catalog()

        self.assertTrue(catalog.ok)
        self.assertEqual(catalog.manifests_parsed, 4)
        self.assertEqual(catalog.manifest_visits, 5)
        self.assertIn("shared.vdf", catalog.manifests)

    def test_shared_manifest_keeps_every_reference_path(self):
        self._diamond_package()
        catalog = self.catalog()

        node = catalog.manifests["shared.vdf"]
        self.assertEqual(sorted(node.referenced_by), [
            ("root.vdf", "a.vdf", "shared.vdf"),
            ("root.vdf", "b.vdf", "shared.vdf"),
        ])

        entry = catalog.resources["shared.txt"]
        self.assertEqual(sorted(entry.references), [
            ("root.vdf", "a.vdf", "shared.vdf"),
            ("root.vdf", "b.vdf", "shared.vdf"),
        ])
        self.assertEqual(entry.origins, ["shared.vdf"])

    def test_same_version_shared_resource_is_not_a_conflict(self):
        self._diamond_package()
        catalog = self.catalog()
        entry = catalog.resources["shared.txt"]
        self.assertFalse(entry.has_conflict)
        self.assertEqual(entry.versions, ["7"])

    def test_nested_relative_paths_resolve_per_parent(self):
        self.write("index.vdf", vdf.dumps({"root": "manifests/root.vdf"}))
        self.write_manifest("manifests/root.vdf", {
            "manifests": {"child": "child.vdf"},
        })
        self.write_manifest("manifests/child.vdf", {
            "resources": {"payload.bin": "1"},
        })
        self.write("manifests/payload.bin", "payload")

        catalog = self.catalog()
        self.assertTrue(catalog.ok)
        self.assertEqual(
            sorted(catalog.manifests),
            ["manifests/child.vdf", "manifests/root.vdf"])
        self.assertIn("manifests/payload.bin", catalog.resources)


class CycleTests(PackageTestBase):
    def _cyclic_package(self):
        self.write_index("a.vdf")
        self.write_manifest("a.vdf", {"manifests": {"b": "b.vdf"}})
        self.write_manifest("b.vdf", {"manifests": {"back": "a.vdf"}})

    def test_cycle_is_reported_and_terminates(self):
        self._cyclic_package()
        catalog = self.catalog()

        self.assertFalse(catalog.ok)
        self.assertEqual(len(catalog.errors), 1)
        error = catalog.errors[0]
        self.assertIsInstance(error, ManifestCycleError)
        self.assertEqual(error.reference, "a.vdf")
        self.assertEqual(
            error.chain, ("a.vdf", "b.vdf", "a.vdf"))

    def test_cycle_strict_raises(self):
        self._cyclic_package()
        with self.assertRaises(ManifestCycleError):
            build_catalog(self.root, strict=True)

    def test_self_referencing_manifest_is_a_cycle(self):
        self.write_index("loop.vdf")
        self.write_manifest("loop.vdf", {
            "manifests": {"self": "loop.vdf"},
        })
        catalog = self.catalog()
        self.assertIsInstance(catalog.errors[0], ManifestCycleError)


class MissingTests(PackageTestBase):
    def test_missing_manifest_is_reported_but_expansion_continues(self):
        self.write_index("root.vdf")
        self.write_manifest("root.vdf", {
            "manifests": {"ghost": "ghost.vdf", "real": "real.vdf"},
        })
        self.write_manifest("real.vdf", {"resources": {"real.txt": "1"}})
        self.write("real.txt", "")

        catalog = self.catalog()
        self.assertEqual(len(catalog.errors), 1)
        self.assertIsInstance(catalog.errors[0], MissingManifestError)
        self.assertEqual(catalog.errors[0].reference, "ghost.vdf")
        self.assertIn("real.txt", catalog.resources)
        self.assertNotIn("ghost.vdf", catalog.manifests)

    def test_missing_manifest_strict_raises(self):
        self.write_index("root.vdf")
        self.write_manifest("root.vdf", {
            "manifests": {"ghost": "ghost.vdf"},
        })
        with self.assertRaises(MissingManifestError):
            build_catalog(self.root, strict=True)

    def test_missing_resource_is_reported_and_marked_absent(self):
        self.write_index("root.vdf")
        self.write_manifest("root.vdf", {
            "resources": {"present.txt": "1", "absent.txt": "1"},
        })
        self.write("present.txt", "")

        catalog = self.catalog()
        self.assertTrue(catalog.resources["present.txt"].exists)
        self.assertFalse(catalog.resources["absent.txt"].exists)
        self.assertEqual(len(catalog.errors), 1)
        error = catalog.errors[0]
        self.assertIsInstance(error, MissingResourceError)
        self.assertEqual(error.reference, "absent.txt")
        self.assertEqual(error.origin, "root.vdf")

    def test_missing_index_raises_package_error(self):
        with self.assertRaises(PackageError):
            build_catalog(self.root, strict=False)


class VersionConflictTests(PackageTestBase):
    def _conflicting_package(self):
        self.write_index("root.vdf")
        self.write_manifest("root.vdf", {
            "manifests": {"p1": "p1.vdf", "p2": "p2.vdf"},
        })
        self.write_manifest("p1.vdf", {"resources": {"shared.dat": "1"}})
        self.write_manifest("p2.vdf", {"resources": {"shared.dat": "2"}})
        self.write("shared.dat", "bytes")

    def test_same_name_different_versions_conflict(self):
        self._conflicting_package()
        catalog = self.catalog()

        entry = catalog.resources["shared.dat"]
        self.assertTrue(entry.has_conflict)
        self.assertEqual(entry.versions, ["1", "2"])
        self.assertEqual(len(catalog.errors), 1)
        error = catalog.errors[0]
        self.assertIsInstance(error, VersionConflictError)
        self.assertEqual(error.name, "shared.dat")
        self.assertEqual(error.versions, ("1", "2"))
        self.assertEqual(len(error.reference_chains), 2)

    def test_conflict_strict_raises(self):
        self._conflicting_package()
        with self.assertRaises(VersionConflictError):
            build_catalog(self.root, strict=True)

    def test_distinct_files_with_same_basename_do_not_conflict(self):
        self.write_index("root.vdf")
        self.write_manifest("root.vdf", {
            "manifests": {"p1": "p1/manifest.vdf", "p2": "p2/manifest.vdf"},
        })
        self.write_manifest("p1/manifest.vdf", {"resources": {"data.bin": "1"}})
        self.write_manifest("p2/manifest.vdf", {"resources": {"data.bin": "2"}})
        self.write("p1/data.bin", "a")
        self.write("p2/data.bin", "b")

        catalog = self.catalog()
        self.assertTrue(catalog.ok)
        self.assertIn("p1/data.bin", catalog.resources)
        self.assertIn("p2/data.bin", catalog.resources)


class BoundaryTests(PackageTestBase):
    def test_manifest_reference_with_parent_dir_stops_at_boundary(self):
        self.write_index("root.vdf")
        self.write_manifest("root.vdf", {
            "manifests": {"out": "../outside.vdf"},
        })
        outside = os.path.join(os.path.dirname(self.root), "outside.vdf")
        with open(outside, "w") as fp:
            fp.write(vdf.dumps({"manifest": {}}))

        catalog = self.catalog()
        self.assertEqual(len(catalog.errors), 1)
        self.assertIsInstance(catalog.errors[0], BoundaryViolationError)
        self.assertNotIn("outside.vdf", catalog.manifests)

        os.remove(outside)

    def test_resource_reference_with_parent_dir_stops_at_boundary(self):
        self.write_index("root.vdf")
        self.write_manifest("root.vdf", {
            "resources": {"../secret.txt": "1"},
        })
        secret = os.path.join(os.path.dirname(self.root), "secret.txt")
        with open(secret, "w") as fp:
            fp.write("secret")

        catalog = self.catalog()
        self.assertIsInstance(catalog.errors[0], BoundaryViolationError)
        self.assertEqual(catalog.resources, {})

        os.remove(secret)

    def test_absolute_reference_stops_at_boundary(self):
        self.write_index("root.vdf")
        self.write_manifest("root.vdf", {
            "manifests": {"out": "/etc/hosts"},
        })
        catalog = self.catalog()
        self.assertIsInstance(catalog.errors[0], BoundaryViolationError)

    def test_symlink_escaping_root_stops_at_boundary(self):
        outside = os.path.join(os.path.dirname(self.root), "linked.vdf")
        with open(outside, "w") as fp:
            fp.write(vdf.dumps({"manifest": {}}))
        link = os.path.join(self.root, "link.vdf")
        os.symlink(outside, link)

        self.write_index("root.vdf")
        self.write_manifest("root.vdf", {
            "manifests": {"link": "link.vdf"},
        })

        catalog = self.catalog()
        self.assertIsInstance(catalog.errors[0], BoundaryViolationError)
        os.remove(outside)


class SchemaAndNormalizationTests(PackageTestBase):
    def test_custom_schema_block_and_target_keys(self):
        schema = Schema(
            index_root_keys=("entrypoint",),
            submanifest_blocks=("deps",),
            resource_blocks=("payload",),
            manifest_target_keys=("source",),
        )
        self.write("index.vdf", vdf.dumps({"entrypoint": "root.vdf"}))
        self.write_manifest("root.vdf", {
            "deps": {"child": {"source": "child.vdf"}},
            "payload": {
                "thing.bin": {"path": "thing.bin", "version": "4"},
            },
        })
        self.write_manifest("child.vdf", {})
        self.write("thing.bin", "x")

        catalog = build_catalog(self.root, strict=False, schema=schema)
        self.assertTrue(catalog.ok)
        self.assertIn("child.vdf", catalog.manifests)
        self.assertEqual(catalog.resources["thing.bin"].version, "4")

    def test_loader_normalizes_scalar_resource_mapping(self):
        loader = VDFPackageLoader()
        normalized = loader.normalize_manifest({
            "version": "1",
            "resources": {"a.txt": "1", "b.txt": "2"},
        })
        refs = {item["ref"]: item["version"]
                for item in normalized["resources"]}
        self.assertEqual(refs, {"a.txt": "1", "b.txt": "2"})

    def test_loader_normalizes_manifest_mapping(self):
        loader = VDFPackageLoader()
        normalized = loader.normalize_manifest({
            "manifests": {
                "child": {"manifest": "child.vdf", "version": "5"},
            },
        })
        self.assertEqual(normalized["manifests"],
                         [{"ref": "child.vdf", "version": "5"}])

    def test_digest_is_captured(self):
        self.write_index("root.vdf")
        self.write_manifest("root.vdf", {
            "resources": {
                "f.bin": {
                    "path": "f.bin",
                    "version": "1",
                    "sha256": "deadbeef",
                },
            },
        })
        self.write("f.bin", "")
        catalog = self.catalog()
        self.assertEqual(catalog.resources["f.bin"].digest, "deadbeef")

    def test_external_digest_map_is_used(self):
        self.write_index("root.vdf")
        self.write_manifest("root.vdf", {"resources": {"f.bin": "1"}})
        self.write("f.bin", "")
        catalog = build_catalog(
            self.root, strict=False, digests={"f.bin": "abc123"})
        self.assertEqual(catalog.resources["f.bin"].digest, "abc123")


class CatalogContractTests(PackageTestBase):
    def test_summary_counts_and_repr_strings(self):
        self.write_index("root.vdf")
        self.write_manifest("root.vdf", {"resources": {"a.txt": "1"}})
        self.write("a.txt", "")
        catalog = self.catalog()

        summary = catalog.summary()
        self.assertEqual(summary["manifests"], 1)
        self.assertEqual(summary["manifests_parsed"], 1)
        self.assertEqual(summary["manifest_visits"], 1)
        self.assertEqual(summary["resources"], 1)
        self.assertEqual(summary["errors"], 0)
        self.assertIn("ResourceEntry", repr(catalog.resources["a.txt"]))
        self.assertIn("ManifestNode", repr(catalog.manifests["root.vdf"]))

    def test_vdf_module_still_parses(self):
        self.assertEqual(vdf.loads('"k" "v"'), {"k": "v"})


if __name__ == "__main__":
    unittest.main()
