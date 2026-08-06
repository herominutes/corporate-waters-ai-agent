# Skill: Unattended automations

Jobs Saul runs on cron, end to end, without being asked.

## Daily mobile-review triage
Every day at 15:00 UTC (`tools/automations/mobile_reviews_daily.py`):
1. Pull the day's app-store reviews from the reviews Slack channel.
2. Filter to ≤3★.
3. Translate each to Russian **in-thread**, cluster by product area.
4. Tag the charter owner for that area (CX / platform / tech ownership map
   lives in the script) and attach a gist of recurring complaints.
Result: every bad review lands on the right PM's desk within a day, in their
language, with zero human routing.

## Heartbeat
A 60-minute proactive loop with three cost/noise controls:
- **Pre-filter** — deterministic checks before any model call.
- **Cheap-model triage** — a small model decides "is this worth the boss's
  attention?" (~90% cost reduction vs. always asking the big model).
- **Rate limits** — cooldown between proactive messages and a hard daily cap.
The bar for interrupting the boss is deliberately high: a missed notification
is cheaper than a noisy assistant.

## Watchers
Ad-hoc cron watchers (stock entry/exit signals, consulate appointment slots)
follow the same pattern: deterministic detection, message only on state change.
