"""Gmail integration for The Goodman Protocol via IMAP/SMTP.

Supports two auth methods:
  - App passwords (legacy, from gmail-accounts.json)
  - OAuth XOAUTH2 (from credentials/oauth-tokens/*.json)
"""

import base64
import imaplib
import json
import smtplib
import email
import email.utils
import logging
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from datetime import datetime, timedelta, timezone
from pathlib import Path
from goodman.config import GMAIL_ACCOUNTS

logger = logging.getLogger(__name__)

IMAP_SERVER = "imap.gmail.com"
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

CREDENTIALS_DIR = Path(__file__).resolve().parent.parent.parent / "credentials"
OAUTH_TOKENS_DIR = CREDENTIALS_DIR / "oauth-tokens"
OAUTH_TOKENS_PATH = CREDENTIALS_DIR / "user-tokens.json"

# Accounts only the owner may access — never exposed to non-owner Slack callers.
_OWNER_ONLY_ACCOUNTS = {"alex.chen@gmail.example"}


def _load_oauth_accounts() -> list[dict]:
    """Load OAuth accounts that have mail.google.com scope."""
    accounts = []
    token_files = []
    if OAUTH_TOKENS_DIR.exists():
        token_files.extend(sorted(OAUTH_TOKENS_DIR.glob("*.json")))
    if not token_files and OAUTH_TOKENS_PATH.exists():
        token_files.append(OAUTH_TOKENS_PATH)

    for tf in token_files:
        try:
            data = json.loads(tf.read_text())
            scopes = data.get("scopes", [])
            if "https://mail.google.com/" not in scopes:
                continue
            accounts.append({
                "email": data["email"],
                "token_file": tf,
                "auth": "oauth",
                "label": data["email"].split("@")[0],
            })
        except Exception as e:
            logger.debug("Skipping token file %s: %s", tf.name, e)
    return accounts


