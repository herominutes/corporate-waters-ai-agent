"""Google Calendar integration for The Goodman Protocol.

Supports multiple OAuth accounts (personal + work) with automatic
calendar discovery and token refresh per account.
"""

import base64
import json
import logging
import os
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from goodman.config import SERVICE_ACCOUNT_PATH, TIMEZONE

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

CREDENTIALS_DIR = Path(__file__).resolve().parent.parent.parent / "credentials"

# Legacy single-token path (kept for backward compat)
OAUTH_TOKENS_PATH = CREDENTIALS_DIR / "user-tokens.json"

# Multi-account tokens directory
OAUTH_TOKENS_DIR = CREDENTIALS_DIR / "oauth-tokens"

# Fallback calendar IDs for service account mode
CALENDAR_IDS = [
    "alex.chen@gmail.example",
    "alex.chen@acme-corp.example",
]

# Cache: {token_file_path: (creds, service, calendar_ids)}
_account_cache: dict[str, tuple] = {}

# Shared personal-assistant account. Meetings requested by non-owner Slack
# callers are booked from here (organizer + invite sender) instead of Alex's
# work calendar — see _organizer_account_override().
PA_ACCOUNT = "assistant@acme-corp.example"

# Accounts/calendars excluded from proactive summary views (today/upcoming/
# heartbeat). The PA calendar stays fully usable for create/find/availability,
# it's just kept out of the boss's at-a-glance digests to avoid noise.
SUMMARY_EXCLUDED = {PA_ACCOUNT}


def _organizer_account_override() -> str | None:
    """Return the account a booking must come from, based on caller context.

    brain.py sets SLACK_CALLER_ROLE for any Slack request that isn't the owner
    (Alex), and the tool subprocess inherits it. For those callers every
    meeting is booked from the shared PA account so it never lands on / sends
    invites from Alex's personal work calendar. Owner / Telegram (var unset)
    keeps the normal calendar the request specified.
    """
    return PA_ACCOUNT if os.getenv("SLACK_CALLER_ROLE") else None


def _find_all_token_files() -> list[Path]:
    """Find all OAuth token files. Checks multi-account dir first, then legacy."""
    files = []
    if OAUTH_TOKENS_DIR.exists():
        files.extend(sorted(OAUTH_TOKENS_DIR.glob("*.json")))
    if not files and OAUTH_TOKENS_PATH.exists():
        files.append(OAUTH_TOKENS_PATH)
    return files


# Dead-token quarantine: a revoked refresh token (invalid_grant) makes EVERY
# calendar call pay a ~1.5s failed-refresh penalty, serially, until re-auth.
# Quarantined tokens are skipped entirely for a cooldown window.
_TOKEN_QUARANTINE: dict[str, float] = {}   # token_path -> retry_after unix ts
_EMAIL_TO_TOKEN: dict[str, str] = {}
_QUARANTINE_SECS = 1800


def _quarantine_token(token_path, why) -> None:
    import time as _time
    _TOKEN_QUARANTINE[str(token_path)] = _time.time() + _QUARANTINE_SECS
    logger.warning("OAuth token QUARANTINED for %ds (%s): %s",
                   _QUARANTINE_SECS, token_path, why)


