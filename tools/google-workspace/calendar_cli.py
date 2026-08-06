"""CLI interface for Calendar operations — called by the brain via Bash.

Usage:
    python -m goodman.calendar_cli create <summary> <date> <start_time> <end_time> [--calendar work] [--attendee email] [--meet]
    python -m goodman.calendar_cli find <query> [--days N]
    python -m goodman.calendar_cli reschedule <query> <new_date> <new_start_time> [--calendar cal_id]
    python -m goodman.calendar_cli update <event_id> [--calendar cal_id] [--summary text] [--start YYYY-MM-DDTHH:MM] [--end YYYY-MM-DDTHH:MM]
    python -m goodman.calendar_cli delete <event_id> [--calendar cal_id]
    python -m goodman.calendar_cli availability <date> <email1> [email2] ... [--start 9] [--end 18]
    python -m goodman.calendar_cli today
    python -m goodman.calendar_cli upcoming [days]
    python -m goodman.calendar_cli date <YYYY-MM-DD>

Examples:
    python -m goodman.calendar_cli create "Meet & Greet with Viktor" 2026-02-18 14:00 14:30 --calendar work --meet
    python -m goodman.calendar_cli find "Anna"
    python -m goodman.calendar_cli reschedule "Anna" 2026-03-12 10:00
    python -m goodman.calendar_cli availability 2026-02-18 pavel.orlov@acme-corp.example fedor.galanin@acme-corp.example
"""

