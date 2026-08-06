"""Trello board management — stateless sync module using httpx.

Auto-discovers lists and labels from the configured board.
Returns human-readable strings (for the brain / CLI).
"""

import logging

import httpx

from goodman.config import TRELLO_API_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID

logger = logging.getLogger(__name__)

BASE_URL = "https://api.trello.com/1"

_lists_cache: dict[str, str] | None = None   # {name_lower: id}
_labels_cache: dict[str, str] | None = None  # {color: id}


def _auth_params() -> dict:
    return {"key": TRELLO_API_KEY, "token": TRELLO_TOKEN}


def _configured() -> bool:
    return bool(TRELLO_API_KEY and TRELLO_TOKEN and TRELLO_BOARD_ID)


def _trello_blocked() -> str:
    """SECURITY: tier gate — return a refusal string if a non-owner Slack caller lacks the
    'trello' capability, else '' (falsy = allowed)."""
    import os
    if os.getenv("SLACK_CALLER_ROLE"):
        from goodman import config as _cfg
        if not _cfg.caller_has(_cfg.CAP_TRELLO):
            return "You're not authorized to modify Trello on Alex's behalf."
    return ""


def _api(method: str, path: str, **kwargs) -> dict | list:
    """Make a Trello API call. Raises on HTTP error."""
    url = f"{BASE_URL}{path}"
    params = {**_auth_params(), **kwargs.pop("params", {})}
    resp = httpx.request(method, url, params=params, timeout=15, **kwargs)
    resp.raise_for_status()
    return resp.json()


# ── List / label discovery ──────────────────────────────────────────


def _load_lists() -> dict[str, str]:
    global _lists_cache
    if _lists_cache is None:
        raw = _api("GET", f"/boards/{TRELLO_BOARD_ID}/lists", params={"filter": "open"})
        _lists_cache = {lst["name"].lower(): lst["id"] for lst in raw}
        logger.info("Trello lists: %s", list(_lists_cache.keys()))
    return _lists_cache


def _load_labels() -> dict[str, str]:
    global _labels_cache
    if _labels_cache is None:
        raw = _api("GET", f"/boards/{TRELLO_BOARD_ID}/labels")
        _labels_cache = {lbl["color"]: lbl["id"] for lbl in raw if lbl.get("color")}
        logger.info("Trello labels: %s", list(_labels_cache.keys()))
    return _labels_cache


def _resolve_list(name: str) -> str | None:
    """Map list name to ID (case-insensitive)."""
    lists = _load_lists()
    return lists.get(name.lower())


def _resolve_label(color: str) -> str | None:
    """Map color name to label ID."""
    labels = _load_labels()
    return labels.get(color.lower())


def invalidate_cache():
    """Clear cached lists/labels (useful after board changes)."""
    global _lists_cache, _labels_cache
    _lists_cache = None
    _labels_cache = None


# ── Public API ──────────────────────────────────────────────────────


def get_lists() -> str:
    """Return board lists as a formatted string."""
    if not _configured():
        return "Trello not configured."
    lists = _load_lists()
    return "\n".join(f"- {name}" for name in lists)


def get_labels() -> str:
    """Return board labels as a formatted string."""
    if not _configured():
        return "Trello not configured."
    labels = _load_labels()
    return "\n".join(f"- {color}" for color in labels)


def create_card(
    list_name: str,
    name: str,
    desc: str = "",
    priority: str | None = None,
    due: str | None = None,
) -> str:
    """Create a card on the board. Returns confirmation string."""
    if _trello_blocked():
        return _trello_blocked()
    if not _configured():
        return "Trello not configured."

    list_id = _resolve_list(list_name)
    if not list_id:
        available = ", ".join(_load_lists().keys())
        return f"List '{list_name}' not found. Available: {available}"

    params: dict = {"idList": list_id, "name": name}
    if desc:
        params["desc"] = desc
    if due:
        params["due"] = due

    if priority:
        label_id = _resolve_label(priority)
        if label_id:
            params["idLabels"] = label_id

    card = _api("POST", "/cards", params=params)
    label_str = f" [{priority}]" if priority else ""
    return f"Created card '{name}'{label_str} in {list_name} — {card.get('shortUrl', '')}"


