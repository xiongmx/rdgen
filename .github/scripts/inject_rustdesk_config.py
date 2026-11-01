#!/usr/bin/env python3
"""Patch RustDesk server defaults and verify the built Android APK contains them."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_RENDEZVOUS_HOST = "rs-ny.rustdesk.com"
DEFAULT_PUBLIC_KEY = "OeVuKk5nlHiXp+APNn0Y3pC1Iwpwn44JGqrQCsWqmBw="
DEFAULT_API_SERVER = "https://admin.rustdesk.com"
PORT_CONSTANTS = (
    "RENDEZVOUS_PORT",
    "RELAY_PORT",
    "WS_RENDEZVOUS_PORT",
    "WS_RELAY_PORT",
)


def validate_config(server: str, port_text: str, public_key: str, api_server: str) -> int:
    if not re.fullmatch(r"[A-Za-z0-9.-]+", server):
        raise ValueError("SERVER_DOMAIN is missing or invalid")
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65532:
        raise ValueError("SERVER_PORT_INPUT must be between 1 and 65532")
    try:
        key_bytes = base64.b64decode(public_key, validate=True)
    except Exception as exc:
        raise ValueError("SERVER_KEY is not valid base64") from exc
    if len(key_bytes) != 32:
        raise ValueError("SERVER_KEY must decode to a 32-byte Ed25519 public key")
    parsed_api = urlsplit(api_server)
    if (
        parsed_api.scheme != "https"
        or not parsed_api.netloc
        or any(ch in api_server for ch in "\r\n|\"")
    ):
        raise ValueError("API_SERVER must be a valid HTTPS URL")
    return int(port_text)


def _replace_one(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ValueError(f"Expected exactly one {label} placeholder; found {count}")
    return text.replace(old, new, 1)


def _replace_port(text: str, name: str, value: int) -> str:
    pattern = re.compile(rf"^pub const {re.escape(name)}: i32 = [0-9]+;$", re.MULTILINE)
    updated, count = pattern.subn(f"pub const {name}: i32 = {value};", text)
    if count != 1:
        raise ValueError(f"Expected exactly one {name} definition; found {count}")
    return updated


def _rust_string_literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _default_settings_initializer(server: str, public_key: str, api_server: str) -> str:
    settings = (
        ("custom-rendezvous-server", server),
        ("relay-server", ""),
        ("api-server", api_server),
        ("key", public_key),
    )
    lines = [
        "pub static ref DEFAULT_SETTINGS: RwLock<HashMap<String, String>> = {",
        "        let mut settings = HashMap::new();",
    ]
    for name, value in settings:
        lines.append(
            f"        settings.insert({_rust_string_literal(name)}.to_owned(), "
            f"{_rust_string_literal(value)}.to_owned());"
        )
    lines.extend(("        RwLock::new(settings)", "    };"))
    return "\n".join(lines)


def patch_source(root: Path, server: str, port_text: str, public_key: str, api_server: str) -> None:
    port = validate_config(server, port_text, public_key, api_server)
    config_path = root / "libs/hbb_common/src/config.rs"
    common_path = root / "src/common.rs"
    if not config_path.is_file() or not common_path.is_file():
        raise ValueError("RustDesk config.rs or common.rs source file is missing")

    config_text = config_path.read_text(encoding="utf-8")
    common_text = common_path.read_text(encoding="utf-8")

    config_text = _replace_one(
        config_text,
        DEFAULT_RENDEZVOUS_HOST,
        server,
        "rendezvous host",
    )
    config_text = _replace_one(
        config_text,
        DEFAULT_PUBLIC_KEY,
        public_key,
        "public key",
    )
    for offset, name in enumerate(PORT_CONSTANTS):
        config_text = _replace_port(config_text, name, port + offset)
    config_text = _replace_one(
        config_text,
        "pub static ref DEFAULT_SETTINGS: RwLock<HashMap<String, String>> = Default::default();",
        _default_settings_initializer(server, public_key, api_server),
        "DEFAULT_SETTINGS initializer",
    )

    api_count = common_text.count(DEFAULT_API_SERVER)
    if api_count < 1:
        raise ValueError("Default API server placeholder is missing from common.rs")
    common_text = common_text.replace(DEFAULT_API_SERVER, api_server)

    # Validate every replacement before writing either source file.
    if server not in config_text or public_key not in config_text:
        raise ValueError("Server host or public key did not survive source replacement")
    expected_ports = [port + offset for offset in range(len(PORT_CONSTANTS))]
    for name, value in zip(PORT_CONSTANTS, expected_ports):
        expected = f"pub const {name}: i32 = {value};"
        if expected not in config_text:
            raise ValueError(f"Verified {name} value is missing after replacement")
    expected_defaults = (
        ("custom-rendezvous-server", server),
        ("relay-server", ""),
        ("api-server", api_server),
        ("key", public_key),
    )
    for name, value in expected_defaults:
        expected = (
            f"settings.insert({_rust_string_literal(name)}.to_owned(), "
            f"{_rust_string_literal(value)}.to_owned());"
        )
        if expected not in config_text:
            raise ValueError(f"Verified default setting {name} is missing after replacement")
    if api_server not in common_text:
        raise ValueError("API server URL did not survive source replacement")

    config_path.write_text(config_text, encoding="utf-8")
    common_path.write_text(common_text, encoding="utf-8")
    print("Verified source replacements and defaults for ID, relay, API, key, and all four ports.")


def verify_android_apk(apk_path: Path, server: str, port_text: str, public_key: str, api_server: str) -> None:
    validate_config(server, port_text, public_key, api_server)
    if not apk_path.is_file():
        raise ValueError("Android APK does not exist")
    with zipfile.ZipFile(apk_path) as apk:
        names = apk.namelist()
        libraries = [name for name in names if name.endswith("/librustdesk.so")]
        if len(libraries) != 1:
            raise ValueError("APK must contain exactly one RustDesk native library")
        native = apk.read(libraries[0])
        expected = {
            "server host": server.encode("utf-8"),
            "public key": public_key.encode("ascii"),
            "API URL": api_server.encode("utf-8"),
        }
        missing = [label for label, value in expected.items() if value not in native]
        if missing:
            raise ValueError("APK native library is missing configured value(s): " + ", ".join(missing))
        explicit_ports = {
            "ID server option with explicit port": f"{server}:{port_text}".encode("utf-8"),
            "relay server option that should be blank": f"{server}:{int(port_text) + 1}".encode("utf-8"),
        }
        unexpected = [label for label, value in explicit_ports.items() if value in native]
        if unexpected:
            raise ValueError("APK contains unexpected server setting(s): " + ", ".join(unexpected))

        # Auto-release configuration has no RDGen signing key, so no custom-client
        # payload is generated. Reject a non-empty asset instead of shipping an
        # unsigned blob that RustDesk's read_custom_client() will refuse.
        assets = [name for name in names if name.endswith("/assets/custom_.txt")]
        if any(apk.read(name).strip() for name in assets):
            raise ValueError("APK contains an unexpected unsigned custom-client payload")
    print("Verified Android APK contains host-only ID defaults, blank relay, API, and key.")


def required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"Required build environment value {name} is missing")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    patch = subparsers.add_parser("patch-source")
    patch.add_argument("--root", type=Path, default=Path("."))
    verify = subparsers.add_parser("verify-apk")
    verify.add_argument("--apk", type=Path, required=True)
    args = parser.parse_args()
    try:
        values = (
            required_env("SERVER_DOMAIN"),
            required_env("SERVER_PORT_INPUT"),
            required_env("SERVER_KEY"),
            required_env("API_SERVER"),
        )
        if args.command == "patch-source":
            patch_source(args.root, *values)
        else:
            verify_android_apk(args.apk, *values)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Configuration injection failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