def _load_oauth_creds_from_file(token_path: Path):
    """Load and refresh OAuth credentials from a specific token file."""
    import time as _time
    if _TOKEN_QUARANTINE.get(str(token_path), 0) > _time.time():
        return None
    if not token_path.exists():
        return None
    try:
        data = json.loads(token_path.read_text())
        # Stored expiry may be a Unix timestamp (int/float) OR an ISO-8601 string.
        # It MUST be loaded into Credentials, otherwise creds.expired is always False
        # and the access token (lives ~1h) is never refreshed via the refresh_token —
        # which forced a manual re-auth every day.
        expiry_dt = None
        exp = data.get("expiry")
        if exp not in (None, ""):
            try:
                expiry_dt = datetime.utcfromtimestamp(float(exp))
            except (TypeError, ValueError):
                try:
                    dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                    expiry_dt = (dt.astimezone(timezone.utc).replace(tzinfo=None)
                                 if dt.tzinfo else dt)
                except ValueError:
                    expiry_dt = None
        creds = Credentials(
            token=data.get("access_token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=SCOPES,
            expiry=expiry_dt,
        )
        if creds.expired or not creds.token:
            creds.refresh(Request())
            data["access_token"] = creds.token
            data["expiry"] = int(datetime.now().timestamp()) + 3500
            token_path.write_text(json.dumps(data, indent=2))
            logger.info("OAuth token refreshed for %s", token_path.name)
        return creds
    except Exception as e:
        logger.warning("OAuth creds failed for %s: %s", token_path.name, e)
        if "invalid_grant" in str(e):
            _quarantine_token(token_path, e)
        return None


def _load_oauth_creds():
    """Load OAuth creds from the first available token file (legacy compat)."""
    for f in _find_all_token_files():
        creds = _load_oauth_creds_from_file(f)
        if creds:
            return creds
    return None


def _get_all_services() -> list[tuple]:
    """Build Calendar API services for ALL OAuth accounts.

    Returns list of (service, account_email) tuples.
    Falls back to service account if no OAuth tokens found.
    """
    token_files = _find_all_token_files()
    services = []
    for tf in token_files:
        creds = _load_oauth_creds_from_file(tf)
        if creds:
            data = json.loads(tf.read_text())
            email = data.get("email", tf.stem)
            _EMAIL_TO_TOKEN[email] = str(tf)
            service = build("calendar", "v3", credentials=creds)
            services.append((service, email))
    if not services:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_PATH, scopes=SCOPES
        )
        services.append((build("calendar", "v3", credentials=creds), "service-account"))
    return services


def _get_service():
    """Build Calendar API service. Prefers OAuth, falls back to service account."""
    creds = _load_oauth_creds()
    if creds:
        logger.debug("Using OAuth credentials for Calendar")
        return build("calendar", "v3", credentials=creds)
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_PATH, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds)


def _get_all_calendar_ids() -> list[tuple[str, str]]:
    """Discover all calendar IDs across all OAuth accounts.

    Returns list of (calendar_id, account_email) tuples.
    """
    all_ids = []
    seen = set()
    for service, email in _get_all_services():
        try:
            result = service.calendarList().list().execute()
            for item in result.get("items", []):
                cal_id = item["id"]
                if cal_id not in seen:
                    seen.add(cal_id)
                    all_ids.append((cal_id, email))
            logger.info("Discovered %d calendars for %s", len(result.get("items", [])), email)
        except Exception as e:
            logger.warning("Calendar discovery failed for %s: %s", email, e)
            if "invalid_grant" in str(e) and email in _EMAIL_TO_TOKEN:
                _quarantine_token(_EMAIL_TO_TOKEN[email], e)
    if not all_ids:
        return [(cid, "fallback") for cid in CALENDAR_IDS]
    return all_ids


def _get_service_for_account(account_email: str):
    """Get a Calendar service for a specific OAuth account."""
    for service, email in _get_all_services():
        if email == account_email:
            return service
    return _get_service()


def _get_calendar_ids(service):
    """Get calendar IDs (legacy compat). Auto-discovers from API with OAuth."""
    if _load_oauth_creds():
        try:
            result = service.calendarList().list().execute()
            ids = [item["id"] for item in result.get("items", [])]
            logger.info("Discovered %d calendars", len(ids))
            return ids
        except Exception as e:
            logger.warning("Calendar discovery failed: %s", e)
            return CALENDAR_IDS
    return CALENDAR_IDS


def _fetch_events_multi(time_min: str, time_max: str) -> list[tuple]:
    """Fetch events across all accounts and calendars.

    Returns list of (start, summary, cal_id) tuples, sorted by start time.
    """
    all_events = []
    seen_event_ids = set()

    for service, account_email in _get_all_services():
        if account_email in SUMMARY_EXCLUDED:
            continue
        try:
            cal_list = service.calendarList().list().execute()
            cal_ids = [item["id"] for item in cal_list.get("items", [])]
        except Exception as e:
            logger.warning("Calendar list failed for %s: %s", account_email, e)
            cal_ids = []

        for cal_id in cal_ids:
            if cal_id in SUMMARY_EXCLUDED:
                continue
            try:
                result = service.events().list(
                    calendarId=cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                ).execute()
                for event in result.get("items", []):
                    eid = event.get("id", "") + cal_id
                    if eid in seen_event_ids:
                        continue
                    seen_event_ids.add(eid)
                    start = event["start"].get("dateTime", event["start"].get("date"))
                    summary = event.get("summary", "No title")
                    all_events.append((start, summary, cal_id))
            except Exception as e:
                logger.warning("Failed to fetch calendar %s (%s): %s", cal_id, account_email, e)
                all_events.append(("error", f"[Could not access {cal_id}: {e}]", cal_id))

    all_events.sort(key=lambda x: x[0])
    return all_events