def _get_oauth_access_token(token_file: Path) -> str | None:
    """Get a fresh access token from a token file, refreshing if needed."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        data = json.loads(token_file.read_text())
        # Stored expiry may be a numeric Unix timestamp (int/float) OR an ISO-8601
        # string (google creds.to_json format) — handle both so the refresh never
        # crashes with "'str' object cannot be interpreted as an integer" (the bug that
        # broke PA + work gmail/IMAP, 2026-06-26). Line 86 re-normalizes to int on write.
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
            scopes=data.get("scopes"),
            expiry=expiry_dt,
        )
        if creds.expired or not creds.token:
            creds.refresh(Request())
            data["access_token"] = creds.token
            data["expiry"] = int(datetime.now().timestamp()) + 3500
            token_file.write_text(json.dumps(data, indent=2))
        return creds.token
    except Exception as e:
        logger.error("OAuth token refresh failed for %s: %s", token_file.name, e)
        return None


def _get_all_accounts() -> list[dict]:
    """Get combined list of OAuth + app-password accounts, deduped by email."""
    seen = set()
    accounts = []

    # OAuth accounts first (preferred)
    for acct in _load_oauth_accounts():
        accounts.append(acct)
        seen.add(acct["email"])

    # App password accounts (skip if already have OAuth for same email)
    for acct in GMAIL_ACCOUNTS:
        if acct["email"] not in seen:
            accounts.append({
                "email": acct["email"],
                "app_password": acct["app_password"],
                "auth": "app_password",
                "label": acct.get("label", acct["email"].split("@")[0]),
            })
            seen.add(acct["email"])

    # SECURITY: the personal account is owner-only. Non-owner Slack callers have
    # SLACK_CALLER_ROLE set (by brain.py); strip owner-only accounts for them so
    # no read/search/send path can ever touch the personal inbox. Enforced here,
    # at the single resolver every gmail function funnels through — not in a prompt.
    if os.getenv("SLACK_CALLER_ROLE"):
        accounts = [a for a in accounts if a["email"] not in _OWNER_ONLY_ACCOUNTS]

    # Delegated mailboxes (e.g. a recruiter's own inbox) are visible ONLY to the mapped
    # caller, and only for their own send-as. Hidden from everyone else — including the owner.
    from goodman import config as _cfg
    _send_as = (os.getenv("SLACK_SEND_AS") or "").lower()
    accounts = [a for a in accounts
                if a["email"].lower() not in _cfg.DELEGATED_SEND_ACCOUNTS
                or a["email"].lower() == _send_as]

    return accounts


def _imap_login(imap: imaplib.IMAP4_SSL, account: dict) -> bool:
    """Login to IMAP using either XOAUTH2 or app password."""
    addr = account["email"]
    if account["auth"] == "oauth":
        token = _get_oauth_access_token(account["token_file"])
        if not token:
            return False
        auth_string = f"user={addr}\x01auth=Bearer {token}\x01\x01"
        imap.authenticate("XOAUTH2", lambda x: auth_string.encode())
        return True
    else:
        imap.login(addr, account["app_password"])
        return True


def _smtp_login(server: smtplib.SMTP, account: dict) -> bool:
    """Login to SMTP using either XOAUTH2 or app password."""
    addr = account["email"]
    if account["auth"] == "oauth":
        token = _get_oauth_access_token(account["token_file"])
        if not token:
            return False
        auth_string = f"user={addr}\x01auth=Bearer {token}\x01\x01"
        encoded = base64.b64encode(auth_string.encode()).decode()
        code, msg = server.docmd("AUTH", f"XOAUTH2 {encoded}")
        if code not in (235, 334):
            logger.error("SMTP XOAUTH2 failed: %d %s", code, msg)
            return False
        return True
    else:
        server.login(addr, account["app_password"])
        return True


def _send_via_api(account: dict, msg: MIMEMultipart) -> str:
    """Send email via Gmail API (HTTPS) instead of SMTP.

    Falls back to SMTP for app-password accounts.
    Many VPS providers block outbound SMTP ports (25/465/587),
    so OAuth accounts use the Gmail API over port 443 instead.
    """
    import httpx as _httpx

    # Audit log — every outbound email is logged with full details
    logger.warning(
        "SENDING EMAIL: From=%s To=%s Subject=%s",
        msg.get("From", "?"), msg.get("To", "?"), msg.get("Subject", "?"),
    )

    if account["auth"] != "oauth":
        # App-password accounts can only use SMTP
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            _smtp_login(server, account)
            server.send_message(msg)
        return "sent"

    token = _get_oauth_access_token(account["token_file"])
    if not token:
        raise RuntimeError("Failed to get OAuth access token")

    # Gmail API expects base64url-encoded RFC 2822 message
    raw_msg = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    resp = _httpx.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {token}"},
        json={"raw": raw_msg},
        timeout=30,
    )
    resp.raise_for_status()
    return "sent"


def _decode_header_value(value: str) -> str:
    """Decode an email header value."""
    if not value:
        return ""
    decoded_parts = decode_header(value)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return " ".join(result)


def _get_body(msg: email.message.Message) -> str:
    """Extract plain text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                try:
                    return part.get_payload(decode=True).decode("utf-8", errors="replace")
                except Exception:
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return part.get_payload(decode=True).decode("utf-8", errors="replace")
                except Exception:
                    continue
    else:
        try:
            return msg.get_payload(decode=True).decode("utf-8", errors="replace")
        except Exception:
            return "(could not decode body)"
    return "(no readable body)"


def _fetch_headers(imap, msg_id) -> dict:
    """Fetch only headers for a message (faster than full RFC822)."""
    status, msg_data = imap.fetch(msg_id, "(RFC822.HEADER)")
    if status != "OK":
        return {}
    msg = email.message_from_bytes(msg_data[0][1])
    return {
        "subject": _decode_header_value(msg.get("Subject", "(no subject)")),
        "sender": _decode_header_value(msg.get("From", "Unknown")),
        "date": msg.get("Date", ""),
    }


