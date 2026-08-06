# Skill: Calendar & scheduling across three Google accounts

## Accounts
Personal Gmail, work Workspace, and a dedicated **PA account** (same work
domain) used when Saul books meetings on behalf of coworkers over Slack.
Service selection matches the **exact email first**, then domain — two
accounts share a domain, so domain-first routing sends events to the wrong
account (learned the hard way).

## Procedure
1. **Resolve the contact deterministically.** Names never go straight to an
   email guess: `contacts.canonical()` maps any known address to the person's
   preferred work address; unknown/ambiguous names are blocked until the boss
   confirms. (Born from a wrong-person invite incident.)
2. **Check the calendar before booking.** Availability check first, never
   blind-book. Weekend slots on the work calendar are refused by default.
3. **Timezone routing:** Slack callers get Asia/Almaty, the boss gets
   Europe/Vilnius — set per-request via environment, so the same code answers
   both correctly.
4. **External attendees** get an ICS invite by email (Workspace blocks API
   notifications to external domains).
5. **Report by quoting the tool output verbatim** — attendee emails are never
   re-typed from memory.

## Safety gates (enforced in code, not prompt)
- Calendar deletes are scoped to PA-booked events only.
- Email sending is restricted to an allowlisted set of domains.
- The PA calendar is excluded from proactive daily summaries (pure noise for
  the boss) while remaining fully searchable for operations.