def get_upcoming_events_today() -> str:
    """Get only FUTURE events for the rest of today (not past ones). Used by heartbeat."""
    try:
        tz = ZoneInfo(TIMEZONE)
        now = datetime.now(tz)
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)

        all_events = _fetch_events_multi(now.isoformat(), end_of_day.isoformat())

        if not all_events:
            return "No upcoming events for the rest of today."

        lines = []
        for start, summary, cal_id in all_events:
            if start == "error":
                continue
            try:
                dt = datetime.fromisoformat(start)
                time_str = dt.strftime("%I:%M %p")
            except (ValueError, TypeError):
                time_str = start
            account = cal_id.split("@")[0]
            lines.append(f"- {time_str}: {summary} [{account}]")

        return "Upcoming today:\n" + "\n".join(lines) if lines else "No upcoming events for the rest of today."

    except Exception as e:
        logger.error("Calendar error: %s", e)
        return f"Calendar check failed: {e}"


def get_todays_events() -> str:
    """Get today's events from all calendars across all accounts."""
    try:
        tz = ZoneInfo(TIMEZONE)
        now = datetime.now(tz)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)

        all_events = _fetch_events_multi(start_of_day.isoformat(), end_of_day.isoformat())

        if not all_events:
            return "No events on the docket today. Calendar is clean."

        lines = []
        for start, summary, cal_id in all_events:
            if start == "error":
                lines.append(summary)
                continue
            try:
                dt = datetime.fromisoformat(start)
                time_str = dt.strftime("%I:%M %p")
            except (ValueError, TypeError):
                time_str = start
            account = cal_id.split("@")[0]
            lines.append(f"- {time_str}: {summary} [{account}]")

        return "Today's calendar:\n" + "\n".join(lines)

    except Exception as e:
        logger.error("Calendar error: %s", e)
        return f"Calendar access hit a snag: {e}"


def get_upcoming_events(days: int = 7) -> str:
    """Get upcoming events for the next N days across all accounts."""
    try:
        tz = ZoneInfo(TIMEZONE)
        now = datetime.now(tz)
        end = now + timedelta(days=days)

        all_events = _fetch_events_multi(now.isoformat(), end.isoformat())

        if not all_events:
            return f"Nothing on the books for the next {days} days."

        lines = []
        current_date = None
        for start, summary, cal_id in all_events:
            try:
                dt = datetime.fromisoformat(start)
                date_str = dt.strftime("%a %b %d")
                time_str = dt.strftime("%I:%M %p")
            except (ValueError, TypeError):
                date_str = start
                time_str = "All day"
            if date_str != current_date:
                current_date = date_str
                lines.append(f"\n{date_str}:")
            account = cal_id.split("@")[0]
            lines.append(f"  - {time_str}: {summary} [{account}]")

        return f"Next {days} days:" + "\n".join(lines)

    except Exception as e:
        logger.error("Calendar error: %s", e)
        return f"Calendar access hit a snag: {e}"


def get_events_for_date(date_str: str) -> str:
    """Get events for a specific date (YYYY-MM-DD format) across all accounts."""
    try:
        tz = ZoneInfo(TIMEZONE)
        date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz)
        end = date + timedelta(days=1)

        all_events = _fetch_events_multi(date.isoformat(), end.isoformat())

        if not all_events:
            return f"No events on {date_str}. Calendar is clear."

        lines = [f"Events on {date_str}:"]
        for start, summary, cal_id in all_events:
            try:
                dt = datetime.fromisoformat(start)
                time_str = dt.strftime("%I:%M %p")
            except (ValueError, TypeError):
                time_str = "All day"
            account = cal_id.split("@")[0]
            lines.append(f"  - {time_str}: {summary} [{account}]")
        return "\n".join(lines)

    except Exception as e:
        logger.error("Calendar error: %s", e)
        return f"Calendar access hit a snag: {e}"


