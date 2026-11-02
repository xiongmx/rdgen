import base64
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
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

    def test_manual_rebuild_can_select_supported_baseline_when_no_release_exists(self):
        self.assertEqual(
            rdgen_release.select_manual_build_version("1.5.0", ["1.5.0"], []),
            "1.5.0",
        )

    def test_manual_rebuild_rejects_unsupported_or_existing_release(self):
        with self.assertRaises(ValueError):
            rdgen_release.select_manual_build_version("1.5.1", ["1.5.0"], [])
        with self.assertRaises(ValueError):
            rdgen_release.select_manual_build_version(
                "1.5.0", ["1.5.0"], [{"tag_name": "rdgen-rustdesk-1.5.0", "assets": []}]
            )

    def test_ignores_non_rdgen_and_non_stable_release_tags(self):
        releases = [
            {"tag_name": "v9.9.9"},
            {"tag_name": "rdgen-rustdesk-1.5.0-beta.1"},
            {"tag_name": "rdgen-rustdesk-1.5.0", "assets": [{"name": "rustdesk.exe"}]},
            {
                "tag_name": "rdgen-rustdesk-1.4.9",
                "assets": [{"name": "rustdesk.exe"}, {"name": "rustdesk-armv7.apk"}],
            },
        ]
        self.assertEqual(rdgen_release.published_versions(releases), ["1.4.9"])

    def test_fails_closed_when_version_choice_is_missing(self):
        with self.assertRaises(ValueError):
            rdgen_release.extract_supported_versions("class GenerateForm: pass\n")


class ConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.private_key_path = Path(cls.temp.name) / "test-signing-key.pem"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(cls.private_key_path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        cls.private_key = cls.private_key_path.read_text(encoding="ascii")
        cls.server_key = base64.b64encode(bytes(range(32))).decode("ascii")
        cls.form_config = {
            "platform": "windows",
            "version": "1.4.2",
            "serverIP": "rd.example.test",
            "serverPort": "",
            "key": cls.server_key,
            "apiServer": "https://api.example.test",
            "appname": "rustdesk",
            "exename": "rustdesk",
            "compname": "Example Co",
            "androidappid": "com.example.rustdesk",
            "urlLink": "https://example.test",
            "downloadLink": "https://example.test/download",
            "direction": "both",
            "installation": "installationY",
            "settings": "settingsY",
            "permissionsDorO": "default",
            "permissionsType": "full",
            "theme": "system",
            "themeDorO": "default",
            "passApproveMode": "password-click",
            "permanentPassword": " password with spaces ",
            "defaultManual": "",
            "overrideManual": "",
            "autoClose": False,
            "cycleMonitor": False,
            "denyLan": False,
            "delayFix": "on",
            "xOffline": "on",
            "removeNewVersionNotif": "on",
            "enableAudio": "on",
            "enableBlockingInput": "on",
            "enableCamera": "on",
            "enableClipboard": "on",
            "enableDirectIP": "on",
            "enableFileTransfer": "on",
            "enableKeyboard": "on",
            "enablePrinter": "on",
            "enableRecording": "on",
            "enableRemoteModi": "off",
            "enableRemoteRestart": "on",
            "enableTCP": "on",
            "enableTerminal": "on",
            "hidecm": "on",
            "removeWallpaper": "on",
            "iconbase64": "",
            "logobase64": "",
            "privacybase64": "",
        }

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def build_with(self, config):
        env = {
            rdgen_release.CONFIG_SECRET_ENV: base64.b64encode(
                json.dumps(config).encode("utf-8")
            ).decode("ascii"),
            rdgen_release.SIGNING_KEY_ENV: self.private_key,
        }
        with mock.patch.dict(os.environ, env, clear=True):
            return rdgen_release.build_config()

    def test_emits_full_saved_settings_as_valid_signed_payload(self):
        config = self.build_with(self.form_config)
        self.assertEqual(config["server"], "rd.example.test")
        self.assertEqual(config["serverPort"], "21116")
        self.assertEqual(config["apiServer"], "https://api.example.test")
        self.assertEqual(config["key"], self.server_key)
        self.assertEqual(config["appname"], "rustdesk")
        self.assertEqual(config["compname"], "Example Co")
        self.assertEqual(config["androidappid"], "com.example.rustdesk")
        self.assertEqual(config["delayFix"], "true")
        self.assertEqual(config["xOffline"], "true")
        self.assertEqual(config["removeNewVersionNotif"], "true")
        self.assertEqual(config["cycleMonitor"], "false")
        self.assertEqual(config["rdgen"], "false")
        self.assertEqual(config["genurl"], "")
        self.assertNotIn("version", config)

        signed = base64.b64decode(config["custom"], validate=True)
        signature, payload = signed[:64], signed[64:]
        custom = json.loads(payload.decode("utf-8"))
        self.assertEqual(custom["password"], " password with spaces ")
        self.assertEqual(custom["default-settings"]["access-mode"], "full")
        self.assertEqual(custom["default-settings"]["enable-keyboard"], "Y")
        self.assertEqual(custom["default-settings"]["enable-file-transfer"], "Y")
        self.assertEqual(custom["default-settings"]["direct-server"], "Y")
        self.assertEqual(custom["default-settings"]["verification-method"], "use-permanent-password")
        self.assertEqual(custom["override-settings"], {})
        self.assertEqual(custom["enable-lan-discovery"], "Y")
        self.assertEqual(custom["allow-auto-disconnect"], "N")

        public_der = subprocess.run(
            ["openssl", "pkey", "-in", str(self.private_key_path), "-pubout", "-outform", "DER"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
        expected_public = base64.b64encode(public_der[-32:]).decode("ascii")
        self.assertEqual(config["customClientPublicKey"], expected_public)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload_path = root / "payload"
            signature_path = root / "signature"
            public_path = root / "public.pem"
            payload_path.write_bytes(payload)
            signature_path.write_bytes(signature)
            subprocess.run(
                ["openssl", "pkey", "-in", str(self.private_key_path), "-pubout", "-out", str(public_path)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                ["openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-inkey", str(public_path), "-in", str(payload_path), "-sigfile", str(signature_path)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        self.assertNotIn("BEGIN PRIVATE KEY", json.dumps(config))

    def test_honors_override_mode_when_the_saved_file_requests_it(self):
        source = {**self.form_config, "permissionsDorO": "override"}
        custom = json.loads(base64.b64decode(self.build_with(source)["custom"])[64:].decode())
        self.assertIn("access-mode", custom["override-settings"])
        self.assertNotIn("access-mode", custom["default-settings"])

    def test_rejects_missing_secrets_unknown_fields_and_unhandled_images(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            rdgen_release.build_config()
        with self.assertRaises(ValueError):
            self.build_with({**self.form_config, "unexpectedNewField": "x"})
        with self.assertRaises(ValueError):
            self.build_with({**self.form_config, "iconbase64": "not-empty"})

    def test_rejects_invalid_saved_server_config(self):
        for patch in (
            {"serverPort": "bad"},
            {"serverPort": "65533"},
            {"serverIP": "invalid|host"},
            {"key": "invalid"},
            {"apiServer": "http://api.example.test"},
        ):
            with self.subTest(patch=sorted(patch)):
                with self.assertRaises(ValueError):
                    self.build_with({**self.form_config, **patch})


if __name__ == "__main__":
    unittest.main()
