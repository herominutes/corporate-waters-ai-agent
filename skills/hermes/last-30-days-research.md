---
name: Last 30 Days Research
description: Engagement-scored recent research across Reddit, X, Hacker News, YouTube, GitHub, Polymarket — what people actually say/do about a person, company, ticker, or topic in the last ~30 days. Adapted from the /last30days skill (github.com/mvanhorn/last30days-skill).
category: research
tags: last 30 days, recent, buzz, sentiment, reddit, twitter, x, youtube, hacker news, github, polymarket, pre-meeting, due diligence, trending, what people are saying, recency
created: 2026-06-16
updated: 2026-06-16
---

# Last 30 Days Research

WHEN to use: the user wants the *current* reality on a person, company, ticker, product, or topic — "last 30 days", recent buzz, what people are actually saying, sentiment, or pre-meeting / pre-call / due-diligence research. The point is to beat stale Google/LinkedIn with what the crowd is engaging with RIGHT NOW.

HOW (use Saul's tools — `browse` stealth-browser daemon, cached web fetch, search, yt-dlp transcripts; hit sources in parallel, prefer freshest, filter to ~last 30 days):
1. Reddit — search the topic; take top threads/comments by UPVOTES (note subreddit + score).
2. Hacker News — Algolia search past month; rank by points/comments.
3. X/Twitter — recent posts by likes/reposts (browser; use logged-in session if available).
4. YouTube — recent relevant videos; pull the transcript of the most-viewed one.
5. GitHub — for people/projects: recent commits/PRs, stars, merge rate.
6. Polymarket — if a market exists, cite the live odds (money-weighted signal).
7. Web — only to fill gaps with fresh news.

SCORE by real engagement (upvotes, likes, views, stars, $ odds) — weight what people actually engaged with, not editor picks. Drop anything older than ~30 days.

OUTPUT — one tight brief:
- 3-6 bullets of what's genuinely new/changing; each with source + its engagement metric + link.
- "Consensus vs contrarian": what the crowd believes vs the dissent.
- For a person: what they're shipping/saying now. For a ticker/company: catalysts, sentiment shift, controversies.
Cite sources inline. If a platform is inaccessible, say so — never fabricate engagement numbers.