def get_unread_emails(limit: int = 50) -> str:
    """Get unread emails from all configured accounts."""
    all_emails = []

    for account in _get_all_accounts():
        addr = account["email"]
        label = account.get("label", addr.split("@")[0])

        try:
            imap = imaplib.IMAP4_SSL(IMAP_SERVER)
            _imap_login(imap, account)
            imap.select("INBOX", readonly=True)

            status, data = imap.search(None, "UNSEEN")
            if status != "OK" or not data[0]:
                all_emails.append(f"\n[{label}] No unread emails.")
                imap.logout()
                continue

            all_msg_ids = data[0].split()
            total_unread = len(all_msg_ids)
            msg_ids = all_msg_ids[-limit:]  # Last N unread

            # Group by subject pattern to avoid listing 30 identical notifications
            subject_groups = {}
            individual = []

            for msg_id in msg_ids:
                headers = _fetch_headers(imap, msg_id)
                if not headers:
                    continue
                subject = headers["subject"]

                # Check if this is a notification-type email (repeated subject pattern)
                # Normalize: strip specifics to find patterns
                matched = False
                for key in list(subject_groups.keys()):
                    # If subjects share the first 30 chars, group them
                    if len(key) > 20 and subject[:20] == key[:20]:
                        subject_groups[key]["count"] += 1
                        subject_groups[key]["latest"] = headers
                        matched = True
                        break
                if not matched:
                    subject_groups[subject] = {"count": 1, "latest": headers, "first": headers}

            # Format output
            account_emails = []
            for subject, group in subject_groups.items():
                h = group["latest"]
                if group["count"] > 1:
                    account_emails.append(
                        f"  [{group['count']}x] Subject: {subject}\n"
                        f"  From: {h['sender']}\n"
                        f"  Latest: {h['date']}"
                    )
                else:
                    account_emails.append(
                        f"  From: {h['sender']}\n"
                        f"  Subject: {subject}\n"
                        f"  Date: {h['date']}"
                    )

            all_emails.append(
                f"\n[{label}] {total_unread} unread email(s):\n" +
                "\n---\n".join(account_emails)
            )
            imap.logout()

        except Exception as e:
            logger.error("IMAP error for %s: %s", addr, e)
            all_emails.append(f"\n[{label}] Error accessing inbox: {e}")

    if not all_emails:
        return "No email accounts configured."

    return "\n".join(all_emails)


def get_recent_emails(hours: int = 24, limit: int = 50) -> str:
    """Get emails from the last N hours across all accounts."""
    all_emails = []
    since_date = (datetime.now() - timedelta(hours=hours)).strftime("%d-%b-%Y")

    for account in _get_all_accounts():
        addr = account["email"]
        label = account.get("label", addr.split("@")[0])

        try:
            imap = imaplib.IMAP4_SSL(IMAP_SERVER)
            _imap_login(imap, account)
            imap.select("INBOX", readonly=True)

            status, data = imap.search(None, f'(SINCE "{since_date}")')
            if status != "OK" or not data[0]:
                all_emails.append(f"\n[{label}] No recent emails.")
                imap.logout()
                continue

            msg_ids = data[0].split()[-limit:]
            account_emails = []

            for msg_id in msg_ids:
                headers = _fetch_headers(imap, msg_id)
                if not headers:
                    continue

                # Check read status
                status2, flags_data = imap.fetch(msg_id, "(FLAGS)")
                flags = flags_data[0].decode() if flags_data[0] else ""
                is_read = "\\Seen" in flags
                status_marker = "" if is_read else "[UNREAD] "

                account_emails.append(
                    f"  {status_marker}From: {headers['sender']}\n"
                    f"  Subject: {headers['subject']}\n"
                    f"  Date: {headers['date']}"
                )

            total = len(msg_ids)
            all_emails.append(
                f"\n[{label}] {total} email(s) in last {hours}h:\n" +
                "\n---\n".join(account_emails)
            )
            imap.logout()

        except Exception as e:
            logger.error("IMAP error for %s: %s", addr, e)
            all_emails.append(f"\n[{label}] Error: {e}")

    return "\n".join(all_emails) if all_emails else "No email accounts configured."