def check_availability(
    emails: list[str],
    date_str: str,
    start_hour: int = 9,
    end_hour: int = 18,
) -> str:
    """Check availability of people using the FreeBusy API.

    Args:
        emails: List of email addresses to check.
        date_str: Date in YYYY-MM-DD format.
        start_hour: Start of window (24h format, default 9).
        end_hour: End of window (24h format, default 18).

    Returns:
        Formatted string showing busy/free times for each person.
    """
    try:
        # Use the workspace service if checking workspace emails,
        # otherwise fall back to default
        workspace_domain = None
        for email in emails:
            domain = email.split("@")[-1] if "@" in email else ""
            if domain and domain != "gmail.com":
                workspace_domain = domain
                break

        service = _get_service()
        if workspace_domain:
            all_services = _get_all_services()
            # Prefer a service whose account is one of the queried emails
            # (e.g. the organizer) — multiple accounts can share a domain and
            # some (personalassistant@) have no free/busy access.
            emails_lower = {e.lower() for e in emails}
            for svc, acct_email in all_services:
                if acct_email.lower() in emails_lower:
                    service = svc
                    break
            else:
                for svc, acct_email in all_services:
                    if workspace_domain in acct_email:
                        service = svc
                        break

        tz = ZoneInfo(TIMEZONE)
        date = datetime.strptime(date_str, "%Y-%m-%d")
        time_min = date.replace(hour=start_hour, minute=0, tzinfo=tz).isoformat()
        time_max = date.replace(hour=end_hour, minute=0, tzinfo=tz).isoformat()

        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "timeZone": TIMEZONE,
            "items": [{"id": email} for email in emails],
        }

        result = service.freebusy().query(body=body).execute()
        calendars = result.get("calendars", {})

        lines = [f"Availability for {date_str} ({start_hour:02d}:00-{end_hour:02d}:00 {TIMEZONE}):"]

        for email in emails:
            cal_data = calendars.get(email, {})
            errors = cal_data.get("errors", [])
            if errors:
                lines.append(f"\n  {email}: Could not check — {errors[0].get('reason', 'unknown error')}")
                continue

            busy_periods = cal_data.get("busy", [])
            if not busy_periods:
                lines.append(f"\n  {email}: Free all day ({start_hour:02d}:00-{end_hour:02d}:00)")
            else:
                busy_strs = []
                for period in busy_periods:
                    start = datetime.fromisoformat(period["start"]).astimezone(tz)
                    end = datetime.fromisoformat(period["end"]).astimezone(tz)
                    busy_strs.append(f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}")
                lines.append(f"\n  {email}: Busy at {', '.join(busy_strs)}")

                # Calculate free slots
                free_slots = []
                cursor = date.replace(hour=start_hour, minute=0, tzinfo=tz)
                window_end = date.replace(hour=end_hour, minute=0, tzinfo=tz)
                for period in sorted(busy_periods, key=lambda p: p["start"]):
                    busy_start = datetime.fromisoformat(period["start"]).astimezone(tz)
                    busy_end = datetime.fromisoformat(period["end"]).astimezone(tz)
                    if cursor < busy_start:
                        free_slots.append(f"{cursor.strftime('%H:%M')}-{busy_start.strftime('%H:%M')}")
                    cursor = max(cursor, busy_end)
                if cursor < window_end:
                    free_slots.append(f"{cursor.strftime('%H:%M')}-{window_end.strftime('%H:%M')}")
                if free_slots:
                    lines.append(f"    Free: {', '.join(free_slots)}")

        return "\n".join(lines)

    except Exception as e:
        logger.error("FreeBusy check failed: %s", e)
        return f"Could not check availability: {e}"


