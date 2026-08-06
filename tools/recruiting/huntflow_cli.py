"""CLI interface for DemoATS recruiting — called by the brain via Bash.

Usage:
    PYTHONPATH=src python -m goodman.demoats_cli vacancies
    PYTHONPATH=src python -m goodman.demoats_cli funnel <vacancy_id>
    PYTHONPATH=src python -m goodman.demoats_cli statuses
    PYTHONPATH=src python -m goodman.demoats_cli coworkers
    PYTHONPATH=src python -m goodman.demoats_cli find "<query>"
    PYTHONPATH=src python -m goodman.demoats_cli candidate <applicant_id>
    PYTHONPATH=src python -m goodman.demoats_cli cv <applicant_id>
    PYTHONPATH=src python -m goodman.demoats_cli feedback <applicant_id>
    PYTHONPATH=src python -m goodman.demoats_cli add-candidate "<First>" "<Last>" \
        [--position P] [--company C] [--money M] [--phone PH] [--email E] [--middle MN] \
        [--resume /path/to/cv.pdf]
    PYTHONPATH=src python -m goodman.demoats_cli upload-resume /path/to/cv.pdf
    PYTHONPATH=src python -m goodman.demoats_cli add-feedback <applicant_id> \
        --vacancy <vacancy_id> --text "<comment>" --author "<who gave this feedback>"
    PYTHONPATH=src python -m goodman.demoats_cli set-status <applicant_id> \
        --vacancy <vacancy_id> --status "<stage name or id>" [--comment "..." --author "..."] [--reject "<reason>"]

Notes:
  - Funnel stages and rejection reasons can be passed by NAME (case-insensitive)
    or numeric id. Use `statuses` to see stage names.
  - add-feedback keeps the candidate's current stage and just appends a comment.
  - DemoATS shows all API comments as "Published from API", so --author is REQUIRED
    on add-feedback: pass the real person whose feedback it is (e.g. "Alex Chen"),
    and it is prepended to the comment. Use --author Saul only for Saul's own notes.
"""

import argparse
import os
import re
import sys

from goodman import demoats as hf
from goodman.config import HUNTFLOW_UI_BASE


# --- Role-mismatch guard ---------------------------------------------------
# Stops candidates landing on a funnel of the wrong discipline (e.g. a Product
# Manager attached to an engineering vacancy). Checked in order; first match
# wins, so "design" precedes "product" ("Product Designer" classifies design).

_ROLE_CLASSES = [
    ("design",      r"design|дизайн|\bux\b|\bui\b"),
    ("qa",          r"\bqa\b|\btest(?:ing|er)?\b|тестиров"),
    ("hr",          r"\bhr\b|recruit|talent|кадров"),
    ("data/ml",     r"\bml\b|machine.learning|data.scien|data.engineer|аналитик данных"),
    ("product",     r"product|продакт|продукт|\bcpo\b|\bpm\b|\bspm\b"),
    ("engineering", r"engineer|developer|разработ|инженер|backend|frontend|full.?stack|"
                    r"android|\bios\b|\bswe\b|architect|\bcto\b|devops|\bsre\b"),
]


def _role_class(text: str) -> str | None:
    t = (text or "").lower()
    for cls, pat in _ROLE_CLASSES:
        if re.search(pat, t):
            return cls
    return None


def _guard_role_match(candidate_position: str, vacancy_id: int, force: bool, *, context: str):
    """Refuse cross-discipline vacancy attaches unless --force is passed."""
    if force:
        return
    cand_cls = _role_class(candidate_position)
    if not cand_cls:
        return  # can't classify the candidate -> don't block
    try:
        vac = hf.get_vacancy(vacancy_id)
    except Exception:
        return  # vacancy lookup failed -> let the API call surface the real error
    vac_cls = _role_class(vac.get("position") or "")
    if not vac_cls or vac_cls == cand_cls:
        return
    try:
        suggestions = "; ".join(
            f"[{v['id']}] {v['position']}" for v in hf.list_vacancies()
            if _role_class(v.get("position") or "") == cand_cls
        )
    except Exception:
        suggestions = ""
    msg = (
        f"BLOCKED ({context}): candidate position '{candidate_position}' classifies as "
        f"{cand_cls.upper()}, but vacancy [{vacancy_id}] '{vac.get('position')}' is "
        f"{vac_cls.upper()}. Wrong funnel?\n"
        + (f"  Open {cand_cls} vacancies: {suggestions}\n" if suggestions else "")
        + "  Re-run with the correct --vacancy, or add --force ONLY if this "
          "cross-discipline attach is genuinely intentional."
    )
    raise SystemExit(msg)


