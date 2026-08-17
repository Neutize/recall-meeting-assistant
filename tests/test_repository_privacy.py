"""Repository-level privacy and credential-ignore regressions."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})"
)
EXAMPLE_DOMAINS = frozenset({"example.com", "example.net", "example.org"})
PRIVATE_DEPLOYMENT_MARKERS = ("/opt" + "/data", "HERMES" + "_HOME")


def _tracked_text_files() -> list[tuple[Path, str]]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    files: list[tuple[Path, str]] = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = Path(raw_path.decode("utf-8"))
        content = (REPOSITORY_ROOT / relative).read_bytes()
        if b"\0" in content:
            continue
        files.append((relative, content.decode("utf-8", errors="replace")))
    return files


def _is_reserved_email_domain(domain: str) -> bool:
    lowered = domain.lower()
    return (
        lowered in EXAMPLE_DOMAINS
        or any(lowered.endswith(f".{item}") for item in EXAMPLE_DOMAINS)
        or lowered == "invalid"
        or lowered.endswith(".invalid")
    )


def test_tracked_tree_contains_only_reserved_email_domains():
    violations: list[str] = []
    for path, text in _tracked_text_files():
        for line_number, line in enumerate(text.splitlines(), start=1):
            for match in EMAIL_PATTERN.finditer(line):
                if not _is_reserved_email_domain(match.group(1)):
                    violations.append(f"{path}:{line_number}:{match.group(0)}")
    assert violations == []


def test_tracked_tree_contains_no_private_deployment_paths():
    violations: list[str] = []
    for path, text in _tracked_text_files():
        for marker in PRIVATE_DEPLOYMENT_MARKERS:
            if marker in text:
                violations.append(f"{path}:{marker}")
    assert violations == []


def test_google_oauth_credential_filenames_are_ignored():
    credential_paths = [
        "google_token.json",
        "google_token-personal.json",
        "token.json",
        "credentials.json",
        "credentials-workspace.json",
        "client_secret.json",
        "client_secret-desktop.json",
        "oauth-client.p12",
        "oauth-client.pfx",
        "oauth-client.jks",
    ]
    missed: list[str] = []
    for path in credential_paths:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", path],
            cwd=REPOSITORY_ROOT,
            check=False,
        )
        if result.returncode != 0:
            missed.append(path)
    assert missed == []
