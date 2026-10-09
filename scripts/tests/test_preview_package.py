"""Real archive/staging tests with tiny files; no Docker or installed PG writes."""
import importlib.util
import gzip
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("preview_package", Path(__file__).parents[1] / "preview_package.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PreviewPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inputs = []
        for index, (name, mode) in enumerate(MODULE.INSTALL_PATHS.items()):
            path = self.root / str(index)
            path.write_bytes(b"native-test-" + str(index).encode())
            self.inputs.append((name, path, mode))
        self.archive = self.root / "preview.tar.gz"
        self.manifest = MODULE.write_archive(self.archive, self.inputs, {"source_commit": "a" * 40})

    def rewritten(self, transform):
        destination = self.root / "changed.tar.gz"
        with tarfile.open(self.archive, "r:gz") as original, tarfile.open(destination, "w:gz") as result:
            rows = [(member, original.extractfile(member).read()) for member in original]
            for member, data in transform(rows):
                result.addfile(member, io.BytesIO(data))
        return destination

    def test_archive_metadata_is_deterministic_and_never_release_supported(self):
        other = self.root / "repeat.tar.gz"
        MODULE.write_archive(other, list(reversed(self.inputs)), {"source_commit": "a" * 40})
        self.assertEqual(self.archive.read_bytes(), other.read_bytes())
        self.assertFalse(MODULE.verify_archive(other)["release_supported"])

    def test_stage_installs_exact_four_members_and_refuses_existing_tree(self):
        destination = self.root / "stage"
        MODULE.stage_archive(self.archive, destination)
        for name, source, mode in self.inputs:
            self.assertEqual((destination / name).read_bytes(), source.read_bytes())
            if os.name == "posix":
                self.assertEqual((destination / name).stat().st_mode & 0o777, mode)
        with self.assertRaises(FileExistsError):
            MODULE.stage_archive(self.archive, destination)

    def test_changed_content_is_rejected_before_creating_staging_tree(self):
        def alter(rows):
            member, data = rows[0]
            rows[0] = member, b"X" + data[1:]
            return rows
        destination = self.root / "refused"
        with self.assertRaisesRegex(ValueError, "checksum"):
            MODULE.stage_archive(self.rewritten(alter), destination)
        self.assertFalse(destination.exists())

    def test_absolute_traversal_and_ambiguous_names_are_rejected(self):
        for name in ("/usr/x", "../x", "a/../x", "a//x", "a/./x", "a\\x", "C:/x", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                MODULE.relative_name(name)

    def test_symlinks_and_special_archive_members_are_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            def alter(rows):
                member, _ = rows[0]
                member.type, member.size, member.linkname = kind, 0, "/outside"
                rows[0] = member, b""
                return rows
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "nonregular"):
                MODULE.verify_archive(self.rewritten(alter))

    def test_duplicate_archive_members_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            MODULE.verify_archive(self.rewritten(lambda rows: rows + [rows[0]]))

    def test_extended_header_is_refused_before_its_body_can_be_read(self):
        for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
            for size in (1, MODULE.MAX_FILE + 1):
                path = self.root / (kind.decode() + str(size) + ".tar.gz")
                header = tarfile.TarInfo("extended")
                header.type, header.size = kind, size
                with gzip.open(path, "wb") as stream:
                    # No body exists: refusal must precede parsing or allocation.
                    stream.write(header.tobuf(format=tarfile.USTAR_FORMAT))
                with self.subTest(kind=kind, size=size), self.assertRaisesRegex(ValueError, "header"):
                    MODULE.verify_archive(path)

    def test_manifest_cannot_omit_a_native_member(self):
        def alter(rows):
            for index, (member, data) in enumerate(rows):
                if member.name == "manifest.json":
                    value = json.loads(data)
                    value["files"].pop()
                    data = MODULE.canonical(value)
                    member.size = len(data)
                    rows[index] = member, data
            return rows
        with self.assertRaisesRegex(ValueError, "membership"):
            MODULE.verify_archive(self.rewritten(alter))

    def test_changed_executable_mode_is_rejected(self):
        def alter(rows):
            rows[0][0].mode = 0o777
            return rows
        with self.assertRaisesRegex(ValueError, "metadata"):
            MODULE.verify_archive(self.rewritten(alter))

    def test_actual_archive_inspection_enforces_total_and_member_budgets(self):
        for name, limit in (("MAX_TOTAL", 5), ("MAX_MEMBERS", 2), ("MAX_FILE", 2)):
            with self.subTest(bound=name), patch.object(MODULE, name, limit), self.assertRaises(ValueError):
                MODULE.verify_archive(self.archive)

    def test_incomplete_product_archive_cannot_publish(self):
        output = self.root / "incomplete.tar.gz"
        with self.assertRaisesRegex(ValueError, "incomplete installed"):
            MODULE.write_archive(output, self.inputs[:-1], {})
        self.assertFalse(output.exists())

    def test_source_symlink_is_refused(self):
        link = self.root / "linked"
        link.symlink_to(self.inputs[0][1])
        with self.assertRaisesRegex(ValueError, "symlink"):
            MODULE.checked_file(self.root, "linked")


if __name__ == "__main__":
    unittest.main()
