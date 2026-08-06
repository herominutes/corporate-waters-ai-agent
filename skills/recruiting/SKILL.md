# Skill: Recruiting pipeline (DemoATS ATS + sourcing)

## Pipeline discipline
- **Role-match guard:** attaching a candidate to a vacancy in a different
  discipline (e.g. a PM into an engineering funnel) is *blocked in code*, with
  an explicit `--force` override. Added after a misfiling incident — the guard
  makes the mistake impossible rather than asking the model to be careful.
- **Attributed feedback:** the DemoATS API can't impersonate coworkers, so
  interview feedback requires an `--author` and is prefixed with the real
  author's name. No anonymous or misattributed notes.
- Stage moves on existing candidate-vacancy links are never blocked — the
  guard only fires on *new* attaches.

## Sourcing
- **LinkedIn parser** — browser-daemon only (never plain HTTP), small page
  batches, login validated via session cookie.
- **Calendar as contact database:** past interview events are searched for
  attendee emails before ever asking the boss for someone's contact.

## Evaluation
The Hermes-generated `team-hiring-evaluation` skill (see `skills/hermes/`)
carries the interview-assessment rubric; comp discipline rule: a mid-level
candidate's offer lands at or under their ask — preserve raise runway to the
role ceiling.
