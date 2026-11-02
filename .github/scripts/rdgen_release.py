#!/usr/bin/env python3
"""Detect supported RustDesk versions and build signed RDGen client config JSON."""

from __future__ import annotations

import argparse
import ast
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

UPSTREAM_OWNER = "bryangerlach"
UPSTREAM_REPO = "rdgen"
UPSTREAM_BRANCH = "master"
RELEASE_PREFIX = "rdgen-rustdesk-"
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+\Z")
CONFIG_SECRET_ENV = "RUSTDESK_CLIENT_CONFIG_JSON_B64"
SIGNING_KEY_ENV = "RUSTDESK_CUSTOM_CLIENT_SIGNING_PRIVATE_KEY"
KNOWN_CONFIG_FIELDS = {
    "androidappid", "apiServer", "appname", "autoClose", "compname", "cycleMonitor",
    "defaultManual", "delayFix", "denyLan", "direction", "downloadLink", "enableAudio",
    "enableBlockingInput", "enableCamera", "enableClipboard", "enableDirectIP",
    "enableFileTransfer", "enableKeyboard", "enablePrinter", "enableRecording",
    "enableRemoteModi", "enableRemoteRestart", "enableTCP", "enableTerminal", "exename",
    "hidecm", "iconbase64", "installation", "key", "logobase64", "overrideManual",
    "passApproveMode", "permanentPassword", "permissionsDorO", "permissionsType",
    "platform", "privacybase64", "removeNewVersionNotif", "removeWallpaper", "serverIP",
    "serverPort", "settings", "theme", "themeDorO", "urlLink", "version", "xOffline",
}
UNSAFE_NAME_CHARS = re.compile(r"[&\\|\x27\"$`\r\n]")


def version_key(version: str) -> tuple[int, int, int]:
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"Not a stable RustDesk version: {version!r}")
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]


def extract_supported_versions(source: str) -> list[str]:
    """Read the version ChoiceField from RDGen forms.py using Python's AST."""
    tree = ast.parse(source, filename="rdgenerator/forms.py")
    choices_node: ast.AST | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "version" for target in node.targets):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr == "ChoiceField"):
            continue
        for keyword in call.keywords:
            if keyword.arg == "choices":
                choices_node = keyword.value
                break
        if choices_node is not None:
            break
    if not isinstance(choices_node, (ast.List, ast.Tuple)):
        raise ValueError("Could not find the RDGen version ChoiceField choices list")

    versions: set[str] = set()
    for choice in choices_node.elts:
        if not isinstance(choice, (ast.Tuple, ast.List)) or not choice.elts:
            continue
        value = choice.elts[0]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            if VERSION_PATTERN.fullmatch(value.value):
                versions.add(value.value)
    if not versions:
        raise ValueError("RDGen version choices contained no stable X.Y.Z release tags")
    return sorted(versions, key=version_key)


def published_versions(releases: list[dict[str, Any]]) -> list[str]:
    found: set[str] = set()
    for release in releases:
        tag = release.get("tag_name", "")
        if not isinstance(tag, str) or not tag.startswith(RELEASE_PREFIX):
            continue
        version = tag[len(RELEASE_PREFIX):]
        if not VERSION_PATTERN.fullmatch(version):
            continue
        assets = release.get("assets", [])
        names = [
            asset.get("name", "").lower()
            for asset in assets
            if isinstance(asset, dict) and isinstance(asset.get("name", ""), str)
        ] if isinstance(assets, list) else []
        # A partial release must not advance the watermark; the next poll can
        # rebuild and overwrite it until both required platform files exist.
        if any(name.endswith(".exe") for name in names) and any(name.endswith(".apk") for name in names):
            found.add(version)
    return sorted(found, key=version_key)


def select_build_version(
    supported: list[str], published: list[str], baseline: str = ""
) -> str | None:
    """Return the newest supported stable tag above the latest published build."""
    if not supported:
        raise ValueError("No supported versions were supplied")
    latest_supported = max(supported, key=version_key)
    watermarks = list(published)
    if baseline:
        version_key(baseline)
        watermarks.append(baseline)
    if not watermarks:
        return latest_supported
    latest_watermark = max(watermarks, key=version_key)
    return latest_supported if version_key(latest_supported) > version_key(latest_watermark) else None


