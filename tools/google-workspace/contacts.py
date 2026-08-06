"""Deterministic contact resolution from the canonical registry.

data/notebooks/contacts.md is THE source of truth for name -> email/handle
mapping. This module parses it and backs hard gates in gmail.send_email and
calendar.create_event/update_event: a recipient address that is not in the
registry (and not one of the boss's own accounts) is REFUSED, not guessed.

Recovery loop for a blocked send: confirm the exact address with the boss,
add it to the registry (notebook append contacts "- Name | email | role"),
then retry. One-off owner-approved override: GOODMAN_ALLOW_UNLISTED=1.
"""

import re

from goodman.config import DATA_DIR

REGISTRY_PATH = DATA_DIR / "notebooks" / "contacts.md"

OWNER_EMAILS = {
    "alex.chen@gmail.example",
    "alex.chen@acme-corp.example",
    "assistant@acme-corp.example",
}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _read_registry() -> str:
    try:
        return REGISTRY_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def known_emails() -> set[str]:
    """All email addresses in the registry, plus the boss's own accounts."""
    found = {e.lower().rstrip(".") for e in _EMAIL_RE.findall(_read_registry())}
    return found | OWNER_EMAILS


def is_known(email: str) -> bool:
    return (email or "").strip().lower().rstrip(".") in known_emails()


def unknown_recipients(to: str) -> list[str]:
    """Extract emails from a recipient string; return the unregistered ones."""
    emails = _EMAIL_RE.findall(to or "")
    known = known_emails()
    return [e for e in emails if e.lower().rstrip(".") not in known]


def resolve(query: str) -> list[str]:
    """Registry lines matching a name/handle/email query (case-insensitive)."""
    q = (query or "").strip().lower()
    if not q:
        return []
    return [
        line.strip()
        for line in _read_registry().splitlines()
        if q in line.lower() and line.strip().startswith("-")
    ]


def _persons() -> list[tuple[str, set[str]]]:
    """Group registry lines into (normalized_name, emails) records.

    A person's name is the text before the first '|', with any parenthetical
    aside stripped (so "Ivan (different from Petrov)" doesn't pollute token
    matching). Lines sharing a name are merged into one person.
    """
    by_name: dict[str, set[str]] = {}
    for line in _read_registry().splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        head = line.lstrip("-").split("|", 1)[0]
        name = re.sub(r"\([^)]*\)", "", head).strip().lower()
        if not name:
            continue
        emails = {e.lower().rstrip(".") for e in _EMAIL_RE.findall(line)}
        by_name.setdefault(name, set()).update(emails)
    return list(by_name.items())


def _preferred(emails: set[str]) -> str | None:
    """Pick a person's canonical address: a acme-corp.example one wins over anything else."""
    acme = sorted(e for e in emails if e.endswith("@acme-corp.example"))
    if acme:
        return acme[0]
    return sorted(emails)[0] if emails else None


def canonical(email: str) -> str:
    """Map an attendee address to its owner's canonical (acme-preferred) address.

    Two deterministic passes, both refusing to guess when identity is ambiguous:
      1. Exact match — if the address is listed for a person, return that
         person's preferred address (so a co-listed gmail is overridden by their
         acme-corp.example address).
      2. Surname match — for a non-acme address whose local-part contains a
         distinctive token (>=5 chars) that belongs to EXACTLY ONE registry
         person who has a acme-corp.example address, rewrite to that acme address.
         Ambiguous tokens (e.g. a shared first name across 3 people) never rewrite.
    Otherwise the address is returned unchanged (the caller's gate then decides).
    """
    e = (email or "").strip().lower().rstrip(".")
    if not e or "@" not in e:
        return email
    persons = _persons()
    for _name, emails in persons:
        if e in emails:
            return _preferred(emails) or email
    if e.endswith("@acme-corp.example"):
        return email
    local_tokens = {t for t in re.split(r"[^a-z]+", e.split("@", 1)[0]) if len(t) >= 5}
    if not local_tokens:
        return email
    hits = [
        (_preferred(emails), emails)
        for name, emails in persons
        if any(t in name.split() for t in local_tokens)
        and any(x.endswith("@acme-corp.example") for x in emails)
    ]
    if len(hits) == 1 and hits[0][0]:
        return hits[0][0]
    return email


def gate_message(unknown: list[str]) -> str:
    return (
        "BLOCKED — not in the contact registry: " + ", ".join(unknown) + ". "
        "I never send to unverified addresses (wrong-recipient protection). "
        "Confirm the exact address with Alex, add it with: "
        'notebook append contacts "- Full Name | email | role" — then retry. '
        "Owner-approved one-off: rerun with GOODMAN_ALLOW_UNLISTED=1."
    )
