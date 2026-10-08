from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_build_report as report


class NativeReportTests(unittest.TestCase):
    def test_dep5_keeps_scope_and_uninterpreted_license_expressions(self):
        text = """Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
License: local-project-license

Files: src/*
 vendor/*
Copyright: 2026 Example
License: MIT or GPL-2+
 Full license text is not another label.
 .
 Another paragraph.

License: MIT
 The MIT text.
"""
        parsed = report.dep5_licenses(text)
        self.assertEqual(parsed["status"], "parsed_dep5")
        self.assertEqual(parsed["labels"], ["MIT", "MIT or GPL-2+", "local-project-license"])
        self.assertEqual([row["scope"] for row in parsed["declarations"]], ["header", "files", "definition"])
        self.assertEqual(parsed["declarations"][1]["files"], "src/*\nvendor/*")

    def test_legacy_text_and_unknown_format_are_not_inferred(self):
        for text in ("This library is GPL licensed.\nLicense: GPL-2+", "Format: https://example.invalid/\nLicense: MIT"):
            self.assertEqual(report.dep5_licenses(text)["status"], "unparsed_non_dep5")
            self.assertIsNone(report.dep5_licenses(text)["labels"])

    def test_ambiguous_fields_and_empty_synopsis_are_not_promoted(self):
        header = "Format: http://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        self.assertEqual(report.dep5_licenses(header + "License: MIT\nLicense: BSD-2-Clause")["status"], "unparsed_malformed_dep5")
        self.assertEqual(report.dep5_licenses(header + "License:\n Text only")["status"], "unparsed_empty_license_synopsis")
        self.assertIsNone(report.dep5_licenses(header)["labels"])

    def test_copyright_hash_alias_and_missing_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "real").mkdir()
            content = b"Legacy copyright prose without machine-readable license metadata.\n"
            (root / "real" / "copyright").write_bytes(content)
            (root / "alias").symlink_to(root / "real", target_is_directory=True)
            metadata = report.copyright_metadata("alias:amd64", root)
            self.assertEqual(metadata["sha256"], report.hashlib.sha256(content).hexdigest())
            self.assertEqual(metadata["resolved_file"], str(root / "real" / "copyright"))
            self.assertEqual(metadata["status"], "unparsed_non_dep5")
            self.assertEqual(report.copyright_metadata("missing", root)["status"], "missing_copyright_file")

    def test_copyright_escape_invalid_encoding_and_size_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "doc"
            root.mkdir()
            for name in ("escape", "bad", "large"):
                (root / name).mkdir()
            outside = root.parent / "outside"
            outside.write_text("not package documentation")
            (root / "escape" / "copyright").symlink_to(outside)
            (root / "bad" / "copyright").write_bytes(b"\xff")
            with (root / "large" / "copyright").open("wb") as large:
                large.truncate(report.MAX_METADATA_BYTES + 1)
            self.assertEqual(report.copyright_metadata("escape", root)["status"], "unreadable_outside_documentation_root")
            self.assertEqual(report.copyright_metadata("bad", root)["status"], "unparsed_non_utf8")
            self.assertEqual(report.copyright_metadata("large", root)["status"], "unparsed_size_budget_exceeded")

    def test_exact_inventory_and_installed_status(self):
        rows = report.installed_packages("libx:amd64\t1.0-1\tamd64\tinstalled\nz\t2\tall\tinstalled\n")
        self.assertEqual(rows[0]["architecture"], "amd64")
        for bad in ("x\t1\tamd64\tconfig-files", "x\t1\tamd64\tinstalled\nx\t1\tamd64\tinstalled", "x\t\tamd64\tinstalled", "../x\t1\tamd64\tinstalled"):
            with self.assertRaises(report.VerificationError):
                report.installed_packages(bad)

    def test_elf_only_direct_dependencies_no_ldd_or_closure_claim(self):
        parsed = report.parse_elf_private_headers("\n/tmp/lib.so:     file format elf64-x86-64\n  NEEDED               libm.so.6\n  NEEDED               libc.so.6\n  RUNPATH              $ORIGIN/../lib\n")
        self.assertEqual(parsed["needed"], ["libm.so.6", "libc.so.6"])
        self.assertIsNone(parsed["resolved_transitive_link_closure"])
        self.assertEqual(parsed["runpath"], "$ORIGIN/../lib")
        with self.assertRaises(report.VerificationError):
            report.parse_elf_private_headers("x: file format pei-x86-64")
        with self.assertRaises(report.VerificationError):
            report.parse_elf_private_headers("x: file format elf64-x86-64\n SONAME x\n SONAME y")

    def test_libclang_trace_requires_pg17_loaded_library_diagnostic(self):
        text = """LIBCLANG_PATH=/configured-only
Generating bindings for pg16
Bindgen found clang version 18
found libclang at /usr/lib/libclang-18.so
Generating bindings for pg17
pg_config --configure CLANG = Some(\"/usr/bin/clang-19\")
Bindgen found clang version 19.1.1
"""
        parsed = report.parse_pgrx_trace(text)
        self.assertEqual(parsed["loaded_version_outputs"], ["clang version 19.1.1"])
        self.assertEqual(parsed["reported_loaded_library_paths"], [])
        parsed = report.parse_pgrx_trace(text + "found libclang at /usr/lib/libclang-19.so\n")
        self.assertEqual(parsed["reported_loaded_library_paths"], ["/usr/lib/libclang-19.so"])
        with self.assertRaises(report.VerificationError):
            report.parse_pgrx_trace(text + "found libclang at relative.so\n")


if __name__ == "__main__":
    unittest.main()
