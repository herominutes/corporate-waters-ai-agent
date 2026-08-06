"""Daily #mobile-reviews watcher (Alex's standing task, 2026-07-03).

Scans the AppFollow-fed #mobile-reviews Slack channel for app-store reviews
rated <=3 stars posted since the last run, and for each one replies IN THREAD:
tag the charter-responsible person + Russian translation (if needed) + gist.

Charter mapping (Domain Ownership 19.05, confirmed by Alex):
  - Ivan Petrov   (CX Lead): seller/buyer experience, ratings-reviews, listings flow
  - Andrei Kovalev (Platform Lead): scam/fraud/fakes, rules/moderation/blocks,
    search/relevance, messenger, monetization/paid packages/pricing
  - Egor Chernov (Eng): crashes, technical bugs, login/OTP, performance

Cron: 0 15 * * *  (20:00 Almaty) — covers the trailing 24h+ via state file.
State: data/mobile_reviews_state.json (high-water ts + replied set).
"""
import json, os, re, subprocess, sys, urllib.parse, urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
CHANNEL = "C0B25BYKMSQ"
STATE_F = BASE / "data" / "mobile_reviews_state.json"
CLAUDE = "/home/claude/.local/bin/claude"
# The channel is fed by >1 AppFollow integration, each posting under its OWN
# bot_id (e.g. B0B1YCC1D29 + B0B1VCJ5QCB, one per store/app). Do NOT hardcode a
# single id — a 2026-07-07 1★ tech review from the second feed was silently
# skipped for exactly that reason. Reviews are now detected structurally (a bot
# message whose attachment leads with ★), so a new integration can't break this.

OWNERS = {
    "cx":       ("U000000005", "Ivan — CX (Seller/Buyer/Ratings)"),
    "platform": ("U000000006", "Andrei — Platform (T&S/Search/Monetization/Messenger)"),
    "tech":     ("U000000003", "Egor — Eng (crashes/tech)"),
}

def load_env():
    for line in (BASE / ".env").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

def slack(method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        f"https://slack.com/api/{method}", data=data,
        headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"})
    r = json.load(urllib.request.urlopen(req, timeout=30))
    if not r.get("ok"):
        raise RuntimeError(f"{method}: {r.get('error')}")
    return r

def parse_review(msg):
    """AppFollow bot message -> dict(stars, lang, text, author) or None."""
    if not msg.get("bot_id") or not msg.get("attachments"):
        return None
    att = msg["attachments"][0]
    body = att.get("text") or ""
    first, _, rest = body.partition("\n")
    stars = first.count("★")
    if not stars:
        return None
    lang = re.sub(r":[a-z0-9_-]+:", "", first.replace("★", "").replace("☆", "")).strip()
    author = ""
    for f in att.get("fields") or []:
        m = re.match(r"by ([^·<]+)", f.get("value") or "")
        if m:
            author = m.group(1).strip()
            break
    return {"stars": stars, "lang": lang, "text": rest.strip(), "author": author}

def analyze(review):
    """claude haiku: translation to RU (if needed) + gist + charter key."""
    prompt = f"""App-store review for ACME Market (classifieds app). Language label: {review['lang']}. Rating: {review['stars']}/5.

REVIEW:
{review['text'][:2000]}

Return ONLY a JSON object:
{{"translation_ru": "<точный перевод отзыва на русский; null если отзыв уже на русском>",
 "gist_ru": "<1-2 предложения по-русски: в чём суть жалобы/проблемы пользователя>",
 "charter": "<one of: cx | platform | tech>"}}

Charter rules: cx = seller/buyer experience, ad posting/editing, ratings, delivery/deal flow, support quality; platform = scam/fraud/fake listings, moderation/rules/blocked accounts, search/relevance, messenger, prices of paid packages/promotion/monetization complaints; tech = app crashes, bugs, login/SMS-code problems, performance, update broke something. If several apply, pick the dominant complaint."""
    out = subprocess.run(
        [CLAUDE, "--print", "--model", "claude-haiku-4-5-20251001"],
        input=prompt, capture_output=True, text=True, timeout=120).stdout.strip()
    m = re.search(r"\{.*\}", out, re.S)
    d = json.loads(m.group(0))
    if d.get("charter") not in OWNERS:
        d["charter"] = "cx"
    return d

def main():
    load_env()
    dry = "--dry-run" in sys.argv
    st = json.loads(STATE_F.read_text()) if STATE_F.exists() else {"last_ts": "0", "replied": []}
    replied = set(st["replied"])

    msgs, cursor = [], None
    while True:
        r = slack("conversations.history", channel=CHANNEL, oldest=st["last_ts"],
                  limit=200, **({"cursor": cursor} if cursor else {}))
        msgs += r["messages"]
        cursor = (r.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break

    msgs.sort(key=lambda m: float(m["ts"]))
    hits = 0
    for msg in msgs:
        rv = parse_review(msg)
        if not rv or rv["stars"] > 3 or msg["ts"] in replied:
            continue
        hits += 1
        try:
            a = analyze(rv)
        except Exception as e:
            print(f"analyze failed ts={msg['ts']}: {e}", file=sys.stderr)
            continue
        uid, role = OWNERS[a["charter"]]
        lines = [f"<@{uid}> ({role})", f"{'⭐' * rv['stars']} {rv['stars']}/5 · {rv['lang']} · {rv['author']}"]
        if a.get("translation_ru"):
            lines.append(f"*Перевод:* {a['translation_ru']}")
        lines.append(f"*Суть:* {a['gist_ru']}")
        text = "\n".join(lines)
        if dry:
            print(f"--- would reply to ts={msg['ts']} ---\n{text}\n")
        else:
            slack("chat.postMessage", channel=CHANNEL, thread_ts=msg["ts"],
                  text=text, unfurl_links="false")
            replied.add(msg["ts"])
            print(f"replied to ts={msg['ts']} charter={a['charter']}")

    if msgs and not dry:
        st["last_ts"] = max(m["ts"] for m in msgs)
        st["replied"] = sorted(replied)[-500:]
        STATE_F.write_text(json.dumps(st))
    print(f"scanned {len(msgs)} msgs, {hits} reviews <=3★")

if __name__ == "__main__":
    main()
