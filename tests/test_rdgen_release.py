import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "rdgen_release.py"
SPEC = importlib.util.spec_from_file_location("rdgen_release", SCRIPT)
assert SPEC and SPEC.loader
rdgen_release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rdgen_release
SPEC.loader.exec_module(rdgen_release)


class VersionDetectionTests(unittest.TestCase):
    def test_reads_stable_versions_from_rdgen_form_choices(self):
        source = '''
from django import forms
class GenerateForm(forms.Form):
    version = forms.ChoiceField(choices=[('master', 'nightly'), ('1.5.0', '1.5.0'), ('1.4.9', 'old')])
'''
        self.assertEqual(rdgen_release.extract_supported_versions(source), ["1.4.9", "1.5.0"])

    def test_builds_only_when_rdgen_support_list_is_newer_than_release(self):
        self.assertIsNone(rdgen_release.select_build_version(["1.5.0"], ["1.5.0"]))
        self.assertEqual(
            rdgen_release.select_build_version(["1.5.0", "1.5.1"], ["1.5.0"]),
            "1.5.1",
        )
        self.assertEqual(rdgen_release.select_build_version(["1.5.0"], []), "1.5.0")
        self.assertIsNone(rdgen_release.select_build_version(["1.5.0"], [], "1.5.0"))
        self.assertIsNone(rdgen_release.select_build_version(["1.5.0"], ["1.5.0"], "1.5.0"))
        self.assertIsNone(rdgen_release.select_build_version(["1.5.0"], ["1.6.0"], "1.5.0"))
        self.assertIsNone(rdgen_release.select_build_version(["1.4.9"], [], "1.5.0"))
        self.assertEqual(
            rdgen_release.select_build_version(["1.5.0", "1.5.1"], [], "1.5.0"),
            "1.5.1",
        )

    def test_ignores_non_rdgen_and_non_stable_release_tags(self):
        releases = [
            {"tag_name": "v9.9.9"},
            {"tag_name": "rdgen-rustdesk-1.5.0-beta.1"},
            {"tag_name": "rdgen-rustdesk-1.5.0", "assets": [{"name": "rustdesk.exe"}]},
            {
                "tag_name": "rdgen-rustdesk-1.4.9",
                "assets": [{"name": "rustdesk.exe"}, {"name": "rustdesk-arm64.apk"}],
            },
        ]
        self.assertEqual(rdgen_release.published_versions(releases), ["1.4.9"])

    def test_fails_closed_when_version_choice_is_missing(self):
        with self.assertRaises(ValueError):
            rdgen_release.extract_supported_versions("class GenerateForm: pass\n")


class ConfigurationTests(unittest.TestCase):
    def test_fixed_server_values_are_emitted_without_unsigned_custom_payload(self):
        env = {
            "RUSTDESK_SERVER": "rustdesk.xiongmx.com",
            "RUSTDESK_SERVER_PORT": "21116",
            "RUSTDESK_API_SERVER": "https://rustdesk.xiongmx.com",
            "RUSTDESK_PUBLIC_KEY": "LOesLO6ZiBDbvmrUhwMjjTl240UQWziRe7nWrvmOpdY=",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            config = rdgen_release.build_config()
        self.assertEqual(config["server"], env["RUSTDESK_SERVER"])
        self.assertEqual(config["serverPort"], "21116")
        self.assertEqual(config["apiServer"], env["RUSTDESK_API_SERVER"])
        self.assertEqual(config["key"], env["RUSTDESK_PUBLIC_KEY"])
        self.assertEqual(config["custom"], "")
        self.assertEqual(config["rdgen"], "false")
        self.assertEqual(config["genurl"], "")

    def test_rejects_invalid_port_and_key(self):
        base = {
            "RUSTDESK_SERVER": "rustdesk.xiongmx.com",
            "RUSTDESK_SERVER_PORT": "21116",
            "RUSTDESK_API_SERVER": "https://rustdesk.xiongmx.com",
            "RUSTDESK_PUBLIC_KEY": "LOesLO6ZiBDbvmrUhwMjjTl240UQWziRe7nWrvmOpdY=",
        }
        for field, value in (
            ("RUSTDESK_SERVER_PORT", "bad"),
            ("RUSTDESK_SERVER_PORT", "65533"),
            ("RUSTDESK_PUBLIC_KEY", "invalid"),
        ):
            env = {**base, field: value}
            with self.subTest(field=field), mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(ValueError):
                    rdgen_release.build_config()


if __name__ == "__main__":
    unittest.main()