def _require_owner(action: str):
    """Block owner-only actions for non-owner Slack callers.

    brain.py sets SLACK_CALLER_ROLE for any non-owner Slack request; it's unset
    for the boss (Telegram / Slack owner).
    """
    if os.getenv("SLACK_CALLER_ROLE"):
        raise SystemExit(
            f"'{action}' is an owner-only action — only Alex can do it. "
            f"Route this to him rather than executing."
        )


def _fmt_money(m):
    return f" | {m}" if m else ""


def _resolve_status(value: str) -> int:
    """Resolve a funnel stage given by name or numeric id to its id."""
    if value.isdigit():
        return int(value)
    _, by_name = hf.status_maps()
    sid = by_name.get(value.strip().lower())
    if not sid:
        names = ", ".join(sorted({n for n in by_name}))
        raise SystemExit(f"Unknown stage '{value}'. Known stages: {names}")
    return sid


def _resolve_reason(value: str) -> int:
    if value.isdigit():
        return int(value)
    for r in hf.get_rejection_reasons():
        if r["name"].strip().lower() == value.strip().lower():
            return r["id"]
    names = ", ".join(r["name"] for r in hf.get_rejection_reasons())
    raise SystemExit(f"Unknown rejection reason '{value}'. Known: {names}")


def cmd_vacancies(args):
    vacs = hf.list_vacancies(state=None if args.all else "OPEN")
    if not vacs:
        print("No vacancies found.")
        return
    print(f"Vacancies ({len(vacs)}):")
    for v in vacs:
        money = _fmt_money(v.get("money"))
        print(f"  [{v['id']}] {v['position']} — {v.get('state')}{money}")


def cmd_funnel(args):
    v = hf.get_vacancy(args.vacancy_id)
    print(f"Funnel for [{v['id']}] {v['position']} ({v.get('state')}):")
    total = 0
    for name, sid, cnt in hf.funnel(args.vacancy_id):
        bar = "#" * cnt
        total += cnt
        print(f"  {name:<28} {cnt:>3} {bar}")
    print(f"  {'TOTAL':<28} {total:>3}")


def cmd_statuses(args):
    print("Funnel stages (in order):")
    for s in hf.get_statuses():
        print(f"  [{s['id']}] {s['name']}")


def cmd_coworkers(args):
    print("Coworkers:")
    for c in hf.list_coworkers():
        print(f"  [{c['member']}] {c['name']} ({c.get('type')}) {c.get('email') or ''}")


def cmd_find(args):
    items = hf.search_applicants(args.query)
    if not items:
        print(f"No candidates matching '{args.query}'.")
        return
    print(f"Candidates matching '{args.query}':")
    for a in items:
        name = " ".join(filter(None, [a.get("first_name"), a.get("last_name")]))
        print(f"  [{a['id']}] {name} — {a.get('position') or '?'}"
              f"{' @ ' + a['company'] if a.get('company') else ''}")


def _status_name(by_id, sid):
    return by_id.get(sid, f"status#{sid}")


def cmd_candidate(args):
    a = hf.get_applicant(args.applicant_id)
    by_id, _ = hf.status_maps()
    vacs = {v["id"]: v["position"] for v in hf.list_vacancies(state=None)}
    name = " ".join(filter(None, [a.get("first_name"), a.get("middle_name"), a.get("last_name")]))
    print(f"[{a['id']}] {name}")
    if a.get("position"):
        print(f"  Position: {a['position']}{' @ ' + a['company'] if a.get('company') else ''}")
    for f in ("email", "phone", "money"):
        if a.get(f):
            print(f"  {f.capitalize()}: {a[f]}")
    links = a.get("links", [])
    if links:
        print("  On vacancies:")
        for l in links:
            vac = vacs.get(l["vacancy"], f"vacancy#{l['vacancy']}")
            rej = " [REJECTED]" if l.get("rejection_reason") else ""
            print(f"    - {vac} → {_status_name(by_id, l['status'])}{rej}")
    else:
        print("  Not attached to any vacancy.")


def cmd_feedback(args):
    logs = hf.get_applicant_logs(args.applicant_id)
    by_id, _ = hf.status_maps()
    if not logs:
        print("No activity/feedback logged for this candidate.")
        return
    print(f"Feedback & history for candidate {args.applicant_id} (newest first):")
    for log in logs:
        who = (log.get("account_info") or {}).get("name", "?")
        when = (log.get("created") or "")[:16].replace("T", " ")
        kind = log.get("type", "")
        stage = _status_name(by_id, log["status"]) if log.get("status") else ""
        head = f"  [{when}] {who} · {kind}{' → ' + stage if stage else ''}"
        print(head)
        if log.get("comment"):
            for line in log["comment"].splitlines():
                print(f"      {line}")


