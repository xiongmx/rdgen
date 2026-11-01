#!/usr/bin/env python3
"""Detect newly supported RustDesk tags in upstream RDGen and build config JSON."""

from __future__ import annotations

import argparse
import ast
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

UPSTREAM_OWNER = "bryangerlach"
UPSTREAM_REPO = "rdgen"
UPSTREAM_BRANCH = "master"
RELEASE_PREFIX = "rdgen-rustdesk-"
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+\Z")


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


def build_config() -> dict[str, str]:
    server = os.environ.get("RUSTDESK_SERVER", "")
    port = os.environ.get("RUSTDESK_SERVER_PORT", "")
    api_server = os.environ.get("RUSTDESK_API_SERVER", "")
    public_key = os.environ.get("RUSTDESK_PUBLIC_KEY", "")
    if not server or not re.fullmatch(r"[A-Za-z0-9.-]+", server):
        raise ValueError("RUSTDESK_SERVER must be a valid host name")
    if not port.isdigit() or not 1 <= int(port) <= 65532:
        raise ValueError("RUSTDESK_SERVER_PORT must allow three derived ports (1-65532)")
    if not api_server.startswith("https://") or any(ch in api_server for ch in "\r\n|\""):
        raise ValueError("RUSTDESK_API_SERVER must be an HTTPS URL")
    if not re.fullmatch(r"[A-Za-z0-9+/]{43}=", public_key):
        raise ValueError("RUSTDESK_PUBLIC_KEY must be a base64 Ed25519 public key")

    # RustDesk's read_custom_client() requires an Ed25519-signed payload.
    # This automatic path has no signing key, so do not synthesize unsigned JSON.
    custom = ""
    values = {
        "server": server,
        "serverPort": port,
        "key": public_key,
        "apiServer": api_server,
        "custom": custom,
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
        "appname": "rustdesk",
        "genurl": "",
        "urlLink": "https://rustdesk.com",
        "downloadLink": "https://rustdesk.com/download",
        "delayFix": "true",
        "rdgen": "false",
        "xOffline": "false",
        "removeNewVersionNotif": "false",
        "update_public_key": "",
        "compname": "Purslane Ltd",
        "androidappid": "com.carriez.flutter_hbb",
        "filename": "rustdesk",
        "runasadmin": "false",
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
    print("Validated fixed build configuration and emitted its encoded JSON output.")


def command_config_file(path: str) -> None:
    """Local test helper that writes the public config JSON to a file."""
    config = build_config()
    Path(path).write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"Wrote validated configuration to {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    detect = subparsers.add_parser("detect", help="Poll upstream support, or validate a manually requested version")
    detect.add_argument("--manual-version", default="", help="Manual rebuild version; use only for explicit workflow_dispatch runs")
    subparsers.add_parser("config", help="Validate the GitHub Environment and emit config JSON output")
    config_file = subparsers.add_parser("config-file", help="Write config JSON locally for tests")
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