def _send_ics_invite(
    result_event: dict,
    organizer_email: str,
    external_attendees: list[str],
):
    """Send ICS calendar invite emails to external attendees.

    Google Workspace may block outbound calendar notifications to external
    domains. This bypasses that by sending a proper ICS email directly.
    """
    try:
        from goodman.gmail import _get_all_accounts, _get_oauth_access_token
    except ImportError:
        logger.warning("gmail module not available, cannot send ICS invites")
        return

    summary = result_event.get("summary", "Meeting")
    description = result_event.get("description", "")
    location = result_event.get("location", "")
    event_id = result_event.get("id", _uuid.uuid4().hex)
    start_dt = result_event.get("start", {}).get("dateTime", "")
    end_dt = result_event.get("end", {}).get("dateTime", "")

    meet_uri = ""
    for ep in result_event.get("conferenceData", {}).get("entryPoints", []):
        if ep.get("entryPointType") == "video":
            meet_uri = ep.get("uri", "")
            break

    def _to_ics_dt(iso_str: str) -> str:
        dt = datetime.fromisoformat(iso_str)
        utc_dt = dt.astimezone(ZoneInfo("UTC"))
        return utc_dt.strftime("%Y%m%dT%H%M%SZ")

    start_ics = _to_ics_dt(start_dt)
    end_ics = _to_ics_dt(end_dt)
    now_ics = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")

    attendee_lines = "\n".join(
        f"ATTENDEE;RSVP=TRUE;CN={email}:mailto:{email}"
        for email in external_attendees
    )

    desc_escaped = description.replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
    if meet_uri:
        desc_escaped += f"\\n\\nGoogle Meet: {meet_uri}"

    ics_content = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//OpenClaw//Goodman//EN\r\n"
        "METHOD:REQUEST\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{event_id}@google.com\r\n"
        f"DTSTART:{start_ics}\r\n"
        f"DTEND:{end_ics}\r\n"
        f"DTSTAMP:{now_ics}\r\n"
        f"ORGANIZER;CN={organizer_email}:mailto:{organizer_email}\r\n"
        f"{attendee_lines}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"DESCRIPTION:{desc_escaped}\r\n"
        f"LOCATION:{location}\r\n"
        "STATUS:CONFIRMED\r\n"
        "SEQUENCE:0\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    # Find a working email account to send from. Prefer the exact organizer
    # address (multiple accounts can share a domain), then same-domain, then any.
    accounts = _get_all_accounts()
    organizer_domain = organizer_email.split("@")[-1]
    send_account = None
    for acct in accounts:
        if acct["email"] == organizer_email:
            token = _get_oauth_access_token(acct["token_file"])
            if token:
                send_account = acct
                break
    if not send_account:
        for acct in accounts:
            if organizer_domain in acct["email"]:
                token = _get_oauth_access_token(acct["token_file"])
                if token:
                    send_account = acct
                    break
    if not send_account:
        for acct in accounts:
            token = _get_oauth_access_token(acct["token_file"])
            if token:
                send_account = acct
                break
    if not send_account:
        logger.warning("No working email account to send ICS invites")
        return

    for attendee_email in external_attendees:
        try:
            msg = MIMEMultipart("mixed")
            msg["From"] = send_account["email"]
            msg["To"] = attendee_email
            msg["Subject"] = f"Invitation: {summary}"

            # Human-readable part
            start_local = datetime.fromisoformat(start_dt)
            time_str = start_local.strftime("%A, %B %d at %H:%M")
            body_text = f"You have been invited to: {summary}\nWhen: {time_str}\n"
            if location:
                body_text += f"Where: {location}\n"
            if meet_uri:
                body_text += f"Google Meet: {meet_uri}\n"
            if description:
                body_text += f"\n{description}\n"
            msg.attach(MIMEText(body_text, "plain"))

            # ICS attachment
            ics_part = MIMEText(ics_content, "calendar", "utf-8")
            ics_part.add_header("Content-Disposition", "attachment", filename="invite.ics")
            msg.attach(ics_part)

            # Send via Gmail API
            token = _get_oauth_access_token(send_account["token_file"])
            import httpx
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            resp = httpx.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                headers={"Authorization": f"Bearer {token}"},
                json={"raw": raw},
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("ICS invite sent to %s for '%s'", attendee_email, summary)
        except Exception as e:
            logger.warning("Failed to send ICS invite to %s: %s", attendee_email, e)


def _canonicalize_attendees(attendees: list[str]) -> list[str]:
    """Rewrite each attendee to its owner's acme-preferred canonical address."""
    from goodman import contacts as _contacts
    out = []
    for a in attendees:
        c = _contacts.canonical(a)
        if c != a:
            logger.warning("CONTACT CANONICAL: rewrote attendee %s -> %s", a, c)
        out.append(c)
    return out