def list_cards(list_name: str | None = None) -> str:
    """List cards, optionally filtered by list name."""
    if not _configured():
        return "Trello not configured."

    if list_name:
        list_id = _resolve_list(list_name)
        if not list_id:
            return f"List '{list_name}' not found."
        cards = _api("GET", f"/lists/{list_id}/cards")
    else:
        cards = _api("GET", f"/boards/{TRELLO_BOARD_ID}/cards", params={"filter": "open"})

    if not cards:
        return "No cards found."

    lines = []
    for card in cards:
        labels = ", ".join(lbl.get("color", "?") for lbl in card.get("labels", []))
        label_str = f" [{labels}]" if labels else ""
        due_str = f" (due {card['due'][:10]})" if card.get("due") else ""
        lines.append(f"- {card['name']}{label_str}{due_str}")
    return "\n".join(lines)


def move_card(card_name: str, to_list: str) -> str:
    """Move a card to a different list. Fuzzy-matches card name."""
    if _trello_blocked():
        return _trello_blocked()
    if not _configured():
        return "Trello not configured."

    list_id = _resolve_list(to_list)
    if not list_id:
        return f"List '{to_list}' not found."

    # Search for card by name (fuzzy — case-insensitive substring)
    cards = _api("GET", f"/boards/{TRELLO_BOARD_ID}/cards", params={"filter": "open"})
    target = card_name.lower()
    match = None
    for card in cards:
        if target in card["name"].lower():
            match = card
            break

    if not match:
        return f"Card matching '{card_name}' not found."

    _api("PUT", f"/cards/{match['id']}", params={"idList": list_id})
    return f"Moved '{match['name']}' to {to_list}."


def update_card(card_name: str, priority: str | None = None) -> str:
    """Update a card's priority label. Fuzzy-matches card name."""
    if _trello_blocked():
        return _trello_blocked()
    if not _configured():
        return "Trello not configured."

    cards = _api("GET", f"/boards/{TRELLO_BOARD_ID}/cards", params={"filter": "open"})
    target = card_name.lower()
    match = None
    for card in cards:
        if target in card["name"].lower():
            match = card
            break

    if not match:
        return f"Card matching '{card_name}' not found."

    if priority:
        label_id = _resolve_label(priority)
        if not label_id:
            return f"Label color '{priority}' not found."
        # Replace all labels with the new one
        _api("PUT", f"/cards/{match['id']}", params={"idLabels": label_id})
        return f"Updated '{match['name']}' → priority [{priority}]"

    return f"Nothing to update on '{match['name']}'."


def get_board_info() -> str:
    """Overview: columns + card counts + recent cards."""
    if not _configured():
        return "Trello not configured."

    lists = _api("GET", f"/boards/{TRELLO_BOARD_ID}/lists", params={"filter": "open"})
    cards = _api("GET", f"/boards/{TRELLO_BOARD_ID}/cards", params={"filter": "open"})

    # Group cards by list
    by_list: dict[str, list] = {lst["id"]: [] for lst in lists}
    for card in cards:
        lid = card["idList"]
        if lid in by_list:
            by_list[lid].append(card)

    lines = [f"Board: {TRELLO_BOARD_ID}", ""]
    for lst in lists:
        lst_cards = by_list.get(lst["id"], [])
        lines.append(f"{lst['name']} ({len(lst_cards)} cards):")
        for card in lst_cards[:5]:
            labels = ", ".join(lbl.get("color", "?") for lbl in card.get("labels", []))
            label_str = f" [{labels}]" if labels else ""
            lines.append(f"  - {card['name']}{label_str}")
        if len(lst_cards) > 5:
            lines.append(f"  ... and {len(lst_cards) - 5} more")
        lines.append("")

    return "\n".join(lines)
