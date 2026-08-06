# Hermes — self-generated skills

These six skills were **written by the agent itself**: after a successful
multi-step session, a generator distills what worked into a reusable markdown
procedure with frontmatter metadata (name, description, trigger conditions).

At runtime, incoming messages are embedded and the top-3 most similar skills
(capped at 2,000 chars each) are injected into context — progressive
disclosure instead of a monolithic prompt.

The approach was benchmarked against a no-skills baseline on held-out tasks
(baseline-vs-skills eval; benchmark code not included in this snapshot).
