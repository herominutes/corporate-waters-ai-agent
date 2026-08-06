"""DemoATS ATS integration for The Goodman Protocol.

Lets Saul (and privileged Slack peers) manage recruiting: add candidates,
read feedback/status, view funnels, add feedback, and move candidates between
funnel stages.

Auth: Bearer access token with single-use refresh token. Tokens are seeded from
.env on first run, then persisted to credentials/demoats-token.json and
auto-refreshed (and re-persisted, since the refresh token rotates) on 401.
"""

import json
import logging
import time
from pathlib import Path

import httpx

from goodman.config import (
    HUNTFLOW_BASE_URL,
    HUNTFLOW_ACCOUNT_ID,
    HUNTFLOW_ACCESS_TOKEN,
    HUNTFLOW_REFRESH_TOKEN,
)

logger = logging.getLogger(__name__)

CREDENTIALS_DIR = Path(__file__).resolve().parent.parent.parent / "credentials"
TOKEN_PATH = CREDENTIALS_DIR / "demoats-token.json"

# DemoATS rejects requests without a descriptive User-Agent.
USER_AGENT = "GoodmanProtocol/1.0 (demo@example.com)"

_TIMEOUT = 20


class DemoATSError(Exception):
    pass


def _load_tokens() -> dict:
    """Load persisted tokens, seeding from .env the first time."""
    if TOKEN_PATH.exists():
        try:
            return json.loads(TOKEN_PATH.read_text())
        except Exception as e:
            logger.warning("DemoATS token file unreadable (%s), reseeding from env", e)
    return {
        "access_token": HUNTFLOW_ACCESS_TOKEN,
        "refresh_token": HUNTFLOW_REFRESH_TOKEN,
        "expires_at": 0,  # unknown — let the first 401 trigger a refresh
    }


def _save_tokens(tokens: dict) -> None:
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(tokens, indent=2))


def _refresh_tokens(tokens: dict) -> dict:
    """Exchange the refresh token for a fresh access token (rotates refresh)."""
    refresh = tokens.get("refresh_token") or HUNTFLOW_REFRESH_TOKEN
    if not refresh:
        raise DemoATSError("No DemoATS refresh token available")
    resp = httpx.post(
        f"{HUNTFLOW_BASE_URL}/v2/token/refresh",
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        json={"refresh_token": refresh},
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        raise DemoATSError(f"Token refresh failed ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    new = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", refresh),
        "expires_at": int(time.time()) + int(data.get("expires_in", 0)) - 60,
    }
    _save_tokens(new)
    logger.info("DemoATS access token refreshed")
    return new


def _request(method: str, path: str, *, params: dict | None = None,
             json_body: dict | None = None, _retried: bool = False) -> dict:
    """Make an authenticated DemoATS API call, refreshing on 401 once."""
    tokens = _load_tokens()
    url = f"{HUNTFLOW_BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "User-Agent": USER_AGENT,
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    resp = httpx.request(method, url, headers=headers, params=params,
                         json=json_body, timeout=_TIMEOUT)

    if resp.status_code == 401 and not _retried:
        logger.info("DemoATS 401 — refreshing token and retrying")
        _refresh_tokens(tokens)
        return _request(method, path, params=params, json_body=json_body, _retried=True)

    if resp.status_code >= 400:
        raise DemoATSError(f"{method} {path} → {resp.status_code}: {resp.text[:300]}")
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


def _acc() -> str:
    if not HUNTFLOW_ACCOUNT_ID:
        raise DemoATSError("HUNTFLOW_ACCOUNT_ID not configured")
    return f"/v2/accounts/{HUNTFLOW_ACCOUNT_ID}"


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

def list_vacancies(state: str = "OPEN", count: int = 30) -> list[dict]:
    """List vacancies (the funnels). state: OPEN | CLOSED | HOLD | None=all."""
    params = {"count": count}
    if state:
        params["state"] = state
    return _request("GET", f"{_acc()}/vacancies", params=params).get("items", [])


def get_vacancy(vacancy_id: int) -> dict:
    return _request("GET", f"{_acc()}/vacancies/{vacancy_id}")


def list_divisions() -> list[dict]:
    return _request("GET", f"{_acc()}/divisions").get("items", [])


def create_vacancy(position: str, *, money: str = "", body: str = "",
                   division: int | None = None, priority: int = 0,
                   applicants_to_hire: int = 1,
                   coworkers: list[int] | None = None) -> dict:
    """Create a vacancy (a funnel). Owner/admin action.

    DemoATS requires `position` and `fill_quotas` (how many to hire). If the
    account uses divisions and none is given, defaults to the single existing
    division so creation doesn't fail.
    """
    payload: dict = {
        "position": position,
        "priority": priority,
        "fill_quotas": [{"applicants_to_hire": applicants_to_hire}],
    }
    if money:
        payload["money"] = money
    if body:
        payload["body"] = body
    if coworkers:
        payload["coworkers"] = coworkers
    if division is None:
        divs = list_divisions()
        if len(divs) == 1:
            division = divs[0]["id"]
    if division is not None:
        payload["account_division"] = division
    return _request("POST", f"{_acc()}/vacancies", json_body=payload)