def search_emails(query: str, limit: int = 50) -> str:
    """Search emails by subject or sender across all accounts."""
    results = []

    for account in _get_all_accounts():
        addr = account["email"]
        label = account.get("label", addr.split("@")[0])

        try:
            imap = imaplib.IMAP4_SSL(IMAP_SERVER)
            _imap_login(imap, account)
            imap.select("INBOX", readonly=True)

            # Primary: Gmail X-GM-RAW with UTF-8 literal — full-text search
            # (body included, Cyrillic works) across All Mail incl. Sent.
            # imaplib sends the literal as the last SEARCH argument; plain
            # search() returns sequence numbers, so downstream fetch is unchanged.
            status, data = "NO", [b""]
            try:
                st, _ = imap.select('"[Gmail]/All Mail"', readonly=True)
                if st != "OK":
                    imap.select("INBOX", readonly=True)
                imap.literal = query.encode("utf-8")
                status, data = imap.search("UTF-8", "X-GM-RAW")
            except Exception:
                status = "NO"

            if status != "OK":
                # Legacy fallback: ASCII subject/from search on INBOX
                imap.select("INBOX", readonly=True)
                safe = query.encode("ascii", "ignore").decode().strip() or query
                words = safe.split()
                if len(words) <= 1:
                    w = words[0] if words else safe
                    criteria = f'(OR SUBJECT "{w}" FROM "{w}")'
                else:
                    parts = [f'SUBJECT "{w}"' for w in words] + [f'FROM "{safe}"']
                    criteria = parts[0]
                    for p in parts[1:]:
                        criteria = f'(OR {criteria} {p})'
                status, data = imap.search(None, criteria)
            if status != "OK" or not data[0]:
                results.append(f"\n[{label}] No results for '{query}'.")
                imap.logout()
                continue

            all_msg_ids = data[0].split()
            total_matches = len(all_msg_ids)
            msg_ids = all_msg_ids[-limit:]

            # For the most recent few, fetch full body so details are visible
            recent_with_body = set(all_msg_ids[-5:])

            # Group repeated subjects
            subject_groups = {}
            for msg_id in msg_ids:
                headers = _fetch_headers(imap, msg_id)
                if not headers:
                    continue
                subject = headers["subject"]

                # Fetch body for most recent results
                body_snippet = ""
                if msg_id in recent_with_body:
                    try:
                        status2, msg_data2 = imap.fetch(msg_id, "(RFC822)")
                        if status2 == "OK":
                            msg_obj = email.message_from_bytes(msg_data2[0][1])
                            body = _get_body(msg_obj)
                            body_snippet = body[:500].strip() if body else ""
                    except Exception:
                        pass
                headers["body_snippet"] = body_snippet

                matched = False
                for key in list(subject_groups.keys()):
                    if len(key) > 20 and subject[:20] == key[:20]:
                        subject_groups[key]["count"] += 1
                        subject_groups[key]["latest"] = headers
                        matched = True
                        break
                if not matched:
                    subject_groups[subject] = {"count": 1, "latest": headers}

            account_results = []
            for subject, group in subject_groups.items():
                h = group["latest"]
                entry = ""
                if group["count"] > 1:
                    entry = (
                        f"  [{group['count']}x] Subject: {subject}\n"
                        f"  From: {h['sender']}\n"
                        f"  Latest: {h['date']}"
                    )
                else:
                    entry = (
                        f"  From: {h['sender']}\n"
                        f"  Subject: {subject}\n"
                        f"  Date: {h['date']}"
                    )
                if h.get("body_snippet"):
                    entry += f"\n  Preview: {h['body_snippet']}"
                account_results.append(entry)

            results.append(
                f"\n[{label}] {total_matches} total match(es):\n" +
                "\n---\n".join(account_results)
            )
            imap.logout()

        except Exception as e:
            logger.error("IMAP search error for %s: %s", addr, e)
            results.append(f"\n[{label}] Search error: {e}")

    return "\n".join(results) if results else "No email accounts configured."


