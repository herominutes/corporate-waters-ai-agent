"""CLI interface for Gmail operations — called by the brain via Bash.

Usage:
    python -m goodman.gmail_cli unread [limit]
    python -m goodman.gmail_cli recent [hours] [limit]
    python -m goodman.gmail_cli search <query> [limit]
    python -m goodman.gmail_cli read <query> [index]
    python -m goodman.gmail_cli send <to> <subject> <body> [--from account]
    python -m goodman.gmail_cli reply <query> <body> [--index N] [--from account]
    python -m goodman.gmail_cli reply-id <message_id> <body> [--from account]

Examples:
    python -m goodman.gmail_cli unread 10
    python -m goodman.gmail_cli search "from:john invoice" 5
    python -m goodman.gmail_cli read "substack subscriber" 0
    python -m goodman.gmail_cli send "john@example.com" "Meeting follow-up" "Hey John, ..."
    python -m goodman.gmail_cli reply "invoice from John" "Thanks, received!"
    python -m goodman.gmail_cli reply-id "<msg-id@gmail.com>" "Thanks!"
"""

import sys
from goodman import gmail


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m goodman.gmail_cli <command> [args...]")
        print("Commands: unread, recent, search, read, send, reply, reply-id")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "unread":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 50
        print(gmail.get_unread_emails(limit))

    elif cmd == "recent":
        hours = int(sys.argv[2]) if len(sys.argv) > 2 else 24
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 50
        print(gmail.get_recent_emails(hours, limit))

    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: python -m goodman.gmail_cli search <query> [limit]")
            sys.exit(1)
        query = sys.argv[2]
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 50
        print(gmail.search_emails(query, limit))

    elif cmd == "read":
        if len(sys.argv) < 3:
            print("Usage: python -m goodman.gmail_cli read <query> [index]")
            sys.exit(1)
        query = sys.argv[2]
        index = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        print(gmail.read_email(query, index))

    elif cmd == "send":
        if len(sys.argv) < 5:
            print("Usage: python -m goodman.gmail_cli send <to> <subject> <body> [--from account]")
            sys.exit(1)
        to = sys.argv[2]
        subject = sys.argv[3]
        body = sys.argv[4]
        from_account = None
        if "--from" in sys.argv:
            idx = sys.argv.index("--from")
            if idx + 1 < len(sys.argv):
                from_account = sys.argv[idx + 1]
        print(gmail.send_email(to, subject, body, from_account))

    elif cmd == "reply":
        if len(sys.argv) < 4:
            print("Usage: python -m goodman.gmail_cli reply <query> <body> [--index N] [--from account]")
            sys.exit(1)
        query = sys.argv[2]
        body = sys.argv[3]
        index = 0
        from_account = None
        i = 4
        while i < len(sys.argv):
            if sys.argv[i] == "--index" and i + 1 < len(sys.argv):
                index = int(sys.argv[i + 1])
                i += 2
            elif sys.argv[i] == "--from" and i + 1 < len(sys.argv):
                from_account = sys.argv[i + 1]
                i += 2
            else:
                i += 1
        print(gmail.reply_to_email(query, body, index, from_account))

    elif cmd == "reply-id":
        if len(sys.argv) < 4:
            print("Usage: python -m goodman.gmail_cli reply-id <message_id> <body> [--from account]")
            sys.exit(1)
        message_id = sys.argv[2]
        body = sys.argv[3]
        from_account = None
        if "--from" in sys.argv:
            idx = sys.argv.index("--from")
            if idx + 1 < len(sys.argv):
                from_account = sys.argv[idx + 1]
        print(gmail.reply_by_message_id(message_id, body, from_account))

    else:
        print(f"Unknown command: {cmd}")
        print("Commands: unread, recent, search, read, send, reply, reply-id")
        sys.exit(1)


if __name__ == "__main__":
    main()