def _fmt_date(d):
    if not d:
        return ""
    y, m = d.get("year"), d.get("month")
    return f"{y}-{m:02d}" if y and m else str(y or "")


def _render_resume(resume: dict) -> str:
    """Flatten DemoATS's structured resume object into readable text.

    Used when a candidate's CV has no parsed plain-text body (e.g. LinkedIn-
    sourced profiles) but does carry structured experience/skills/education.
    """
    lines = []
    pi = resume.get("personal_info") or {}
    name = " ".join(filter(None, [pi.get("first_name"), pi.get("last_name")]))
    if name:
        lines.append(name)
    skills = resume.get("skill_set") or []
    if skills:
        lines.append("\nSkills: " + ", ".join(skills))
    exp = resume.get("experience") or []
    if exp:
        lines.append("\nExperience:")
        for e in exp:
            span = " – ".join(filter(None, [_fmt_date(e.get("date_from")),
                                            _fmt_date(e.get("date_to")) or "present"]))
            company = e.get("company") or ""
            city = ((e.get("area") or {}).get("city") or {}).get("name") or ""
            head = " | ".join(filter(None, [e.get("position") or "", company,
                                            city, span]))
            lines.append(f"  • {head}")
            if e.get("description"):
                for ln in e["description"].splitlines():
                    if ln.strip():
                        lines.append(f"      {ln.strip()}")
    edu = resume.get("education") or {}
    higher = (edu.get("higher") or []) if isinstance(edu, dict) else []
    if higher:
        lines.append("\nEducation:")
        for ed in higher:
            head = " | ".join(filter(None, [ed.get("name") or "", ed.get("faculty") or "",
                                            _fmt_date(ed.get("date_to"))]))
            lines.append(f"  • {head}")
    return "\n".join(lines).strip()


def cmd_cv(args):
    cvs = hf.get_applicant_cv(args.applicant_id)
    if not cvs:
        print(f"No resume attached to candidate {args.applicant_id}.")
        return
    for i, cv in enumerate(cvs, 1):
        label = f"Resume {i}/{len(cvs)} (external {cv['id']})"
        if cv.get("source_url"):
            label += f" — source: {cv['source_url']}"
        print(label)
        body = cv.get("body")
        if body:
            print(body)
        else:
            rendered = _render_resume(cv.get("resume") or {})
            if rendered:
                print(rendered)
            else:
                print("  (no parsed text or structured resume available — "
                      "open in DemoATS web to view the original file)")
        print()


def cmd_add_candidate(args):
    resume_id = None
    resume_text = ""
    parsed: dict = {}
    if args.resume:
        if not os.path.isfile(args.resume):
            raise SystemExit(f"Resume file not found: {args.resume}")
        with open(args.resume, "rb") as fh:
            data = fh.read()
        up = hf.upload_file(data, os.path.basename(args.resume), parse=True)
        resume_id = up.get("id")
        resume_text = up.get("text") or ""
        parsed = up.get("fields") or {}

    # Explicit args win; fall back to fields parsed from the CV.
    position = args.position or parsed.get("position") or ""
    email = args.email or parsed.get("email") or ""
    phone = args.phone or (parsed.get("phones") or [""])[0]

    # Guard BEFORE creating the card, so a blocked add leaves nothing behind.
    if args.vacancy and args.status:
        _guard_role_match(position, args.vacancy, args.force, context="add-candidate")

    a = hf.add_applicant(
        args.first, args.last, middle_name=args.middle or "",
        position=position, company=args.company or "",
        money=args.money or "", phone=phone, email=email,
        resume_id=resume_id, resume_text=resume_text,
    )
    name = " ".join(filter(None, [a.get("first_name"), a.get("last_name")]))
    print(f"Candidate created: [{a['id']}] {name}")
    if resume_id:
        print(f"  Resume attached (upload id {resume_id}).")
    if args.vacancy and args.status:
        sid = _resolve_status(args.status)
        hf.move_applicant(a["id"], args.vacancy, sid, comment=args.comment or "")
        print(f"  Added to vacancy {args.vacancy} at stage '{args.status}'.")
    print(f"  Open in DemoATS: {HUNTFLOW_UI_BASE}/my/acme")