def read_email(query: str, index: int = 0) -> str:
    """Read a specific email's full body, found by search query."""
    for account in _get_all_accounts():
        addr = account["email"]
        label = account.get("label", addr.split("@")[0])

        try:
            imap = imaplib.IMAP4_SSL(IMAP_SERVER)
            _imap_login(imap, account)
            imap.select("INBOX", readonly=True)

            words = query.split()
            if len(words) == 1:
                criteria = f'(OR SUBJECT "{words[0]}" FROM "{words[0]}")'
            else:
                parts = [f'SUBJECT "{w}"' for w in words] + [f'FROM "{query}"']
                criteria = parts[0]
                for p in parts[1:]:
                    criteria = f'(OR {criteria} {p})'

            status, data = imap.search(None, criteria)
            if status != "OK" or not data[0]:
                imap.logout()
                continue

            msg_ids = data[0].split()
            # index 0 = most recent (last in IMAP order)
            target_idx = -(index + 1)
            if abs(target_idx) > len(msg_ids):
                imap.logout()
                continue

            msg_id = msg_ids[target_idx]
            status2, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status2 != "OK":
                imap.logout()
                continue

            msg_obj = email.message_from_bytes(msg_data[0][1])
            subject_str = _decode_header_value(msg_obj.get("Subject", "(no subject)"))
            sender = _decode_header_value(msg_obj.get("From", "Unknown"))
            to_addr = _decode_header_value(msg_obj.get("To", ""))
            date_str = msg_obj.get("Date", "")
            message_id = msg_obj.get("Message-ID", "")
            body = _get_body(msg_obj)

            # Truncate very long emails
            if len(body) > 4000:
                body = body[:4000] + "\n\n[... body truncated ...]"

            imap.logout()

            return (
                f"[{label}] Full email:\n"
                f"From: {sender}\n"
                f"To: {to_addr}\n"
                f"Subject: {subject_str}\n"
                f"Date: {date_str}\n"
                f"Message-ID: {message_id}\n"
                f"\n{body}"
            )

        except Exception as e:
            logger.error("IMAP read error for %s: %s", addr, e)
            continue

    return f"Could not find an email matching '{query}'."