import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from goodman import calendar
from goodman.config import TIMEZONE


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m goodman.calendar_cli <command> [args...]")
        print("Commands: create, availability, today, upcoming, date")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "today":
        print(calendar.get_todays_events())

    elif cmd == "upcoming":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        print(calendar.get_upcoming_events(days))

    elif cmd == "date":
        if len(sys.argv) < 3:
            print("Usage: python -m goodman.calendar_cli date <YYYY-MM-DD>")
            sys.exit(1)
        print(calendar.get_events_for_date(sys.argv[2]))

    elif cmd == "availability":
        if len(sys.argv) < 4:
            print("Usage: python -m goodman.calendar_cli availability <YYYY-MM-DD> <email1> [email2] ... [--start 9] [--end 18]")
            sys.exit(1)
        date_str = sys.argv[2]
        emails = []
        start_hour = 9
        end_hour = 18
        i = 3
        while i < len(sys.argv):
            if sys.argv[i] == "--start" and i + 1 < len(sys.argv):
                start_hour = int(sys.argv[i + 1])
                i += 2
            elif sys.argv[i] == "--end" and i + 1 < len(sys.argv):
                end_hour = int(sys.argv[i + 1])
                i += 2
            else:
                emails.append(sys.argv[i])
                i += 1
        if not emails:
            print("Error: provide at least one email address")
            sys.exit(1)
        print(calendar.check_availability(emails, date_str, start_hour, end_hour))

    elif cmd == "create":
        if len(sys.argv) < 5:
            print("Usage: python -m goodman.calendar_cli create <summary> <date> <start_time> <end_time> [options]")
            print("Options: --calendar work, --attendee email@, --meet, --description text, --location text")
            sys.exit(1)

        summary = sys.argv[2]
        date_str = sys.argv[3]
        start_time_str = sys.argv[4]
        end_time_str = sys.argv[5] if len(sys.argv) > 5 and not sys.argv[5].startswith("--") else None

        # Parse optional flags
        cal_id = None
        attendees = []
        add_meet = False
        description = ""
        location = ""
        recurrence = None

        i = 5 if end_time_str else 4
        if end_time_str:
            i = 6
        while i < len(sys.argv):
            arg = sys.argv[i]
            if arg == "--calendar" and i + 1 < len(sys.argv):
                cal_val = sys.argv[i + 1]
                if cal_val == "work":
                    cal_id = "alex.chen@acme-corp.example"
                else:
                    cal_id = cal_val
                i += 2
            elif arg == "--attendee" and i + 1 < len(sys.argv):
                attendees.append(sys.argv[i + 1])
                i += 2
            elif arg == "--meet":
                add_meet = True
                i += 1
            elif arg == "--description" and i + 1 < len(sys.argv):
                description = sys.argv[i + 1]
                i += 2
            elif arg == "--location" and i + 1 < len(sys.argv):
                location = sys.argv[i + 1]
                i += 2
            elif arg == "--recurrence" and i + 1 < len(sys.argv):
                recurrence = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        # Build datetime strings
        tz = ZoneInfo(TIMEZONE)
        try:
            start_dt = datetime.strptime(f"{date_str} {start_time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            if end_time_str:
                end_dt = datetime.strptime(f"{date_str} {end_time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            else:
                end_dt = start_dt + timedelta(minutes=30)
        except ValueError as e:
            print(f"Date/time parse error: {e}")
            print("Use format: YYYY-MM-DD HH:MM (e.g. 2026-02-18 14:00)")
            sys.exit(1)

        result = calendar.create_event(
            summary=summary,
            start_time=start_dt.isoformat(),
            end_time=end_dt.isoformat(),
            calendar_id=cal_id,
            description=description,
            attendees=attendees if attendees else None,
            location=location,
            add_meet=add_meet,
            recurrence=recurrence,
        )
        print(result)

    elif cmd == "find":
        if len(sys.argv) < 3:
            print("Usage: python -m goodman.calendar_cli find <query> [--days N] [--days-back N]")
            sys.exit(1)
        query = sys.argv[2]
        days = 30
        days_back = 90  # Default: search 90 days into the past (for contact lookup)
        i = 3
        while i < len(sys.argv):
            if sys.argv[i] == "--days" and i + 1 < len(sys.argv):
                days = int(sys.argv[i + 1])
                i += 2
            elif sys.argv[i] == "--days-back" and i + 1 < len(sys.argv):
                days_back = int(sys.argv[i + 1])
                i += 2
            else:
                i += 1
        matches = calendar.find_events(query, days_ahead=days, days_back=days_back)
        if not matches:
            print(f"No events found matching '{query}' (searched {days_back} days back, {days} days ahead).")
        else:
            for m in matches:
                dur = f" ({m['duration_minutes']}min)" if m.get('duration_minutes') else ""
                att = f"  Attendees: {', '.join(m['attendees'])}" if m.get('attendees') else ""
                desc = f"  Description: {m['description']}" if m.get('description') else ""
                loc = f"  Location: {m['location']}" if m.get('location') else ""
                rec = f"  Recurrence: {m['recurrence']}" if m.get('recurrence') else ""
                print(f"- {m['summary']}{dur}")
                print(f"  Start: {m['start']}")
                print(f"  End: {m['end']}")
                print(f"  ID: {m['id']}")
                print(f"  Calendar: {m['calendar_id']}")
                if att:
                    print(att)
                if desc:
                    print(desc)
                if loc:
                    print(loc)
                if rec:
                    print(rec)
                print()

    elif cmd == "reschedule":
        if len(sys.argv) < 5:
            print("Usage: python -m goodman.calendar_cli reschedule <query> <new_date> <new_start_time> [--calendar cal_id]")
            print("Finds the event by query, preserves its duration/summary/description/attendees, moves to new date/time.")
            sys.exit(1)
        query = sys.argv[2]
        new_date = sys.argv[3]
        new_start = sys.argv[4]
        cal_override = None
        i = 5
        while i < len(sys.argv):
            if sys.argv[i] == "--calendar" and i + 1 < len(sys.argv):
                cal_val = sys.argv[i + 1]
                cal_override = "alex.chen@acme-corp.example" if cal_val == "work" else cal_val
                i += 2
            else:
                i += 1

        # Find the original event
        matches = calendar.find_events(query, days_ahead=60)
        if not matches:
            print(f"No events found matching '{query}'. Cannot reschedule.")
            sys.exit(1)
        if len(matches) > 1:
            print(f"Multiple events match '{query}'. Please be more specific or use 'update' with the event ID:")
            for m in matches:
                dur = f" ({m['duration_minutes']}min)" if m.get('duration_minutes') else ""
                print(f"  - {m['summary']}{dur} | {m['start']} | ID: {m['id']} | Calendar: {m['calendar_id']}")
            sys.exit(1)

        original = matches[0]
        duration_min = original.get("duration_minutes") or 30
        cal_id = cal_override or original["calendar_id"]

        # Build new start/end preserving original duration
        tz = ZoneInfo(TIMEZONE)
        try:
            new_start_dt = datetime.strptime(f"{new_date} {new_start}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            new_end_dt = new_start_dt + timedelta(minutes=duration_min)
        except ValueError as e:
            print(f"Date/time parse error: {e}")
            sys.exit(1)

        # Update the existing event in place
        result = calendar.update_event(
            event_id=original["id"],
            calendar_id=cal_id,
            start_time=new_start_dt.isoformat(),
            end_time=new_end_dt.isoformat(),
        )
        print(f"Rescheduled: {original['summary']} ({duration_min}min)")
        print(f"  From: {original['start']}")
        print(f"  To:   {new_start_dt.isoformat()} — {new_end_dt.isoformat()}")
        if original.get('description'):
            print(f"  Description: preserved")
        if original.get('attendees'):
            print(f"  Attendees: preserved ({', '.join(original['attendees'])})")
        print(result)

    elif cmd == "update":
        if len(sys.argv) < 3:
            print("Usage: python -m goodman.calendar_cli update <event_id> [--calendar cal_id] [--summary text] [--description text] [--start ISO] [--end ISO] [--attendee email]")
            sys.exit(1)
        event_id = sys.argv[2]
        cal_id = None
        summary = None
        description = None
        start_time = None
        end_time = None
        recurrence = None
        attendees = None
        i = 3
        while i < len(sys.argv):
            arg = sys.argv[i]
            if arg == "--calendar" and i + 1 < len(sys.argv):
                cal_val = sys.argv[i + 1]
                cal_id = "alex.chen@acme-corp.example" if cal_val == "work" else cal_val
                i += 2
            elif arg == "--summary" and i + 1 < len(sys.argv):
                summary = sys.argv[i + 1]
                i += 2
            elif arg == "--description" and i + 1 < len(sys.argv):
                description = sys.argv[i + 1]
                i += 2
            elif arg == "--start" and i + 1 < len(sys.argv):
                start_time = sys.argv[i + 1]
                i += 2
            elif arg == "--end" and i + 1 < len(sys.argv):
                end_time = sys.argv[i + 1]
                i += 2
            elif arg == "--recurrence" and i + 1 < len(sys.argv):
                recurrence = sys.argv[i + 1]
                i += 2
            elif arg == "--attendee" and i + 1 < len(sys.argv):
                if attendees is None:
                    attendees = []
                attendees.append(sys.argv[i + 1])
                i += 2
            else:
                i += 1
        result = calendar.update_event(
            event_id=event_id,
            calendar_id=cal_id,
            summary=summary,
            description=description,
            start_time=start_time,
            end_time=end_time,
            recurrence=recurrence,
            attendees=attendees,
        )
        print(result)

    elif cmd == "delete":
        if len(sys.argv) < 3:
            print("Usage: python -m goodman.calendar_cli delete <event_id> [--calendar cal_id]")
            sys.exit(1)
        event_id = sys.argv[2]
        cal_id = None
        if "--calendar" in sys.argv:
            idx = sys.argv.index("--calendar")
            if idx + 1 < len(sys.argv):
                cal_val = sys.argv[idx + 1]
                cal_id = "alex.chen@acme-corp.example" if cal_val == "work" else cal_val
        result = calendar.delete_event(event_id, cal_id)
        print(result)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