def cmd_upload_resume(args):
    """Upload + parse a CV without creating a candidate (preview)."""
    if not os.path.isfile(args.path):
        raise SystemExit(f"File not found: {args.path}")
    with open(args.path, "rb") as fh:
        data = fh.read()
    up = hf.upload_file(data, os.path.basename(args.path), parse=True)
    f = up.get("fields") or {}
    print(f"Uploaded & parsed '{os.path.basename(args.path)}' (upload id {up.get('id')}):")
    print(f"  name:     {f.get('name')}")
    print(f"  position: {f.get('position')}")
    print(f"  email:    {f.get('email')}")
    print(f"  phones:   {', '.join(f.get('phones') or []) or None}")
    print(f"  telegram: {f.get('telegram')}")
    print(f"  (no candidate created — use: add-candidate \"First\" \"Last\" --resume {args.path})")


def _current_status_on_vacancy(applicant_id: int, vacancy: int) -> int:
    a = hf.get_applicant(applicant_id)
    for l in a.get("links", []):
        if l.get("vacancy") == vacancy:
            return l["status"]
    raise SystemExit(f"Candidate {applicant_id} is not attached to vacancy {vacancy}. "
                     f"Use set-status to attach them first.")


def _attribute(text: str, author: str | None) -> str:
    return f"Отзыв от {author.strip()}:\n{text}" if author and author.strip() else text


def cmd_add_feedback(args):
    status = _current_status_on_vacancy(args.applicant_id, args.vacancy)
    hf.move_applicant(args.applicant_id, args.vacancy, status,
                      comment=_attribute(args.text, args.author))
    print(f"Feedback added to candidate {args.applicant_id} on vacancy {args.vacancy} "
          f"(author: {args.author}).")


def _resolve_coworker(value: str) -> dict:
    """Resolve a coworker given by id, name, or email to a coworker dict."""
    if value.isdigit():
        cw = hf.get_coworker(int(value))
        return cw
    matches = hf.find_coworker(value)
    if not matches:
        raise SystemExit(f"No coworker matching '{value}'. Run `coworkers` to list them.")
    if len(matches) > 1:
        names = ", ".join(f"{m['name']} ({m['email']})" for m in matches)
        raise SystemExit(f"Ambiguous '{value}' — matches: {names}. Be more specific or pass the id.")
    return matches[0]


def cmd_grant(args):
    _require_owner("grant access")
    cw = _resolve_coworker(args.coworker)
    hf.grant_vacancy_access(args.vacancy_id, cw["id"])
    print(f"Granted {cw['name']} access to vacancy {args.vacancy_id}.")


def cmd_revoke(args):
    _require_owner("revoke access")
    cw = _resolve_coworker(args.coworker)
    hf.revoke_vacancy_access(args.vacancy_id, cw["id"])
    print(f"Revoked {cw['name']}'s access to vacancy {args.vacancy_id}.")


def cmd_access(args):
    cw = _resolve_coworker(args.coworker)
    vac_ids = hf.coworker_vacancy_ids(cw["id"])
    vacs = {v["id"]: v["position"] for v in hf.list_vacancies(state=None)}
    print(f"{cw['name']} ({cw.get('type')}) has access to {len(vac_ids)} vacancy(ies):")
    for vid in sorted(vac_ids):
        print(f"  [{vid}] {vacs.get(vid, '?')}")


def cmd_register_user(args):
    _require_owner("register-user")
    res = hf.create_user(args.name, args.email, role=args.role)
    if res.get("scheduled"):
        print(f"User creation scheduled for {args.name} <{args.email}> (role {args.role}). "
              f"It may take a moment to appear — re-check with `coworkers`.")
    else:
        print(f"Registered {res['name']} <{res.get('email')}> as DemoATS user "
              f"(coworker id {res['id']}, role {res.get('type')}).")
        print("You can now grant them vacancy access with: grant <vacancy_id> \""
              + res["name"] + "\"")


def cmd_create_vacancy(args):
    _require_owner("create-vacancy")
    v = hf.create_vacancy(
        args.position, money=args.money or "", body=args.body or "",
        division=args.division, priority=args.priority or 0,
    )
    print(f"Vacancy created: [{v['id']}] {v['position']} ({v.get('state')})")
    print(f"  Open in DemoATS: {HUNTFLOW_UI_BASE}/my/acme")


def cmd_set_status(args):
    # Guard only FRESH attaches: stage moves on an existing link (incl. rejecting
    # a misfiled candidate off the wrong funnel) must never be blocked.
    a = hf.get_applicant(args.applicant_id)
    already_linked = any(l.get("vacancy") == args.vacancy for l in a.get("links", []))
    if not already_linked:
        _guard_role_match(a.get("position") or "", args.vacancy, args.force,
                          context="set-status attach")
    sid = _resolve_status(args.status)
    reason = _resolve_reason(args.reject) if args.reject else None
    hf.move_applicant(args.applicant_id, args.vacancy, sid,
                      comment=_attribute(args.comment, args.author) if args.comment else "",
                      rejection_reason=reason)
    print(f"Candidate {args.applicant_id} moved to '{args.status}' on vacancy {args.vacancy}"
          + (f" (rejected: {args.reject})" if args.reject else "") + ".")


