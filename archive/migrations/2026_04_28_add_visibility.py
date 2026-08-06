"""Add visibility column to memories and backfill personal/shared.

Default is 'personal'. Only memories matching the work whitelist below become 'shared'.
Run once, idempotent.
"""
import sqlite3
import sys

DB = "/home/claude/goodman/data/goodman.db"

SHARED_CATEGORIES = ()

SHARED_DOC_FILE_PATTERNS = (
    "Apex-Mirabad",
    "Pedro-Alex-Ivan",
    "Kayan-Ofiyat",
    "GH-OzMakon",
    "Nirvana.md",
    "Newport.md",
    "NRG-BI",
)

def main():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row

    cols = [r["name"] for r in db.execute("PRAGMA table_info(memories)")]
    if "visibility" not in cols:
        db.execute("ALTER TABLE memories ADD COLUMN visibility TEXT NOT NULL DEFAULT 'personal'")
        db.execute("CREATE INDEX IF NOT EXISTS idx_memories_visibility ON memories(visibility)")
        print("Added visibility column + index.")
    else:
        print("visibility column already exists — re-running backfill.")

    db.execute("UPDATE memories SET visibility = 'personal'")

    placeholders = ",".join("?" * len(SHARED_CATEGORIES))
    if SHARED_CATEGORIES:
        db.execute(f"UPDATE memories SET visibility='shared' WHERE category IN ({placeholders})", SHARED_CATEGORIES)

    for pat in SHARED_DOC_FILE_PATTERNS:
        like = f"[file:{pat}%"
        db.execute("UPDATE memories SET visibility='shared' WHERE category='document' AND content LIKE ?", (like,))

    db.commit()

    print()
    print("=== visibility totals ===")
    for r in db.execute("SELECT visibility, COUNT(*) AS n FROM memories GROUP BY visibility"):
        print(f"  {r['visibility']:9}  {r['n']:>7}")

    print()
    print("=== shared by category ===")
    for r in db.execute("SELECT category, COUNT(*) AS n FROM memories WHERE visibility='shared' GROUP BY category ORDER BY n DESC"):
        print(f"  {r['category']:18}  {r['n']:>6}")

    print()
    print("=== personal by category ===")
    for r in db.execute("SELECT category, COUNT(*) AS n FROM memories WHERE visibility='personal' GROUP BY category ORDER BY n DESC"):
        print(f"  {r['category']:18}  {r['n']:>6}")

if __name__ == "__main__":
    main()