def delete_vacancy(vacancy_id: int) -> dict:
    return _request("DELETE", f"{_acc()}/vacancies/{vacancy_id}")


# ---------------------------------------------------------------------------
# Vacancy access (coworker permissions)
# ---------------------------------------------------------------------------

def find_coworker(query: str) -> list[dict]:
    """Match coworkers by name or email (case-insensitive substring)."""
    q = query.strip().lower()
    out = []
    for c in list_coworkers():
        if q in (c.get("name") or "").lower() or q in (c.get("email") or "").lower():
            out.append(c)
    return out


def get_coworker(coworker_id: int) -> dict:
    return _request("GET", f"{_acc()}/coworkers/{coworker_id}")


def coworker_vacancy_ids(coworker_id: int) -> set[int]:
    """Vacancy ids a coworker currently has any permission on."""
    perms = get_coworker(coworker_id).get("permissions", [])
    return {p["vacancy"] for p in perms if p.get("vacancy")}


def _slugify(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")


def create_user(name: str, email: str, role: str = "manager",
                foreign_id: str | None = None) -> dict:
    """Register a NEW DemoATS account user (coworker) via the foreign-users API.

    role: owner | manager | watcher. Creation is async (schedules a task); we
    poll the coworkers list until the new user appears. Returns the coworker dict
    once visible, else {'scheduled': True, ...}.
    """
    if role not in ("owner", "manager", "watcher"):
        raise DemoATSError(f"Invalid role '{role}' (use owner|manager|watcher)")
    fid = foreign_id or f"ext-{_slugify(email.split('@')[0])}"
    body = {"id": fid, "name": name, "email": email, "type": role}
    task = _request("POST", f"{_acc()}/users/foreign", json_body=body)

    # Poll for the new coworker to appear (creation is asynchronous).
    for _ in range(8):
        time.sleep(2)
        for c in list_coworkers(count=100):
            if (c.get("email") or "").lower() == email.lower():
                return c
    return {"scheduled": True, "foreign_id": fid, "task": task}


def grant_vacancy_access(vacancy_id: int, coworker_id: int) -> dict:
    """Give a coworker full visibility of a vacancy's funnel.

    Watchers require explicit per-status permissions (the API rejects a bare
    vacancy grant with `pipeline.error.no_statuses`), so we grant every active
    funnel stage plus the vacancy itself.
    """
    perms = [{"permission": "status", "value": str(s["id"]), "vacancy": vacancy_id}
             for s in get_statuses()]
    perms.append({"permission": "vacancy", "value": None, "vacancy": vacancy_id})
    return _request("PUT", f"{_acc()}/vacancies/{vacancy_id}/members/{coworker_id}",
                    json_body={"permissions": perms})


def revoke_vacancy_access(vacancy_id: int, coworker_id: int) -> dict:
    return _request("DELETE", f"{_acc()}/vacancies/{vacancy_id}/members/{coworker_id}")


def get_statuses() -> list[dict]:
    """Funnel stages, ordered. Excludes removed stages."""
    items = _request("GET", f"{_acc()}/vacancies/statuses").get("items", [])
    return [s for s in items if not s.get("removed")]


def status_maps() -> tuple[dict, dict]:
    """Return ({id: name}, {name_lower: id}) for the active funnel stages."""
    by_id, by_name = {}, {}
    for s in get_statuses():
        by_id[s["id"]] = s["name"]
        by_name[s["name"].strip().lower()] = s["id"]
    return by_id, by_name


def list_coworkers(count: int = 50) -> list[dict]:
    return _request("GET", f"{_acc()}/coworkers", params={"count": count}).get("items", [])


def get_rejection_reasons() -> list[dict]:
    return _request("GET", f"{_acc()}/rejection_reasons").get("items", [])


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

def search_applicants(query: str, count: int = 10) -> list[dict]:
    """Search candidates by name/email/position."""
    return _request("GET", f"{_acc()}/applicants/search",
                    params={"q": query, "count": count}).get("items", [])


def get_applicant(applicant_id: int) -> dict:
    return _request("GET", f"{_acc()}/applicants/{applicant_id}")


def get_applicant_logs(applicant_id: int, count: int = 20) -> list[dict]:
    """Candidate activity log — status changes and feedback comments."""
    return _request("GET", f"{_acc()}/applicants/{applicant_id}/logs",
                    params={"count": count}).get("items", [])


def get_applicant_external(applicant_id: int, external_id: int) -> dict:
    """Fetch one external resume in full (parsed body + structured resume).

    The applicant object only lists external ids; the parsed CV (plain-text
    'data.body' and/or the structured 'resume' object) is only returned when an
    external is fetched by id.
    """
    return _request("GET", f"{_acc()}/applicants/{applicant_id}/externals/{external_id}")


def _reparse_file(file_obj: dict) -> str:
    """Download an attached CV file (signed url) and re-run resume parsing.

    DemoATS sometimes stores only a thin stub as the external 'body' while the
    real CV sits in the attached PDF/DOCX. Re-uploading the bytes with parsing
    enabled recovers the full extracted text.
    """
    url = file_obj.get("url")
    if not url:
        return ""
    try:
        data = httpx.get(url, timeout=60).content
        up = upload_file(data, file_obj.get("name") or "resume",
                         parse=True,
                         content_type=file_obj.get("content_type") or
                         "application/octet-stream")
        return up.get("text") or ""
    except Exception as e:
        logger.warning("CV re-parse failed for file %s: %s", file_obj.get("id"), e)
        return ""


def get_applicant_cv(applicant_id: int) -> list[dict]:
    """Return every attached resume for a candidate, each fetched in full.

    Each item: {id, body, resume, source_url, files}. 'body' is the parsed
    plain text; 'resume' is the structured object — one or both may be populated.
    When the stored body is a thin stub (and no structured resume) but a CV file
    is attached, the file is downloaded and re-parsed to fill 'body'.
    """
    a = get_applicant(applicant_id)
    out = []
    for e in a.get("external", []):
        full = get_applicant_external(applicant_id, e["id"])
        body = (full.get("data") or {}).get("body") or ""
        resume = full.get("resume") or {}
        files = full.get("files") or []
        if len(body) < 300 and not resume.get("experience") and files:
            reparsed = _reparse_file(files[0])
            if len(reparsed) > len(body):
                body = reparsed
        out.append({
            "id": e["id"],
            "body": body,
            "resume": resume,
            "source_url": full.get("source_url") or "",
            "files": files,
        })
    return out


def upload_file(file_bytes: bytes, filename: str, *, parse: bool = True,
                content_type: str = "application/octet-stream",
                _retried: bool = False) -> dict:
    """Upload a file (e.g. a CV/resume) to DemoATS storage.

    With parse=True, DemoATS runs resume parsing and the result includes
    'text' (extracted plain text) and 'fields' (name/phones/email/position/...).
    The returned 'id' is used to attach the file to a candidate via add_applicant.
    """
    tokens = _load_tokens()
    url = f"{HUNTFLOW_BASE_URL}{_acc()}/upload"
    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "User-Agent": USER_AGENT,
    }
    if parse:
        headers["X-File-Parse"] = "true"
    files = {"file": (filename, file_bytes, content_type)}
    resp = httpx.post(url, headers=headers, files=files, timeout=60)

    if resp.status_code == 401 and not _retried:
        logger.info("DemoATS 401 on upload — refreshing token and retrying")
        _refresh_tokens(tokens)
        return upload_file(file_bytes, filename, parse=parse,
                           content_type=content_type, _retried=True)
    if resp.status_code >= 400:
        raise DemoATSError(f"POST /upload → {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def add_applicant(first_name: str, last_name: str, *, middle_name: str = "",
                  position: str = "", company: str = "", money: str = "",
                  phone: str = "", email: str = "",
                  resume_id: int | None = None, resume_text: str = "") -> dict:
    """Create a candidate. Returns the created applicant (incl. id).

    If resume_id is given (from upload_file), the uploaded CV is attached to the
    candidate as an external resume, with resume_text as its parsed body.
    """
    body: dict = {"first_name": first_name, "last_name": last_name}
    if middle_name:
        body["middle_name"] = middle_name
    if position:
        body["position"] = position
    if company:
        body["company"] = company
    if money:
        body["money"] = money
    if phone:
        body["phone"] = phone
    if email:
        body["email"] = email
    if resume_id is not None:
        body["externals"] = [{
            "files": [resume_id],
            "auth_type": "NATIVE",
            "data": {"body": resume_text or ""},
        }]
    return _request("POST", f"{_acc()}/applicants", json_body=body)


