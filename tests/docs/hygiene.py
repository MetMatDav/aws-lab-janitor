"""What must never be in a tracked file or in the history, and how to find it.

Run ``python -m tests.docs.hygiene --history`` to scan every version of every
file the repository has ever held.
"""

import hashlib
import ipaddress
import re
import subprocess
import sys
from pathlib import Path

# The owner's account id, real resource names, SSO profile and mailbox name,
# as sha256 of the lowercased token: the list forbids them without holding them.
FORBIDDEN_SHA256 = frozenset(
    {
        "d488c164deadcc9e7a6aa38a93a2c25dfead8355c845aa9c0dc5e38b8343f586",
        "a1fca76c3213af12e03cd70f2d6f7fc9abd0cce461c8eef8d273e82c2ed1626c",
        "0a3d306a5c2a5cc6da279807140a63334b45d8849ba462551f23ccd2bd049ebf",
        "3d2a7a720ff09f7cc56a0c79f57c0ac1aab10bb337e52fa2d824bef95383c0ec",
        "422165fce6c33d46df99f3a5d2f10aa5635233574c455ee444f31b1256a6a769",
        "3b342e71921a3ef79393abb77a2e800b169f17b4e9b5a12e31335f9ab0a18cc0",
        "1e124675512c560799223f9ff268e97e5a886ee37e5268cffc1cb82096b11d62",
    }
)
ALLOWED_ACCOUNT_IDS = frozenset({"123456789012", "111122223333", "000000000000"})
ALLOWED_EMAIL_DOMAINS = (
    "example.com",
    "example.net",
    "example.org",
    "anthropic.com",
    "sns.amazonaws.com",
    "users.noreply.github.com",
    "github.com",
    # The shape shown in the "must be an email address" error message.
    "domain.tld",
)
# Documentation ranges (RFC 5737) next to the private, loopback and link-local ones.
DOCUMENTATION_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "0.0.0.0/32")
)
BINARY_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip"})

_TOKEN = re.compile(r"[A-Za-z0-9._-]+")
_TWELVE_DIGITS = re.compile(r"(?<!\d)\d{12}(?!\d)")
_UUID_TAIL = re.compile(r"[0-9a-fA-F]{4}-\d{12}(?![0-9A-Za-z])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
_IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
_HOME = re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+")
_COMMIT_MARK = "==commit== "


def digest(token: str) -> str:
    """sha256 of the lowercased token, as hex."""
    return hashlib.sha256(token.lower().encode()).hexdigest()


def _account_ids(text: str) -> list[str]:
    uuid_tails = {match.end() for match in _UUID_TAIL.finditer(text)}
    return [
        match.group()
        for match in _TWELVE_DIGITS.finditer(text)
        if match.group() not in ALLOWED_ACCOUNT_IDS and match.end() not in uuid_tails
    ]


def _emails(text: str) -> list[str]:
    return [
        match.group()
        for match in _EMAIL.finditer(text)
        if not match.group(1).lower().endswith(ALLOWED_EMAIL_DOMAINS)
    ]


def _addresses(text: str) -> list[str]:
    found = []
    for match in _IPV4.finditer(text):
        try:
            address = ipaddress.ip_address(match.group())
        except ValueError:
            continue
        documented = any(address in network for network in DOCUMENTATION_NETWORKS)
        if not (documented or address.is_private or address.is_loopback or address.is_link_local):
            found.append(match.group())
    return found


def _hashed(text: str, forbidden: frozenset[str]) -> list[str]:
    found = []
    for token in _TOKEN.findall(text):
        candidates = {token, token.strip("._-")}
        for candidate in sorted(candidates):
            if digest(candidate) in forbidden:
                found.append(f"sha256:{digest(candidate)[:12]}")
                break
    return found


def find_forbidden(text: str, forbidden: frozenset[str] = FORBIDDEN_SHA256) -> list[str]:
    """Every personal token in ``text``; a hashed name is reported by its hash."""
    return [
        *_account_ids(text),
        *_emails(text),
        *_addresses(text),
        *_HOME.findall(text),
        *_hashed(text, forbidden),
    ]


def tracked_text_files() -> list[Path]:
    """The files git tracks, without images and other binaries."""
    listed = subprocess.run(
        ["git", "ls-files"],  # noqa: S607 - git from PATH, as every developer runs it
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [Path(name) for name in listed if Path(name).suffix.lower() not in BINARY_SUFFIXES]


def scan_history() -> list[str]:
    """Forbidden tokens in any commit message or any version of any file."""
    log = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        ["git", "log", "-p", "--all", f"--format={_COMMIT_MARK}%h%n%B"],  # noqa: S607 - git from PATH
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
    ).stdout
    found = []
    commit = "?"
    for line in log.splitlines():
        if line.startswith(_COMMIT_MARK):
            commit = line.removeprefix(_COMMIT_MARK)
        else:
            # Messages and patches alike; the diff marker is not part of the text.
            text = line[1:] if line[:1] in "+-" else line
            found += [f"{commit}: {hit}" for hit in find_forbidden(text)]
    return found


if __name__ == "__main__":
    if sys.argv[1:] != ["--history"]:
        sys.exit("usage: python -m tests.docs.hygiene --history")
    hits = scan_history()
    print("\n".join(hits) or "history is clean")
    sys.exit(1 if hits else 0)