def select_manual_build_version(
    requested: str, supported: list[str], releases: list[dict[str, Any]]
) -> str:
    """Allow an explicit workflow-dispatch rebuild only for supported, unpublished tags."""
    version_key(requested)
    if requested not in supported:
        raise ValueError("Requested manual version is not currently supported by RDGen")
    target_tag = f"{RELEASE_PREFIX}{requested}"
    if any(isinstance(release, dict) and release.get("tag_name") == target_tag for release in releases):
        raise ValueError("Requested manual release tag already exists; refusing to overwrite it")
    return requested


def _request_json(url: str, token: str = "") -> Any:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "rdgen-release-watcher"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def detect_remote(manual_version: str = "") -> tuple[str | None, str]:
    token = os.environ.get("GITHUB_TOKEN", "")
    forms_url = f"https://raw.githubusercontent.com/{UPSTREAM_OWNER}/{UPSTREAM_REPO}/refs/heads/{UPSTREAM_BRANCH}/rdgenerator/forms.py"
    source_request = urllib.request.Request(forms_url, headers={"User-Agent": "rdgen-release-watcher"})
    with urllib.request.urlopen(source_request, timeout=30) as response:
        source = response.read().decode("utf-8")

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not repository or "/" not in repository:
        raise ValueError("GITHUB_REPOSITORY is required to detect existing Releases")
    releases_url = f"https://api.github.com/repos/{repository}/releases?per_page=100"
    releases = _request_json(releases_url, token)
    if not isinstance(releases, list):
        raise ValueError("GitHub Releases API returned an unexpected response")

    commit_url = f"https://api.github.com/repos/{UPSTREAM_OWNER}/{UPSTREAM_REPO}/commits/{UPSTREAM_BRANCH}"
    commit = _request_json(commit_url, token)
    upstream_sha = commit.get("sha", "") if isinstance(commit, dict) else ""
    if not isinstance(upstream_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", upstream_sha):
        upstream_sha = "unknown"

    supported = extract_supported_versions(source)
    if manual_version:
        version = select_manual_build_version(manual_version, supported, releases)
    else:
        baseline = os.environ.get("RDGEN_BASELINE_VERSION", "")
        version = select_build_version(supported, published_versions(releases), baseline)
    return version, upstream_sha


def _load_client_config() -> dict[str, Any]:
    encoded = os.environ.get(CONFIG_SECRET_ENV, "")
    if not encoded:
        raise ValueError(f"Required environment secret {CONFIG_SECRET_ENV} is missing")
    try:
        raw = base64.b64decode(encoded, validate=True)
        config = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Saved client configuration is not valid base64-encoded UTF-8 JSON") from exc
    if not isinstance(config, dict):
        raise ValueError("Saved client configuration must be a JSON object")
    unknown = sorted(set(config) - KNOWN_CONFIG_FIELDS)
    if unknown:
        raise ValueError("Saved client configuration contains unsupported field(s): " + ", ".join(unknown))
    return config


def _string(config: dict[str, Any], field: str, default: str = "", *, strip: bool = True) -> str:
    value = config.get(field, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"Saved client configuration field {field} must be text")
    return value.strip() if strip else value


def _checkbox(config: dict[str, Any], field: str, default: bool = False) -> bool:
    value = config.get(field, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"on", "true", "1", "yes"}:
            return True
        if normalized in {"", "off", "false", "0", "no"}:
            return False
    raise ValueError(f"Saved client configuration field {field} is not a valid checkbox value")


def _as_yn(value: bool) -> str:
    return "Y" if value else "N"


def _parse_manual_settings(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key:
                result[key] = value
    return result


def _client_custom_settings(config: dict[str, Any], server: str, api_server: str) -> bytes:
    """Translate saved RDGen form fields into RustDesk advanced custom-client settings."""
    custom: dict[str, Any] = {}
    direction = _string(config, "direction", "both")
    installation = _string(config, "installation", "installationY")
    settings_enabled = _string(config, "settings", "settingsY")
    display_name = _string(config, "appname") or "rustdesk"
    app_name = re.sub(r"[:|\\/]", "", display_name).replace(" ", "")
    if not app_name.isascii():
        app_name = "rustdesk"
    if app_name.lower() != "rustdesk":
        custom["app-name"] = display_name
    if direction != "both":
        if direction not in {"incoming", "outgoing"}:
            raise ValueError("Saved client configuration has an unsupported connection direction")
        custom["conn-type"] = direction
    if installation == "installationN":
        custom["disable-installation"] = "Y"
    elif installation != "installationY":
        raise ValueError("Saved client configuration has an unsupported installation setting")
    if settings_enabled == "settingsN":
        custom["disable-settings"] = "Y"
    elif settings_enabled != "settingsY":
        raise ValueError("Saved client configuration has an unsupported settings setting")

    password = _string(config, "permanentPassword", strip=False)
    if password:
        custom["password"] = password

    theme = _string(config, "theme", "system")
    theme_mode = _string(config, "themeDorO", "default")
    if theme not in {"light", "dark", "system"} or theme_mode not in {"default", "override"}:
        raise ValueError("Saved client configuration has an unsupported theme setting")

    permissions_mode = _string(config, "permissionsDorO", "default")
    if permissions_mode not in {"default", "override"}:
        raise ValueError("Saved client configuration has an unsupported permissions mode")
    custom["default-settings"] = {}
    custom["override-settings"] = {}
    target_name = "default-settings" if permissions_mode == "default" else "override-settings"
    target: dict[str, str] = custom[target_name]
    if theme != "system":
        theme_target_name = "default-settings" if theme_mode == "default" else "override-settings"
        custom[theme_target_name]["theme"] = theme

    custom["enable-lan-discovery"] = _as_yn(not _checkbox(config, "denyLan"))
    custom["allow-auto-disconnect"] = _as_yn(_checkbox(config, "autoClose"))
    permissions_type = _string(config, "permissionsType", "custom")
    if permissions_type not in {"custom", "full", "view"}:
        raise ValueError("Saved client configuration has an unsupported access mode")
    target["access-mode"] = permissions_type
    permission_fields = (
        ("enableKeyboard", "enable-keyboard", True),
        ("enableClipboard", "enable-clipboard", True),
        ("enableFileTransfer", "enable-file-transfer", True),
        ("enableAudio", "enable-audio", True),
        ("enableTCP", "enable-tunnel", True),
        ("enableRemoteRestart", "enable-remote-restart", True),
        ("enableRecording", "enable-record-session", True),
        ("enableBlockingInput", "enable-block-input", True),
        ("enableRemoteModi", "allow-remote-config-modification", False),
        ("enableDirectIP", "direct-server", False),
        ("hidecm", "allow-hide-cm", False),
        ("removeWallpaper", "allow-remove-wallpaper", True),
        ("enablePrinter", "enable-remote-printer", True),
        ("enableCamera", "enable-camera", True),
        ("enableTerminal", "enable-terminal", True),
    )
    for field, setting, default in permission_fields:
        target[setting] = _as_yn(_checkbox(config, field, default))
    target["verification-method"] = (
        "use-permanent-password" if _checkbox(config, "hidecm") else "use-both-passwords"
    )
    approve_mode = _string(config, "passApproveMode", "password-click")
    if approve_mode not in {"password", "click", "password-click"}:
        raise ValueError("Saved client configuration has an unsupported approval mode")
    target["approve-mode"] = approve_mode

    if direction == "incoming" and target_name == "override-settings":
        target["custom-rendezvous-server"] = server
        target["api-server"] = api_server
    custom["default-settings"].update(_parse_manual_settings(_string(config, "defaultManual", strip=False)))
    custom["override-settings"].update(_parse_manual_settings(_string(config, "overrideManual", strip=False)))
    return json.dumps(custom, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _openssl(args: list[str]) -> bytes:
    try:
        result = subprocess.run(args, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise ValueError("OpenSSL is required to sign the custom client configuration") from exc
    if result.returncode != 0:
        raise ValueError("OpenSSL could not create or verify the custom configuration signature")
    return result.stdout


def _sign_custom_payload(payload: bytes, private_key_pem: str) -> tuple[str, str]:
    """Return base64(signature || payload) and the raw Ed25519 public key in base64."""
    if "-----BEGIN PRIVATE KEY-----" not in private_key_pem or "-----END PRIVATE KEY-----" not in private_key_pem:
        raise ValueError(f"{SIGNING_KEY_ENV} must contain an Ed25519 PKCS#8 PEM private key")
    with tempfile.TemporaryDirectory(prefix="rdgen-custom-sign-") as directory:
        root = Path(directory)
        private_path = root / "private.pem"
        public_der_path = root / "public.der"
        public_pem_path = root / "public.pem"
        payload_path = root / "payload.bin"
        signature_path = root / "signature.bin"
        private_path.write_text(private_key_pem, encoding="ascii")
        private_path.chmod(0o600)
        payload_path.write_bytes(payload)
        payload_path.chmod(0o600)

        public_der = _openssl(["openssl", "pkey", "-in", str(private_path), "-pubout", "-outform", "DER"])
        # RFC 8410 SubjectPublicKeyInfo for Ed25519: a 12-byte prefix and 32 raw key bytes.
        if len(public_der) != 44 or public_der[:12] != bytes.fromhex("302a300506032b6570032100"):
            raise ValueError("Signing key is not a valid Ed25519 PKCS#8 private key")
        public_raw = public_der[-32:]
        public_der_path.write_bytes(public_der)
        _openssl(["openssl", "pkey", "-pubin", "-inform", "DER", "-in", str(public_der_path), "-out", str(public_pem_path)])

        signature = _openssl([
            "openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(private_path),
            "-in", str(payload_path), "-out", str(signature_path),
        ])
        # pkeyutl writes its result to the named file; stdout is normally empty.
        if not signature and signature_path.is_file():
            signature = signature_path.read_bytes()
        if len(signature) != 64:
            raise ValueError("Ed25519 signing did not produce a 64-byte signature")
        _openssl([
            "openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-inkey", str(public_pem_path),
            "-in", str(payload_path), "-sigfile", str(signature_path),
        ])
        signed = base64.b64encode(signature + payload).decode("ascii")
        public = base64.b64encode(public_raw).decode("ascii")
        return signed, public


def build_config() -> dict[str, str]:
    source = _load_client_config()
    missing = [name for name in ("serverIP", "key", "apiServer") if not _string(source, name)]
    if missing:
        raise ValueError("Saved client configuration is missing required field(s): " + ", ".join(missing))
    server = _string(source, "serverIP")
    port = _string(source, "serverPort") or "21116"
    api_server = _string(source, "apiServer")
    public_key = _string(source, "key")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", server):
        raise ValueError("Saved client configuration server host is invalid")
    if not port.isdigit() or not 1 <= int(port) <= 65532:
        raise ValueError("Saved client configuration server port must allow three derived ports")
    parsed_api = urlsplit(api_server)
    if parsed_api.scheme != "https" or not parsed_api.netloc or any(ch in api_server for ch in "\r\n|\""):
        raise ValueError("Saved client configuration API server must be a valid HTTPS URL")
    try:
        decoded_key = base64.b64decode(public_key, validate=True)
    except Exception as exc:
        raise ValueError("Saved client configuration server key is not valid base64") from exc
    if len(decoded_key) != 32:
        raise ValueError("Saved client configuration server key must decode to a 32-byte Ed25519 public key")

    for field in ("iconbase64", "logobase64", "privacybase64"):
        if _string(source, field):
            raise ValueError(f"Saved client configuration {field} is non-empty, but this unattended release path cannot attach image files")

    display_name = _string(source, "appname") or "rustdesk"
    company = _string(source, "compname") or "Purslane Ltd"
    filename = _string(source, "exename") or "rustdesk"
    for field, value in (("appname", display_name), ("compname", company)):
        if UNSAFE_NAME_CHARS.search(value):
            raise ValueError(f"Saved client configuration field {field} contains characters unsafe for build scripts")
    appname = re.sub(r"[:|\\/]", "", display_name).replace(" ", "")
    if not appname.isascii():
        appname = "rustdesk"
    if not filename.isascii():
        filename = "rustdesk"
    else:
        filename = re.sub(r"[^\w\s-]", "_", filename).strip().replace(" ", "_") or "rustdesk"
    if not company:
        company = "Purslane Ltd"
    android_app_id = _string(source, "androidappid") or "com.carriez.flutter_hbb"
    if not re.fullmatch(r"[A-Za-z0-9_.]+", android_app_id):
        raise ValueError("Saved client configuration Android application ID is invalid")

    custom_payload = _client_custom_settings(source, server, api_server)
    signing_key = os.environ.get(SIGNING_KEY_ENV, "")
    if not signing_key:
        raise ValueError(f"Required environment secret {SIGNING_KEY_ENV} is missing")
    signed_custom, custom_public_key = _sign_custom_payload(custom_payload, signing_key)

    values = {
        "server": server,
        "serverPort": port,
        "key": public_key,
        "apiServer": api_server,
        "custom": signed_custom,
        "customClientPublicKey": custom_public_key,
        "uuid": "00000000-0000-4000-8000-000000000000",
        "iconlink_url": "false",
        "iconlink_uuid": "false",
        "iconlink_file": "false",
        "logolink_url": "false",
        "logolink_uuid": "false",
        "logolink_file": "false",
        "privacylink_url": "false",
        "privacylink_uuid": "false",
        "privacylink_file": "false",
        "appname": appname,
        "genurl": "",
        "urlLink": _string(source, "urlLink") or "https://rustdesk.com",
        "downloadLink": _string(source, "downloadLink") or "https://rustdesk.com/download",
        "delayFix": str(_checkbox(source, "delayFix", True)).lower(),
        "rdgen": "false",
        "xOffline": str(_checkbox(source, "xOffline")).lower(),
        "removeNewVersionNotif": str(_checkbox(source, "removeNewVersionNotif")).lower(),
        "update_public_key": "",
        "compname": company,
        "androidappid": android_app_id,
        "filename": filename,
        "cycleMonitor": str(_checkbox(source, "cycleMonitor")).lower(),
        "token": "",
    }
    return values


def write_github_output(values: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        raise ValueError("GITHUB_OUTPUT is not set")
    with open(path, "a", encoding="utf-8") as output:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ValueError(f"Output {key} contains a newline")
            output.write(f"{key}={value}\n")


def command_detect(manual_version: str = "") -> None:
    version, upstream_sha = detect_remote(manual_version)
    write_github_output({
        "build_needed": "true" if version else "false",
        "version": version or "",
        "upstream_sha": upstream_sha,
    })
    if manual_version and version:
        print(f"Manual rebuild requested for RDGen-supported tag: {version} (RDGen commit {upstream_sha})")
    elif version:
        print(f"RDGen supports a new RustDesk build tag: {version} (RDGen commit {upstream_sha})")
    else:
        print("No newer stable RustDesk version is currently supported by RDGen upstream.")


def command_config() -> None:
    config = build_config()
    encoded = base64.b64encode(json.dumps(config, separators=(",", ":")).encode("utf-8")).decode("ascii")
    write_github_output({"secrets_json_b64": encoded})
    print("Validated saved client settings and emitted a signed build configuration.")


def command_config_file(path: str) -> None:
    """Local test helper that writes the build config JSON to a file."""
    config = build_config()
    Path(path).write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"Wrote validated configuration to {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    detect = subparsers.add_parser("detect", help="Poll upstream support, or validate a manually requested version")
    detect.add_argument("--manual-version", default="", help="Manual rebuild version; use only for explicit workflow_dispatch runs")
    subparsers.add_parser("config", help="Validate saved config and emit a signed build JSON output")
    config_file = subparsers.add_parser("config-file", help="Write validated configuration locally for tests")
    config_file.add_argument("path")
    args = parser.parse_args()
    try:
        if args.command == "detect":
            command_detect(args.manual_version)
        elif args.command == "config":
            command_config()
        else:
            command_config_file(args.path)
    except (ValueError, SyntaxError, urllib.error.URLError, json.JSONDecodeError) as error:
        print(f"RDGen release preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
