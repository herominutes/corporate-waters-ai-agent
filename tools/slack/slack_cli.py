"""CLI interface for Slack operations — called by the brain via Bash.

Usage:
    python -m goodman.slack_cli send <channel_or_user> <message>
    python -m goodman.slack_cli dm <user_name> <message>
    python -m goodman.slack_cli channels
    python -m goodman.slack_cli users
    python -m goodman.slack_cli search <query>
    python -m goodman.slack_cli read <channel> [limit]
    python -m goodman.slack_cli react <channel> <timestamp> <emoji>
    python -m goodman.slack_cli info <channel>
"""

import sys
from goodman import slack


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m goodman.slack_cli <command> [args...]")
        print("Commands: send, dm, channels, users, search, read, react, info")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd in ("send", "dm"):
        if len(sys.argv) < 4:
            print("Usage: python -m goodman.slack_cli send <channel_or_user> <message>")
            sys.exit(1)
        target = sys.argv[2]
        message = " ".join(sys.argv[3:])
        print(slack.send_message(target, message))

    elif cmd == "channels":
        print(slack.list_channels())

    elif cmd == "users":
        print(slack.list_users())

    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: python -m goodman.slack_cli search <query>")
            sys.exit(1)
        query = " ".join(sys.argv[2:])
        print(slack.search_messages(query))

    elif cmd == "read":
        if len(sys.argv) < 3:
            print("Usage: python -m goodman.slack_cli read <channel> [limit]")
            sys.exit(1)
        channel = sys.argv[2]
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 20
        print(slack.get_channel_messages(channel, limit))

    elif cmd == "react":
        if len(sys.argv) < 5:
            print("Usage: python -m goodman.slack_cli react <channel> <timestamp> <emoji>")
            sys.exit(1)
        print(slack.add_reaction(sys.argv[2], sys.argv[3], sys.argv[4]))

    elif cmd == "info":
        if len(sys.argv) < 3:
            print("Usage: python -m goodman.slack_cli info <channel>")
            sys.exit(1)
        print(slack.get_channel_info(sys.argv[2]))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
