import base64
import importlib.util
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
            '\n'.join(
                [
                    'pub const RENDEZVOUS_SERVERS: &[&str] = &["rs-ny.rustdesk.com"];',
                    f'pub const RS_PUB_KEY: &str = "{inject.DEFAULT_PUBLIC_KEY}";',
                    'pub const RENDEZVOUS_PORT: i32 = 21116;',
                    'pub const RELAY_PORT: i32 = 21117;',
                    'pub const WS_RENDEZVOUS_PORT: i32 = 21118;',
                    'pub const WS_RELAY_PORT: i32 = 21119;',
                ]
            )
            + '\n',
            encoding="utf-8",
        )
        self.common_path.write_text(
            'fn get_api_server() { "https://admin.rustdesk.com".to_owned(); }\n',
            encoding="utf-8",
        )
        self.server = "rd.example.test"
        self.port = "24116"
        self.key = base64.b64encode(bytes(range(32))).decode("ascii")
        self.api = "https://api.example.test"

    def tearDown(self):
        self.temp.cleanup()

    def test_patches_and_verifies_all_server_values(self):
        inject.patch_source(self.root, self.server, self.port, self.key, self.api)
        config = self.config_path.read_text(encoding="utf-8")
        common = self.common_path.read_text(encoding="utf-8")
        self.assertIn(self.server, config)
        self.assertIn(self.key, config)
        for name, value in zip(inject.PORT_CONSTANTS, (24116, 24117, 24118, 24119)):
            self.assertIn(f"pub const {name}: i32 = {value};", config)
        self.assertIn(self.api, common)

    def test_fails_when_expected_source_placeholder_is_missing(self):
        self.config_path.write_text("pub const RENDEZVOUS_SERVERS: &[&str] = &[];\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            inject.patch_source(self.root, self.server, self.port, self.key, self.api)

    def test_rejects_port_block_that_would_overflow(self):
        with self.assertRaises(ValueError):
            inject.patch_source(self.root, self.server, "65533", self.key, self.api)

    def test_android_apk_verification_requires_embedded_server_values(self):
        apk_path = self.root / "client.apk"
        library = self.server.encode() + self.key.encode() + self.api.encode()
        with zipfile.ZipFile(apk_path, "w") as apk:
            apk.writestr("lib/armeabi-v7a/librustdesk.so", library)
        inject.verify_android_apk(apk_path, self.server, self.port, self.key, self.api)

        with zipfile.ZipFile(apk_path, "w") as apk:
            apk.writestr("lib/armeabi-v7a/librustdesk.so", self.key.encode() + self.api.encode())
        with self.assertRaises(ValueError):
            inject.verify_android_apk(apk_path, self.server, self.port, self.key, self.api)


if __name__ == "__main__":
    unittest.main()
