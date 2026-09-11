import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ppx"))
SPEC = importlib.util.spec_from_file_location("ppx_client", ROOT / "ppx" / "ppx.py")
ppx = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ppx)


class PPXTests(unittest.TestCase):
    def test_semver_ranges_and_stable_preference(self):
        self.assertTrue(ppx.satisfies("1.4.5", "^1.4.0"))
        self.assertFalse(ppx.satisfies("2.0.0", "^1.4.0"))
        chosen = ppx.choose_version(
            {"name": "demo", "versions": [
                {"version": "1.5.0-dev.1", "yanked": False},
                {"version": "1.4.5", "yanked": False},
            ]},
            None,
        )
        self.assertEqual(chosen["version"], "1.4.5")

    def test_dependency_paths_are_sorted_unique_and_require_materialization(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a").mkdir()
            (root / "b").mkdir()
            (root / "Punpun.toml").write_text(
                '[package]\nname="app"\nversion="0.1.0"\n\n[dependencies]\n'
                'b={path="b"}\na={path="a"}\nagain={path="a"}\n',
                encoding="utf-8",
            )
            self.assertEqual(ppx.dependency_paths(root), [(root / "a").resolve(), (root / "b").resolve()])
            (root / "Punpun.toml").write_text(
                '[package]\nname="app"\nversion="0.1.0"\n\n[dependencies]\nmissing="^1.0.0"\n',
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                ppx.dependency_paths(root)

    def test_archive_is_reproducible_and_verifiable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Punpun.toml").write_text(
                '[package]\nname="demo"\nversion="1.0.0"\ndescription="demo"\n\n[dependencies]\n',
                encoding="utf-8",
            )
            (root / "main.pp").write_text('launch { say("ok"); }\n', encoding="utf-8")
            first, first_hash = ppx.create_package_archive(root)
            os.utime(root / "main.pp", None)
            second, second_hash = ppx.create_package_archive(root)
            self.assertEqual(first, second)
            self.assertEqual(first_hash, second_hash)
            archive = root / "demo.zip"
            archive.write_bytes(first)
            extracted = root / "extracted"
            ppx.safe_extract_zip(archive, extracted)
            self.assertEqual(ppx.verify_package_tree(extracted)["format"], 1)


if __name__ == "__main__":
    unittest.main()
