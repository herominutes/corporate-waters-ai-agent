#!/usr/bin/env python3
"""CLI for Confluence operations — callable by Saul via Bash.

Usage:
  python3 -m goodman.confluence_cli fetch <page_id>
  python3 -m goodman.confluence_cli url <confluence_url>
  python3 -m goodman.confluence_cli search <query>
  python3 -m goodman.confluence_cli pages [space]
  python3 -m goodman.confluence_cli tree [space]
  python3 -m goodman.confluence_cli cached <query>
  python3 -m goodman.confluence_cli create <title> <body_text> [--parent <page_id>]
  python3 -m goodman.confluence_cli update <page_id> [--title <new_title>] [--body <new_body>]
"""

import sys
from goodman import confluence


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    action = sys.argv[1]
    args = sys.argv[2:]

    if not confluence.enabled():
        print("ERROR: Confluence not configured (check CONFLUENCE_* env vars)")
        sys.exit(1)

    if action == "fetch":
        if not args:
            print("Usage: confluence_cli fetch <page_id>")
            return
        page = confluence.fetch_and_cache(args[0])
        if page:
            print(f"# {page['title']}")
            print(f"URL: {page['url']}")
            print(f"Page ID: {page['page_id']}")
            print()
            print(page["body"] or "(empty page)")
        else:
            print(f"Page {args[0]} not found")

    elif action == "url":
        if not args:
            print("Usage: confluence_cli url <confluence_url>")
            return
        page = confluence.fetch_page_by_url(args[0])
        if page:
            confluence.cache_page(page)
            print(f"# {page['title']}")
            print(f"URL: {page['url']}")
            print()
            print(page["body"] or "(empty page)")
        else:
            print(f"Could not fetch page from URL: {args[0]}")

    elif action == "search":
        if not args:
            print("Usage: confluence_cli search <query>")
            return
        query = " ".join(args)
        results = confluence.search_pages(query)
        if not results:
            print(f"No results for: {query}")
            return
        print(f"Found {len(results)} results:\n")
        for r in results:
            print(f"## {r['title']}")
            print(f"URL: {r['url']} | ID: {r['page_id']}")
            preview = r["body"][:500] if r["body"] else "(empty)"
            print(preview)
            print()

    elif action == "pages":
        space = args[0] if args else None
        pages = confluence.get_space_pages(space)
        if not pages:
            print("No pages found")
            return
        print(f"Pages in space {space or confluence.CONFLUENCE_DEFAULT_SPACE}:\n")
        for p in pages:
            print(f"  [{p['page_id']}] {p['title']}")
            print(f"           {p['url']}")

    elif action == "cached":
        if not args:
            print("Usage: confluence_cli cached <query>")
            return
        query = " ".join(args)
        results = confluence.search_cached(query)
        if not results:
            print(f"Nothing in cache matching: {query}")
            return
        print(f"Cached results for \"{query}\":\n")
        for r in results:
            print(f"## {r['title']}")
            print(f"URL: {r['url']}")
            preview = r["body"][:500] if r["body"] else "(empty)"
            print(preview)
            print()

    elif action == "tree":
        space = args[0] if args else None
        print(confluence.get_space_structure_summary(space))

    elif action == "create":
        if len(args) < 2:
            print("Usage: confluence_cli create <title> <body_text> "
                  "[--parent-title \"<section>\" | --parent <page_id> | --root]")
            return
        parent_id = None
        parent_title = None
        want_root = False
        positional = []
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--parent" and i + 1 < len(args):
                parent_id = args[i + 1]
                i += 2
            elif a in ("--parent-title", "-pt") and i + 1 < len(args):
                parent_title = args[i + 1]
                i += 2
            elif a == "--root":
                want_root = True
                i += 1
            else:
                positional.append(a)
                i += 1
        title = positional[0] if positional else ""
        body_text = " ".join(positional[1:]) if len(positional) > 1 else ""
        # If body looks like a file path, read it
        from pathlib import Path
        if body_text and " " not in body_text and Path(body_text).exists():
            body_text = Path(body_text).read_text()

        # Resolve a parent section by title (preferred — the brain knows the
        # section name, rarely the numeric id).
        if parent_title and not parent_id:
            match = confluence.find_page_by_title(parent_title)
            if not match:
                print(f"Could not find a page titled '{parent_title}' to nest under. "
                      "Pick one of the existing sections below and re-run with "
                      "--parent-title \"<exact section>\":\n")
                print(confluence.get_space_structure_summary())
                sys.exit(1)
            parent_id = match["page_id"]
            print(f"Nesting under '{match['title']}' [id:{parent_id}]")

        # Stop the silent root-dump: require a deliberate placement.
        if not parent_id and not want_root:
            print("Refusing to create at the space ROOT by default — pages belong under "
                  "the right section.\nRe-run with --parent-title \"<section>\" (recommended), "
                  "--parent <page_id>, or --root to force root placement.\n")
            print(confluence.get_space_structure_summary())
            sys.exit(1)

        result = confluence.create_page(title, body_text, parent_id=parent_id)
        if result:
            print(f"Created: {result['title']}")
            print(f"URL: {result['url']}")
            print(f"Page ID: {result['page_id']}")
        else:
            print("Failed to create page")
            sys.exit(1)

    elif action == "update":
        if not args:
            print("Usage: confluence_cli update <page_id> [--title <new_title>] [--body <text_or_file>]")
            return
        page_id = args[0]
        title = None
        body = None
        i = 1
        while i < len(args):
            if args[i] == "--title" and i + 1 < len(args):
                title = args[i + 1]
                i += 2
            elif args[i] == "--body" and i + 1 < len(args):
                body = " ".join(args[i + 1:])
                from pathlib import Path
                if Path(body).exists() and " " not in body:
                    body = Path(body).read_text()
                break
            else:
                i += 1
        if not title and not body:
            print("Nothing to update. Use --title and/or --body")
            return
        result = confluence.update_page(page_id, title=title, body=body)
        if result:
            print(f"Updated: {result['title']} (v{result['version']})")
            print(f"URL: {result['url']}")
        else:
            print("Failed to update page")
            sys.exit(1)

    else:
        print(f"Unknown action: {action}")
        print(__doc__)


if __name__ == "__main__":
    main()