def reply_to_email(query: str, reply_body: str, index: int = 0,
                   from_account: str | None = None) -> str:
    """Reply to an email found by search query."""
    for account in _get_all_accounts():
        addr = account["email"]

        if from_account and from_account not in addr:
            continue

        try:
            imap = imaplib.IMAP4_SSL(IMAP_SERVER)
            _imap_login(imap, account)
            imap.select("INBOX", readonly=True)

            words = query.split()
            if len(words) == 1:
                criteria = f'(OR SUBJECT "{words[0]}" FROM "{words[0]}")'
            else:
                parts = [f'SUBJECT "{w}"' for w in words] + [f'FROM "{query}"']
                criteria = parts[0]
                for p in parts[1:]:
                    criteria = f'(OR {criteria} {p})'

            status, data = imap.search(None, criteria)
            if status != "OK" or not data[0]:
                imap.logout()
                continue

            msg_ids = data[0].split()
            target_idx = -(index + 1)
            if abs(target_idx) > len(msg_ids):
                imap.logout()
                continue

            msg_id = msg_ids[target_idx]
            status2, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status2 != "OK":
                imap.logout()
                continue

            original = email.message_from_bytes(msg_data[0][1])
            orig_subject = _decode_header_value(original.get("Subject", ""))
            orig_from = _decode_header_value(original.get("From", ""))
            orig_message_id = original.get("Message-ID", "")
            orig_references = original.get("References", "")
            orig_date = original.get("Date", "")
            orig_body = _get_body(original)

            imap.logout()

            # Extract reply-to address (prefer Reply-To header, fallback to From)
            reply_to = _decode_header_value(original.get("Reply-To", "")) or orig_from
            # Extract just the email address from "Name <email>" format
            parsed_addr = email.utils.parseaddr(reply_to)
            to_addr = parsed_addr[1] if parsed_addr[1] else reply_to

            # Build reply subject
            reply_subject = orig_subject
            if not reply_subject.lower().startswith("re:"):
                reply_subject = f"Re: {reply_subject}"

            # Build threading headers
            references = orig_references
            if orig_message_id:
                references = f"{references} {orig_message_id}".strip()

            # Build the reply with quoted original
            quoted = "\n".join(f"> {line}" for line in orig_body[:2000].split("\n"))
            full_body = f"{reply_body}\n\nOn {orig_date}, {orig_from} wrote:\n{quoted}"

            # Send via Gmail API (bypasses SMTP port blocks)
            msg = MIMEMultipart()
            msg["From"] = addr
            msg["To"] = to_addr
            msg["Subject"] = reply_subject
            if orig_message_id:
                msg["In-Reply-To"] = orig_message_id
            if references:
                msg["References"] = references
            msg.attach(MIMEText(full_body, "plain"))

            _send_via_api(account, msg)

            return f"Reply sent to {to_addr} (Re: {orig_subject}) from {addr}"

        except Exception as e:
            logger.error("Reply error for %s: %s", addr, e)
            return f"Failed to reply: {e}"

    return f"Could not find email matching '{query}' to reply to."


def reply_by_message_id(message_id: str, reply_body: str,
                        from_account: str | None = None) -> str:
    """Reply to a specific email identified by its Message-ID header.

    Unlike reply_to_email (which re-searches), this targets an exact email.
    Prevents TOCTOU race where new emails shift search results between
    preview and send.
    """
    if not message_id:
        return "No Message-ID provided."

    for account in _get_all_accounts():
        addr = account["email"]
        if from_account and from_account not in addr:
            continue

        try:
            imap_conn = imaplib.IMAP4_SSL(IMAP_SERVER)
            _imap_login(imap_conn, account)
            imap_conn.select("INBOX", readonly=True)

            # Search by Message-ID header
            status, data = imap_conn.search(None, f'HEADER Message-ID "{message_id}"')
            if status != "OK" or not data[0]:
                imap_conn.logout()
                continue

            msg_ids = data[0].split()
            status2, msg_data = imap_conn.fetch(msg_ids[-1], "(RFC822)")
            if status2 != "OK":
                imap_conn.logout()
                continue

            original = email.message_from_bytes(msg_data[0][1])
            orig_subject = _decode_header_value(original.get("Subject", ""))
            orig_from = _decode_header_value(original.get("From", ""))
            orig_message_id = original.get("Message-ID", "")
            orig_references = original.get("References", "")
            orig_date = original.get("Date", "")
            orig_body = _get_body(original)

            imap_conn.logout()

            # Extract reply-to address
            reply_to = _decode_header_value(original.get("Reply-To", "")) or orig_from
            parsed_addr = email.utils.parseaddr(reply_to)
            to_addr = parsed_addr[1] if parsed_addr[1] else reply_to

            # Build reply
            reply_subject = orig_subject
            if not reply_subject.lower().startswith("re:"):
                reply_subject = f"Re: {reply_subject}"

            references = orig_references
            if orig_message_id:
                references = f"{references} {orig_message_id}".strip()

            quoted = "\n".join(f"> {line}" for line in orig_body[:2000].split("\n"))
            full_body = f"{reply_body}\n\nOn {orig_date}, {orig_from} wrote:\n{quoted}"

            msg = MIMEMultipart()
            msg["From"] = addr
            msg["To"] = to_addr
            msg["Subject"] = reply_subject
            if orig_message_id:
                msg["In-Reply-To"] = orig_message_id
            if references:
                msg["References"] = references
            msg.attach(MIMEText(full_body, "plain"))

            _send_via_api(account, msg)

            return f"Reply sent to {to_addr} (Re: {orig_subject}) from {addr}"

        except Exception as e:
            logger.error("Reply-by-ID error for %s: %s", addr, e)
            return f"Failed to reply: {e}"

    return f"Could not find email with Message-ID '{message_id}'."


