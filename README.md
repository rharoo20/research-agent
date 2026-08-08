# Research Agent

A weekly, autonomous research agent that runs on GitHub Actions. It tracks two
independent things and emails you a report on each:

- **Frontier** — emerging technology and research breakthroughs (arXiv,
  bioRxiv, research-lab announcements, patents, technical press) that could
  plausibly break into mainstream awareness or become a business.
- **Trends** — early-stage consumer, cultural, or market trends (social
  platforms, niche forums, small-business/retail press) before they hit
  mainstream media.

Each run researches the week via Claude + web search, diffs findings against
a persistent memory file per track (so it never re-reports the same thing as
new), updates that memory, writes a dated Markdown report into `reports/`,
and — if there's anything to report — emails you a summary.

## One-time setup

### 1. Get an Anthropic API key

1. Go to [platform.claude.com](https://platform.claude.com) and sign in (or create an account).
2. Go to **Settings → API Keys** and create a new key.
3. Copy it — you'll add it as a repo secret below. You'll also want billing set up on the account, since this makes real API calls each week.

### 2. Get a Resend API key

1. Go to [resend.com](https://resend.com) and sign up (or sign in).
2. Go to **API Keys** and create a new key with send access.
3. Copy it — you'll add it as a repo secret below. Resend only shows the key once.

Emails are sent from `onboarding@resend.dev`, Resend's shared sandbox
address — it works with no domain verification, so there's nothing else to
configure to get started. **Caveat:** without verifying your own domain in
Resend, this sandbox sender can only deliver to the email address associated
with your Resend account (Resend's anti-abuse restriction for unverified
senders). If you want `RECIPIENT_EMAIL` to be a different address, verify a
domain in Resend and change `RESEND_FROM_ADDRESS` in `agent.py` to an address
on that domain.

### 3. Add repo secrets

In this repo on GitHub: **Settings → Secrets and variables → Actions → New repository secret**. Add all three:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | The key from step 1 |
| `RESEND_API_KEY` | The key from step 2 |
| `RECIPIENT_EMAIL` | Where you want the reports sent |

### 4. Trigger a manual test run before trusting the schedule

Don't wait for Saturday. Go to the **Actions** tab → **weekly-report** workflow
→ **Run workflow** (this uses the `workflow_dispatch` trigger). Watch the run:

- It should install dependencies, run `agent.py`, and (assuming it found
  anything) commit updated `*_memory.json` files and new files under
  `reports/frontier/` and `reports/trends/` back to the repo.
- Check your inbox for up to two emails: "Weekly Frontier Report" and
  "Weekly Trends Report" — subject to the "skip email if nothing to report"
  behavior described below.
- Open the new `reports/<track>/<date>.md` files in the repo — these are the
  primary artifact meant to be read, independent of email.

If a run fails, check the Actions log first — most failures are a missing/typo'd
secret, or the Resend sandbox-sender delivery restriction described in step 2
above (check the Resend dashboard's **Logs** tab for the exact rejection reason).

## How it works

- `agent.py` runs both tracks in one invocation. For each track it: loads
  `<track>_memory.json` → asks Claude (with the `web_search` tool) to research
  the week, given what's already tracked → parses Claude's JSON response into
  new/updated entries → merges into memory → writes
  `reports/<track>/<YYYY-MM-DD>.md` → emails an HTML summary (skipped if
  nothing new or updated that week, though the file is still written).
- Memory schema (same shape in both `frontier_memory.json` and
  `trends_memory.json`):

  ```json
  {
    "id": "short-slug",
    "first_seen": "YYYY-MM-DD",
    "topic": "short description",
    "status": "emerging | developing | maturing | mainstream",
    "last_update": "YYYY-MM-DD",
    "summary": "one-line summary",
    "why_it_matters": "why this is commercially interesting or worth watching",
    "update_log": ["dated notes on developments over time"]
  }
  ```

- Both memory files and every report file get committed back to the repo by
  the workflow, so the full history lives in git — `git log` on either memory
  file is effectively a changelog of what the agent learned week to week.
- Each run caps itself at 5 new entries and 8 updates per track (configurable
  — see below) so reports stay skimmable and API cost stays bounded.

## Editing either track's prompt or focus

Both prompts live in `agent.py` as `FRONTIER_PROMPT` and `TRENDS_PROMPT`.
Edit the prose directly — e.g. to add/remove preferred sources, change what
counts as noise, or adjust the per-track focus.

The `emerging → developing → maturing → mainstream` status scale is shared
between both tracks and defined once, in the `STATUS_ANCHORS` constant near
the top of `agent.py`:

| Status | Anchor |
|---|---|
| `emerging` | First credible signal spotted; single source or small cluster; no measurable momentum yet |
| `developing` | Multiple independent sources within weeks of each other; early funding, a launch, or visible growth |
| `maturing` | Picked up by mid-tier trade/tech press or a recognizable brand/lab; clear commercial moves |
| `mainstream` | Major consumer press, a top app-store/retail category, or a Fortune-500-scale decision |

If the model is classifying too aggressively or too conservatively, tighten
or loosen these anchor definitions rather than adding a numeric threshold —
they're deliberately judgment-based so the model can weigh real-world
evidence instead of counting weeks.

To change the per-week cap, edit `MAX_NEW` / `MAX_UPDATES` near the top of
`agent.py`.

## Schedule and the DST caveat

The workflow (`.github/workflows/weekly-report.yml`) is scheduled for
**Saturday 9am America/Chicago**. GitHub Actions `schedule` triggers run on
UTC cron and have **no timezone or DST awareness**, so the cron expression is
a fixed UTC time that only matches 9am Chicago time for part of the year:

- `0 14 * * 6` → 9am **CDT** (UTC-5), correct roughly mid-March through
  early November — this is what's currently in the workflow.
- `0 15 * * 6` → 9am **CST** (UTC-6), correct roughly early November through
  mid-March.

Flip between the two lines around the US DST transitions if you want the
send time to stay accurate to the hour, or just leave it — the drift is one
hour, twice a year, and cosmetic.

`workflow_dispatch` (the manual trigger) ignores the cron schedule entirely,
so you can always run it on demand regardless of what time it currently is.