def build_parser():
    p = argparse.ArgumentParser(prog="demoats_cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    sv = sub.add_parser("vacancies"); sv.add_argument("--all", action="store_true"); sv.set_defaults(func=cmd_vacancies)
    sf = sub.add_parser("funnel"); sf.add_argument("vacancy_id", type=int); sf.set_defaults(func=cmd_funnel)
    sub.add_parser("statuses").set_defaults(func=cmd_statuses)
    sub.add_parser("coworkers").set_defaults(func=cmd_coworkers)
    sfi = sub.add_parser("find"); sfi.add_argument("query"); sfi.set_defaults(func=cmd_find)
    sc = sub.add_parser("candidate"); sc.add_argument("applicant_id", type=int); sc.set_defaults(func=cmd_candidate)
    sfb = sub.add_parser("feedback"); sfb.add_argument("applicant_id", type=int); sfb.set_defaults(func=cmd_feedback)
    scv2 = sub.add_parser("cv"); scv2.add_argument("applicant_id", type=int); scv2.set_defaults(func=cmd_cv)

    sa = sub.add_parser("add-candidate")
    sa.add_argument("first"); sa.add_argument("last")
    sa.add_argument("--middle"); sa.add_argument("--position"); sa.add_argument("--company")
    sa.add_argument("--money"); sa.add_argument("--phone"); sa.add_argument("--email")
    sa.add_argument("--resume", help="Path to a CV file to upload and attach")
    sa.add_argument("--vacancy", type=int); sa.add_argument("--status"); sa.add_argument("--comment")
    sa.add_argument("--force", action="store_true",
                    help="Override the role-mismatch guard (cross-discipline attach)")
    sa.set_defaults(func=cmd_add_candidate)

    sur = sub.add_parser("upload-resume")
    sur.add_argument("path", help="Path to a CV file to upload and parse (no candidate created)")
    sur.set_defaults(func=cmd_upload_resume)

    saf = sub.add_parser("add-feedback")
    saf.add_argument("applicant_id", type=int)
    saf.add_argument("--vacancy", type=int, required=True)
    saf.add_argument("--text", required=True)
    saf.add_argument("--author", required=True,
                     help="Who gave this feedback (real person's name, e.g. 'Alex Chen'); "
                          "prepended to the comment since DemoATS labels API posts 'Published from API'")
    saf.set_defaults(func=cmd_add_feedback)

    sg = sub.add_parser("grant")
    sg.add_argument("vacancy_id", type=int); sg.add_argument("coworker")
    sg.set_defaults(func=cmd_grant)
    sr = sub.add_parser("revoke")
    sr.add_argument("vacancy_id", type=int); sr.add_argument("coworker")
    sr.set_defaults(func=cmd_revoke)
    sac = sub.add_parser("access")
    sac.add_argument("coworker"); sac.set_defaults(func=cmd_access)

    sru = sub.add_parser("register-user")
    sru.add_argument("name"); sru.add_argument("email")
    sru.add_argument("--role", default="manager", choices=["owner", "manager", "watcher"])
    sru.set_defaults(func=cmd_register_user)

    scv = sub.add_parser("create-vacancy")
    scv.add_argument("position")
    scv.add_argument("--money"); scv.add_argument("--body")
    scv.add_argument("--division", type=int); scv.add_argument("--priority", type=int)
    scv.set_defaults(func=cmd_create_vacancy)

    ss = sub.add_parser("set-status")
    ss.add_argument("applicant_id", type=int)
    ss.add_argument("--vacancy", type=int, required=True)
    ss.add_argument("--status", required=True)
    ss.add_argument("--comment"); ss.add_argument("--author")
    ss.add_argument("--reject")
    ss.add_argument("--force", action="store_true",
                    help="Override the role-mismatch guard (cross-discipline attach)")
    ss.set_defaults(func=cmd_set_status)
    return p


def main():
    # SECURITY: tier gate — a non-owner Slack caller needs the 'demoats' capability.
    if os.getenv("SLACK_CALLER_ROLE"):
        from goodman import config as _cfg
        if not _cfg.caller_has(_cfg.CAP_HUNTFLOW):
            print("You're not authorized to use DemoATS recruiting tools.")
            sys.exit(1)
    args = build_parser().parse_args()
    try:
        args.func(args)
    except hf.DemoATSError as e:
        print(f"DemoATS error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