def send_email(to: str, subject: str, body: str, from_account: str | None = None) -> str:
    """Send an email from one of the configured accounts."""
    import os, re
    # SCOPED SEND-AS: a delegation caller (e.g. a recruiter) sends from THEIR OWN mailbox.
    # Force that account and skip the non-owner gate below — external recipients (candidates)
    # are fine because it's their own inbox, not an impersonation of Alex.
    send_as = os.getenv("SLACK_SEND_AS")
    if send_as:
        from_account = send_as
    # SECURITY GATE: a non-owner Slack caller (SLACK_CALLER_ROLE set) may only email
    # allowlisted domains — default the work domain — so Saul can't be used to email
    # arbitrary external recipients from Alex's work identity. Tune via env
    # SLACK_CALLER_EMAIL_DOMAINS (comma-separated). Owner/internal is unrestricted.
    if os.getenv("SLACK_CALLER_ROLE") and not send_as:
        from goodman import config as _cfg
        if not _cfg.caller_has(_cfg.CAP_EMAIL):
            return "You're not authorized to send email on Alex's behalf."
        allowed = [d.strip().lower() for d in
                   os.getenv("SLACK_CALLER_EMAIL_DOMAINS", "acme-corp.example").split(",") if d.strip()]
        recips = re.findall(r'[\w.+-]+@([\w.-]+)', to or "")
        bad = [d for d in recips
               if not any(d.lower() == a or d.lower().endswith("." + a) for a in allowed)]
        if not recips or bad:
            logger.warning("EMAIL GATE: non-owner blocked emailing %s (allowed=%s)", to, allowed)
            return ("I can only send email to approved domains (" + ", ".join(allowed) +
                    ") on your behalf — I won't email " +
                    (", ".join(bad) if bad else "an unspecified recipient") + ".")
    # CONTACT GATE (wrong-recipient protection, Pavlov→Krasnoperov class):
    # every recipient must be in the canonical registry (data/notebooks/contacts.md)
    # or be one of the boss's own accounts. Skipped for SLACK_SEND_AS delegation
    # (recruiter sending from their own mailbox to external candidates).
    if not send_as and os.getenv("GOODMAN_ALLOW_UNLISTED") != "1":
        from goodman import contacts as _contacts
        unknown = _contacts.unknown_recipients(to)
        if unknown:
            logger.warning("CONTACT GATE: blocked email to unlisted %s", unknown)
            return _contacts.gate_message(unknown)
    all_accounts = _get_all_accounts()
    account = None
    if from_account:
        for a in all_accounts:
            if from_account in a["email"]:
                account = a
                break
    # SCOPED SEND-AS: never fall back to another mailbox. If the delegated account isn't
    # resolved (e.g. token not installed), REFUSE — don't silently send from PA/work.
    if send_as:
        if not account or send_as.lower() not in account["email"].lower():
            return (f"Can't send as {send_as} — that mailbox isn't set up for me "
                    "(no valid credentials). Nothing sent.")
    elif not account:
        account = all_accounts[0] if all_accounts else None

    if not account:
        return "No email accounts configured."

    try:
        msg = MIMEMultipart()
        msg["From"] = account["email"]
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        _send_via_api(account, msg)

        return f"Email sent to {to} from {account['email']}"

    except Exception as e:
        logger.error("Send email error: %s", e)
        return f"Failed to send email: {e}"
