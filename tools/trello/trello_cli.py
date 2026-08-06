"""CLI interface for Trello operations — called by the brain via Bash.

Usage:
    python -m goodman.trello_cli create <list_name> <card_title> [--desc text] [--priority color] [--due YYYY-MM-DD]
    python -m goodman.trello_cli list [list_name]
    python -m goodman.trello_cli move <card_name> <to_list>
    python -m goodman.trello_cli board
    python -m goodman.trello_cli lists
    python -m goodman.trello_cli labels
"""

import sys
from goodman import trello


def _parse_flags(args: list[str]) -> dict[str, str]:
    """Parse --key value flags from arg list."""
    flags = {}
    i = 0
    while i < len(args):
        if args[i].startswith("--") and i + 1 < len(args):
            key = args[i][2:]
            flags[key] = args[i + 1]
            i += 2
        else:
            i += 1
    return flags


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m goodman.trello_cli <command> [args...]")
        print("Commands: create, list, move, board, lists, labels")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "create":
        if len(sys.argv) < 4:
            print("Usage: python -m goodman.trello_cli create <list_name> <card_title> [--desc text] [--priority color] [--due YYYY-MM-DD]")
            sys.exit(1)
        list_name = sys.argv[2]
        card_title = sys.argv[3]
        flags = _parse_flags(sys.argv[4:])
        print(trello.create_card(
            list_name,
            card_title,
            desc=flags.get("desc", ""),
            priority=flags.get("priority"),
            due=flags.get("due"),
        ))

    elif cmd == "list":
        list_name = sys.argv[2] if len(sys.argv) > 2 else None
        print(trello.list_cards(list_name))

    elif cmd == "move":
        if len(sys.argv) < 4:
            print("Usage: python -m goodman.trello_cli move <card_name> <to_list>")
            sys.exit(1)
        card_name = sys.argv[2]
        to_list = sys.argv[3]
        print(trello.move_card(card_name, to_list))

    elif cmd == "update":
        if len(sys.argv) < 3:
            print("Usage: python -m goodman.trello_cli update <card_name> [--priority color]")
            sys.exit(1)
        card_name = sys.argv[2]
        flags = _parse_flags(sys.argv[3:])
        print(trello.update_card(card_name, priority=flags.get("priority")))

    elif cmd == "board":
        print(trello.get_board_info())

    elif cmd == "lists":
        print(trello.get_lists())

    elif cmd == "labels":
        print(trello.get_labels())

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
