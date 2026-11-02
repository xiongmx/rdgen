import base64
import importlib.util
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "inject_rustdesk_config.py"
SPEC = importlib.util.spec_from_file_location("inject_rustdesk_config", SCRIPT)
assert SPEC and SPEC.loader
inject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inject)


class ConfigInjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "libs/hbb_common/src/config.rs"
        self.common_path = self.root / "src/common.rs"
        self.config_path.parent.mkdir(parents=True)
        self.common_path.parent.mkdir(parents=True)
        self.config_path.write_text(
            "\n".join(
                [
                    'pub const RENDEZVOUS_SERVERS: &[&str] = &["rs-ny.rustdesk.com"];',
                    f'pub const RS_PUB_KEY: &str = "{inject.DEFAULT_PUBLIC_KEY}";',
                    "pub const RENDEZVOUS_PORT: i32 = 21116;",
                    "pub const RELAY_PORT: i32 = 21117;",
                    "pub const WS_RENDEZVOUS_PORT: i32 = 21118;",
                    "pub const WS_RELAY_PORT: i32 = 21119;",
                    "pub static ref DEFAULT_SETTINGS: RwLock<HashMap<String, String>> = Default::default();",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.common_path.write_text(
            "\n".join(
                [
                    f'const KEY: &str = "{inject.DEFAULT_CUSTOM_CLIENT_PUBLIC_KEY}";',
                    'fn load_debug() { std::fs::read_to_string("./custom.txt"); }',
                    'fn load_release(path: PathBuf) { path.join("custom.txt"); }',
                    'fn check_signature() { sign::verify(&data, &pk); }',
                    'fn get_api_server() { "https://admin.rustdesk.com".to_owned(); }',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.server = "rd.example.test"
        self.port = "24116"
        self.key = base64.b64encode(bytes(range(32))).decode("ascii")
        self.api = "https://api.example.test"
        self.custom_private = self.root / "custom-test-key.pem"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(self.custom_private)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.custom_public_der = subprocess.run(
            ["openssl", "pkey", "-in", str(self.custom_private), "-pubout", "-outform", "DER"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
        self.custom_public = base64.b64encode(self.custom_public_der[-32:]).decode("ascii")
        self.custom_config = self._signed_custom_payload()

    def _signed_custom_payload(self):
        payload = json.dumps({"default-settings": {"access-mode": "full"}}, separators=(",", ":")).encode()
        payload_path = self.root / "test-payload.json"
        signature_path = self.root / "test-payload.sig"
        payload_path.write_bytes(payload)
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(self.custom_private), "-in", str(payload_path), "-out", str(signature_path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return base64.b64encode(signature_path.read_bytes() + payload).decode("ascii")

    def tearDown(self):
        self.temp.cleanup()

    def test_patches_server_defaults_and_custom_public_key_without_disabling_verification(self):
        inject.patch_source(self.root, self.server, self.port, self.key, self.api, self.custom_public)
        config = self.config_path.read_text(encoding="utf-8")
        common = self.common_path.read_text(encoding="utf-8")
        self.assertIn(self.server, config)
        self.assertIn(self.key, config)
        for name, value in zip(inject.PORT_CONSTANTS, (24116, 24117, 24118, 24119)):
            self.assertIn(f"pub const {name}: i32 = {value};", config)
        for name, value in (
            ("custom-rendezvous-server", "rd.example.test"),
            ("relay-server", ""),
            ("api-server", self.api),
            ("key", self.key),
        ):
            self.assertIn(f'settings.insert("{name}".to_owned(), "{value}".to_owned());', config)
        self.assertIn(self.custom_public, common)
        self.assertIn('read_to_string("./custom_.txt")', common)
        self.assertIn('path.join("custom_.txt")', common)
        self.assertIn("sign::verify(&data, &pk)", common)
        self.assertNotIn(self.inject_original_public_key(), common)

    def inject_original_public_key(self):
        return f'const KEY: &str = "{inject.DEFAULT_CUSTOM_CLIENT_PUBLIC_KEY}";'

    def test_fails_when_expected_source_placeholder_is_missing(self):
        self.config_path.write_text('pub const RENDEZVOUS_SERVERS: &[&str] = &[];\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            inject.patch_source(self.root, self.server, self.port, self.key, self.api, self.custom_public)

    def test_rejects_port_block_that_would_overflow(self):
        with self.assertRaises(ValueError):
            inject.patch_source(self.root, self.server, "65533", self.key, self.api, self.custom_public)

    def _write_apk(self, apk_path: Path, library: bytes, custom_asset: bytes):
        with zipfile.ZipFile(apk_path, "w") as apk:
            apk.writestr("lib/armeabi-v7a/librustdesk.so", library)
            apk.writestr("assets/flutter_assets/assets/custom_.txt", custom_asset)

    def test_android_apk_verification_requires_matching_signed_custom_settings(self):
        apk_path = self.root / "client.apk"
        library = self.server.encode() + self.key.encode() + self.api.encode()
        self._write_apk(apk_path, library, self.custom_config.encode("ascii"))
        inject.verify_android_apk(
            apk_path, self.server, self.port, self.key, self.api, self.custom_config, self.custom_public
        )

        self._write_apk(apk_path, self.key.encode() + self.api.encode(), self.custom_config.encode("ascii"))
        with self.assertRaises(ValueError):
            inject.verify_android_apk(
                apk_path, self.server, self.port, self.key, self.api, self.custom_config, self.custom_public
            )

        self._write_apk(
            apk_path,
            library + f"{self.server}:{int(self.port) + 1}".encode(),
            self.custom_config.encode("ascii"),
        )
        with self.assertRaises(ValueError):
            inject.verify_android_apk(
                apk_path, self.server, self.port, self.key, self.api, self.custom_config, self.custom_public
            )

        tampered = self.custom_config[:-4] + "AAAA"
        self._write_apk(apk_path, library, tampered.encode("ascii"))
        with self.assertRaises(ValueError):
            inject.verify_android_apk(
                apk_path, self.server, self.port, self.key, self.api, tampered, self.custom_public
            )


if __name__ == "__main__":
    unittest.main()