def create_event(
    summary: str,
    start_time: str,
    end_time: str,
    calendar_id: str | None = None,
    description: str = "",
    attendees: list[str] | None = None,
    location: str = "",
    add_meet: bool = False,
    recurrence: str | None = None,
) -> str:
    """Create a calendar event with attendees, optional Meet link, and recurrence."""
    try:
        import os
        if os.getenv("SLACK_CALLER_ROLE"):
            from goodman import config as _cfg
            if not _cfg.caller_has(_cfg.CAP_CALENDAR):
                return "You're not authorized to create calendar events on Alex's behalf."
        # CONTACT GATE: attendee emails must come from the canonical registry
        # (data/notebooks/contacts.md) — never free-associated from chat context.
        if attendees:
            from goodman import contacts as _contacts
            # Canonicalize ALWAYS (even in override mode) so a stale gmail or
            # misremembered local-part still maps to the person's acme address.
            attendees = _canonicalize_attendees(attendees)
            unknown = [a for a in attendees if not _contacts.is_known(a)]
            if unknown and os.getenv("GOODMAN_ALLOW_UNLISTED") == "1":
                logger.warning("CONTACT GATE: OVERRIDE — unlisted %s invited on owner approval", unknown)
            elif unknown:
                logger.warning("CONTACT GATE: blocked event invite to unlisted %s", unknown)
                return _contacts.gate_message(unknown)
        cal_id = calendar_id or CALENDAR_IDS[0]

        # Non-owner Slack callers always book from the shared PA account,
        # regardless of which calendar the request specified.
        forced = _organizer_account_override()
        if forced:
            cal_id = forced

        # Pick the right service for the target calendar
        service = _get_service_for_calendar(cal_id)

        event = {
            "summary": summary,
            "start": {"dateTime": start_time, "timeZone": TIMEZONE},
            "end": {"dateTime": end_time, "timeZone": TIMEZONE},
        }

        if recurrence:
            event["recurrence"] = [recurrence]
        if description:
            event["description"] = description
        if location:
            event["location"] = location
        if attendees:
            event["attendees"] = [{"email": e} for e in attendees]
        if add_meet:
            import uuid
            event["conferenceData"] = {
                "createRequest": {"requestId": uuid.uuid4().hex}
            }

        # AUDIT: log what we're creating
        logger.warning("CALENDAR AUDIT: CREATING event '%s' at %s on cal=%s, attendees=%s",
                       summary, start_time, cal_id, attendees or [])

        # Try with attendees + meet first
        conference_ver = 1 if add_meet else 0
        try:
            result = service.events().insert(
                calendarId=cal_id, body=event,
                conferenceDataVersion=conference_ver,
                sendUpdates="all" if attendees else "none",
            ).execute()
        except Exception as e:
            logger.warning("Full create failed (%s), retrying without attendees/meet", e)
            # Strip attendees and meet, put attendees in description as fallback
            fallback_parts = []
            if description:
                fallback_parts.append(description)
            if attendees:
                event.pop("attendees", None)
                fallback_parts.append(
                    "Invited:\n" + "\n".join(f"  • {email}" for email in attendees)
                )
            if fallback_parts:
                event["description"] = "\n\n".join(fallback_parts)
            event.pop("conferenceData", None)
            result = service.events().insert(
                calendarId=cal_id, body=event, sendUpdates="none",
            ).execute()

        link = result.get("htmlLink", "done")
        lines = [f"Event created: {summary}", f"Link: {link}"]

        # Report Meet link if we got one
        meet_uri = None
        conf = result.get("conferenceData", {})
        for ep in conf.get("entryPoints", []):
            if ep.get("entryPointType") == "video":
                meet_uri = ep.get("uri")
                break
        if meet_uri:
            lines.append(f"Meet: {meet_uri}")
        elif add_meet:
            lines.append("Note: Meet link couldn't be auto-created — add it manually in the event.")

        # Report attendee status
        result_attendees = result.get("attendees", [])
        if result_attendees:
            # Echo the FINAL addresses (post-canonicalization) so the reporting
            # layer quotes reality instead of re-typing from memory.
            final = ", ".join(a["email"] for a in result_attendees if a.get("email"))
            lines.append(f"Invited ({len(result_attendees)}): {final}")
        elif attendees:
            lines.append("Attendees listed in description — add them as guests manually.")

        # Send ICS invites to external attendees (Workspace may block API notifications)
        if attendees and result_attendees:
            organizer_domain = cal_id.split("@")[-1] if "@" in cal_id else ""
            external = [
                a["email"] for a in result_attendees
                if a.get("email") and a["email"].split("@")[-1] != organizer_domain
            ]
            if external:
                try:
                    _send_ics_invite(result, cal_id, external)
                    lines.append(f"ICS invite emailed to {len(external)} external attendee(s).")
                except Exception as e:
                    logger.warning("ICS invite sending failed: %s", e)

        return "\n".join(lines)

    except Exception as e:
        logger.error("Failed to create event: %s", e)
        return f"Couldn't create the event: {e}"