def move_applicant(applicant_id: int, vacancy: int, status: int, *,
                   comment: str = "", rejection_reason: int | None = None) -> dict:
    """Add to / move a candidate on a vacancy's funnel, optionally with a comment.

    Used for: attaching a candidate to a vacancy, changing their funnel stage,
    and adding feedback (pass the current status + a comment).
    """
    body: dict = {"vacancy": vacancy, "status": status}
    if comment:
        body["comment"] = comment
    if rejection_reason is not None:
        body["rejection_reason"] = rejection_reason
    return _request("POST", f"{_acc()}/applicants/{applicant_id}/vacancy", json_body=body)


def funnel(vacancy_id: int) -> list[tuple[str, int, int]]:
    """Return [(status_name, status_id, candidate_count)] in funnel order."""
    by_id, _ = status_maps()
    ordered = get_statuses()
    counts: dict[int, int] = {}
    page = 1
    while True:
        data = _request("GET", f"{_acc()}/applicants",
                        params={"vacancy": vacancy_id, "count": 30, "page": page})
        for appl in data.get("items", []):
            for link in appl.get("links", []):
                if link.get("vacancy") == vacancy_id:
                    counts[link["status"]] = counts.get(link["status"], 0) + 1
        if page >= data.get("total_pages", 1):
            break
        page += 1
    return [(s["name"], s["id"], counts.get(s["id"], 0)) for s in ordered]