def find_events(query: str, days_ahead: int = 30, days_back: int = 0) -> list[dict]:
    """Search for events matching a query string across all calendars.

    Args:
        query: Search string (matches event titles and attendee emails).
        days_ahead: How many days forward to search (default 30).
        days_back: How many days backward to search (default 0). Use this to find
                   past events, e.g., to look up attendee emails from previous meetings.

    Returns list of dicts with full event details including duration info.
    """
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    time_min = (now - timedelta(days=days_back)).isoformat() if days_back else now.isoformat()
    time_max = (now + timedelta(days=days_ahead)).isoformat()
    query_lower = query.lower()

    matches = []
    for service, account_email in _get_all_services():
        try:
            cal_list = service.calendarList().list().execute()
            cal_ids = [item["id"] for item in cal_list.get("items", [])]
        except Exception:
            continue

        for cal_id in cal_ids:
            try:
                result = service.events().list(
                    calendarId=cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=False,  # keep recurring event masters
                    q=query,
                ).execute()
                for event in result.get("items", []):
                    summary = event.get("summary", "")
                    if query_lower in summary.lower():
                        start = event.get("start", {})
                        end = event.get("end", {})
                        start_str = start.get("dateTime", start.get("date", ""))
                        end_str = end.get("dateTime", end.get("date", ""))
                        # Calculate duration in minutes
                        duration_min = None
                        try:
                            s = datetime.fromisoformat(start_str)
                            e = datetime.fromisoformat(end_str)
                            duration_min = int((e - s).total_seconds() / 60)
                        except (ValueError, TypeError):
                            pass
                        matches.append({
                            "id": event["id"],
                            "summary": summary,
                            "start": start_str,
                            "end": end_str,
                            "duration_minutes": duration_min,
                            "calendar_id": cal_id,
                            "account": account_email,
                            "description": event.get("description", ""),
                            "location": event.get("location", ""),
                            "recurrence": event.get("recurrence"),
                            "attendees": [
                                a.get("email") for a in event.get("attendees", [])
                            ],
                        })
            except Exception as e:
                logger.warning("Event search failed on %s: %s", cal_id, e)

    return matches


def _slack_caller_may_mutate(service, cal_id: str, event_id: str, verb: str) -> str | None:
    """SECURITY GATE for calendar mutations by non-owner Slack callers.
    They may only delete/update events ORGANIZED or CREATED by the PA account (i.e. booked
    through Saul on their behalf) — never Alex's own meetings. Returns a refusal string
    to abort, or None to allow."""
    import os
    if not os.getenv("SLACK_CALLER_ROLE"):
        return None  # owner / internal — unrestricted
    from goodman import config as _cfg
    if not _cfg.caller_has(_cfg.CAP_CALENDAR):
        return f"You're not authorized to {verb} calendar events."
    try:
        ev = service.events().get(calendarId=cal_id, eventId=event_id).execute()
    except Exception:
        return f"I can't verify who owns that event, so I won't {verb} it."
    org = (ev.get("organizer") or {}).get("email", "")
    creator = (ev.get("creator") or {}).get("email", "")
    if PA_ACCOUNT not in (org, creator):
        logger.warning("CALENDAR GATE: non-owner BLOCKED from %s '%s' (organizer=%s creator=%s)",
                       verb, ev.get("summary", "?"), org, creator)
        return (f"I can only {verb} meetings that were booked through me; this one wasn't "
                f"(it's organized elsewhere), so I can't touch it.")
    return None


def delete_event(event_id: str, calendar_id: str | None = None) -> str:
    """Delete a calendar event by its ID."""
    try:
        cal_id = calendar_id or CALENDAR_IDS[0]
        service = _get_service_for_calendar(cal_id)
        gate = _slack_caller_may_mutate(service, cal_id, event_id, "cancel")
        if gate:
            return gate
        # AUDIT: log what we're about to delete
        try:
            event = service.events().get(calendarId=cal_id, eventId=event_id).execute()
            summary = event.get("summary", "?")
            start = event.get("start", {}).get("dateTime", "?")
            attendees = [a.get("email", "") for a in event.get("attendees", [])]
            logger.warning("CALENDAR AUDIT: DELETING event '%s' at %s (id=%s, attendees=%s)",
                           summary, start, event_id, attendees)
        except Exception:
            logger.warning("CALENDAR AUDIT: DELETING event id=%s (could not fetch details)", event_id)
        service.events().delete(
            calendarId=cal_id, eventId=event_id, sendUpdates="all",
        ).execute()
        return f"Event {event_id} deleted."
    except Exception as e:
        logger.error("Failed to delete event %s: %s", event_id, e)
        return f"Couldn't delete the event: {e}"


def update_event(
    event_id: str,
    calendar_id: str | None = None,
    summary: str | None = None,
    description: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    recurrence: str | None = None,
    attendees: list[str] | None = None,
) -> str:
    """Update an existing calendar event (partial update via patch)."""
    try:
        cal_id = calendar_id or CALENDAR_IDS[0]
        service = _get_service_for_calendar(cal_id)
        gate = _slack_caller_may_mutate(service, cal_id, event_id, "reschedule")
        if gate:
            return gate

        if attendees:
            from goodman import contacts as _contacts
            attendees = _canonicalize_attendees(attendees)
            unknown = [a for a in attendees if not _contacts.is_known(a)]
            if unknown and os.getenv("GOODMAN_ALLOW_UNLISTED") == "1":
                logger.warning("CONTACT GATE: OVERRIDE — unlisted %s on event update, owner approval assumed", unknown)
            elif unknown:
                logger.warning("CONTACT GATE: blocked event update with unlisted %s", unknown)
                return _contacts.gate_message(unknown)

        body: dict = {}
        if summary:
            body["summary"] = summary
        if description is not None:
            body["description"] = description
        if start_time:
            body["start"] = {"dateTime": start_time, "timeZone": TIMEZONE}
        if end_time:
            body["end"] = {"dateTime": end_time, "timeZone": TIMEZONE}
        if recurrence is not None:
            body["recurrence"] = [recurrence] if recurrence else []
        if attendees is not None:
            body["attendees"] = [{"email": e} for e in attendees]

        result = service.events().patch(
            calendarId=cal_id, eventId=event_id, body=body,
            sendUpdates="all" if attendees else "none",
        ).execute()
        link = result.get("htmlLink", "done")
        lines = [f"Event updated: {result.get('summary', '')} — {link}"]

        # Send ICS invites to external attendees on update too
        if attendees:
            result_attendees = result.get("attendees", [])
            organizer_domain = cal_id.split("@")[-1] if "@" in cal_id else ""
            external = [
                a["email"] for a in result_attendees
                if a.get("email") and a["email"].split("@")[-1] != organizer_domain
            ]
            if external:
                try:
                    _send_ics_invite(result, cal_id, external)
                    lines.append(f"ICS invite emailed to {len(external)} external attendee(s).")
                except Exception as e:
                    logger.warning("ICS invite sending failed on update: %s", e)

        return "\n".join(lines)
    except Exception as e:
        logger.error("Failed to update event %s: %s", event_id, e)
        return f"Couldn't update the event: {e}"


def _get_service_for_calendar(cal_id: str):
    """Get the right service for a given calendar ID.

    Matches by: exact email, domain, or by checking which service
    actually owns the calendar.
    """
    cal_domain = cal_id.split("@")[-1] if "@" in cal_id else ""
    all_services = _get_all_services()
    # Exact email match wins first — multiple accounts can share one domain
    # (e.g. alex.chen@acme-corp.example and assistant@acme-corp.example).
    for svc, acct_email in all_services:
        if cal_id == acct_email:
            return svc
    for svc, acct_email in all_services:
        acct_domain = acct_email.split("@")[-1] if "@" in acct_email else ""
        if cal_domain and cal_domain == acct_domain:
            return svc
    # Fallback: check which service actually has this calendar
    for svc, acct_email in all_services:
        try:
            cals = svc.calendarList().list().execute().get("items", [])
            if any(c["id"] == cal_id for c in cals):
                return svc
        except Exception:
            continue
    return _get_service()
