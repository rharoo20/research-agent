# Research Agent

An on-demand research tracker, run from a browser dashboard — no schedule, no
autonomous spend. It tracks two independent things:

- **Frontier** — emerging technology and research breakthroughs (arXiv,
  bioRxiv, research-lab announcements, patents, technical press) that could
  plausibly break into mainstream awareness or become a business.
- **Trends** — early-stage consumer, cultural, or market trends (social
  platforms, niche forums, small-business/retail press) before they hit
  mainstream media.

You trigger a run from the [dashboard](#dashboard) (`docs/index.html`,
served via GitHub Pages), watch its progress live, then browse and discuss
the results — nothing happens on its own until you click **Run**.

## Repo structure

```
research-agent/
├── agent.py                        # the research pipeline
├── config.json                     # per-track tunables (dashboard-editable)
├── prompts/
│   ├── frontier.md                 # Frontier track's research brief
│   └── trends.md                   # Trends track's research brief
├── frontier_memory.json            # persistent tracked-signal log
├── trends_memory.json
├── usage_log.jsonl                 # per-run token/cost log
├── reports/
│   ├── frontier/<date>.md
│   └── trends/<date>.md
├── docs/index.html                 # the dashboard (GitHub Pages)
├── requirements.txt
└── .github/workflows/run-agent.yml # on-demand only — no schedule trigger
```

## How a run works

1. You open the dashboard's **Home** tab. It shows what's currently tracked
   per track, then lets you configure this run: which track(s) to include,
   which currently-tracked topics to skip re-checking this time (unchecked =
   skipped, not deleted — it stays tracked), and optional free-text extra
   focus ("also check on fusion energy startups this run").
2. Clicking **Run** calls GitHub's `workflow_dispatch` API with those
   choices as inputs, then polls the resulting Actions run for step-by-step
   progress, shown live on the page.
3. On GitHub's infrastructure, `agent.py` runs the selected track(s): loads
   that track's memory → asks Claude (Sonnet 5, with the `web_search` tool)
   to research, respecting your exclusions/focus for this run → diffs the
   response against memory (new / updated / nothing to report) → updates
   memory → writes `reports/<track>/<date>.md` → appends a line to
   `usage_log.jsonl` with exact token/search counts and cost.
4. The workflow commits all of that back to the repo. The dashboard's
   **History**, **Usage / Cost**, and Home-tab status all reflect it once
   the run completes.

Memory schema (same shape in both `frontier_memory.json` and
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

Everything (memory, reports, usage log) is committed to git by the workflow,
so `git log` on any of these files is a changelog of what the agent learned
and spent, run by run.

## One-time setup

### 1. Get an Anthropic API key

1. Go to [platform.claude.com](https://platform.claude.com) and sign in (or create an account).
2. Go to **Settings → API Keys** and create a new key.
3. Copy it. You'll add it as a repo secret in step 3 — it's used server-side, by the GitHub Actions run itself, never by the dashboard directly. Make sure billing/credits are set up on the account.

### 2. Enable GitHub Pages

Repo → **Settings → Pages** → under **Build and deployment**, set **Source**
to **Deploy from a branch**, **Branch** to `main` / `/docs`, then **Save**.
The dashboard will be live within a minute or two at
**`https://rharoo20.github.io/research-agent/`**.

### 3. Add the one repo secret

Repo → **Settings → Secrets and variables → Actions → New repository
secret**:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | The key from step 1 |

That's the only one. There's no email integration anymore, so nothing else
is needed server-side.

### 4. Create a GitHub token for the dashboard

Go to
[github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new)
and create a **fine-grained personal access token** scoped to **only this
repository**, with Repository permissions:

- **Contents: Read and write** — saving settings/prompts/report edits.
- **Actions: Read and write** — triggering a run and reading its progress.

Open the dashboard → **Settings** tab → paste it into **GitHub access** →
**Save token**. It's stored only in that browser's local storage — never
sent to me, never committed, never visible in the page's source.

### 5. (Optional) Add an Anthropic key for chat

The **History** tab's "ask Claude about this report" feature calls
`api.anthropic.com` directly from your browser, using a key you provide —
this is separate from the server-side key in step 1/3. Get one the same way
as step 1 (a second key is fine, or reuse the same one), then paste it into
the dashboard's **Settings** tab → **Anthropic API key**. Skip this if you
don't want the chat feature; everything else works without it.

**Be clear-eyed about what this means:** this uses Anthropic's supported
"direct browser access" mode specifically for cases like this, but a key
sitting in browser local storage is a real credential exposed to that
browser — anyone with access to the browser profile could read and use it.
Fine for a personal tool on your own machine; don't do this on a shared
computer.

### 6. Do a first run

Open the dashboard, go to **Home**, leave the defaults (both tracks, no
exclusions), click **Run**, and watch it complete. Check **History** and
**Usage / Cost** afterward to confirm reports and cost logging landed.

## The dashboard, tab by tab

### Home

Per-track status (tracked count, `emerging → mainstream` breakdown), the
run configuration (track selection, topic exclusion checklist, extra focus
text), the **Run** button, and live step-by-step progress once a run starts.

### History

Every past report, per track. Click one to read it, and — if you added an
Anthropic key — chat with Claude about it underneath: ask questions, or ask
it to rewrite something. If Claude's reply includes a full revised version
(in a code block), a **Save this as the new report version** button appears
so you can commit that edit back — nothing gets overwritten without you
explicitly clicking save.

### Usage / Cost

Total spend, spend per run, and a per-run log (tokens, cached tokens, web
searches, cost) read straight from `usage_log.jsonl`. This is exact, not
estimated — `agent.py` logs real `usage` figures from each API response.

### Settings

- The two browser-local keys (Anthropic for chat, GitHub for everything
  else).
- Per-track tunables — max new items, max updates, `effort`
  (low/medium/high), max web searches per run — writing to `config.json`.
- The two track prompts (`prompts/frontier.md` / `prompts/trends.md`),
  editable directly. Keep the `<<...>>` placeholders intact — see the
  in-page hint for which ones matter.

## Cost

Two separate things now:

**A run.** Same Claude Sonnet 5 + `web_search` pipeline as before, priced at
$2/$10 per million input/output tokens and $10 per 1,000 searches (current
published pricing, checked 2026-08). What changed since the first version:
`effort` now defaults to `medium` instead of `high`, the search cap defaults
to 8 instead of 15, and the initial prompt is cache-enabled — a run that
needs multiple search rounds now re-reads that cached prefix at ~10% of
input price instead of rebilling it from scratch each time. All three
(effort, search cap, and whether caching helps) are visible per-run in
**Usage / Cost**, so real cost is always checkable instead of estimated.
The one thing that hasn't changed: cost now only happens when you click
**Run** — no unattended schedule spending in the background.

**Chat.** Genuinely open-ended, since you control how much you use it.
Defaults to Haiku 4.5 ($1/$5 per MTok — a fraction of Sonnet 5's cost), with
a dropdown to switch to Sonnet 5 per-conversation if a question needs it.

## Editing a track's research focus

Edit `prompts/frontier.md` or `prompts/trends.md` directly (in the repo or
via the dashboard's Settings tab) — e.g. to add/remove preferred sources,
change what counts as noise, or shift the focus. `<<MEMORY_JSON>>`,
`<<TODAY>>`, `<<MAX_NEW>>`, `<<MAX_UPDATES>>`, `<<EXTRA_FOCUS_BLOCK>>`,
`<<STATUS_ANCHORS>>`, and `<<OUTPUT_SCHEMA>>` are all substituted at run
time — keep them if you want that data available to the model.
`<<STATUS_ANCHORS>>` and `<<OUTPUT_SCHEMA>>` pull from fixed constants in
`agent.py` rather than being freely editable text, since the latter in
particular defines the exact JSON contract `agent.py` depends on to parse
Claude's response — a corrupted version of it would break every run. If you
accidentally delete the `<<OUTPUT_SCHEMA>>` placeholder itself, `agent.py`
appends the schema instructions anyway as a safety net, but the other
placeholders have no such fallback.

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

## Running `agent.py` locally

Useful for testing changes without going through the dashboard/Actions:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python agent.py                       # both tracks, no exclusions/focus
RUN_TRACKS=frontier python agent.py   # just one track
FRONTIER_EXCLUDE_IDS="some-id,another-id" \
FRONTIER_EXTRA_FOCUS="also check on X" \
  python agent.py
```

Env vars `agent.py` reads: `ANTHROPIC_API_KEY` (required), `RUN_TRACKS`
(`both` / `frontier` / `trends`, default `both`), and per track
`{TRACK}_EXCLUDE_IDS` / `{TRACK}_EXTRA_FOCUS` (both optional) — these are
exactly what the dashboard's Run button sets as `workflow_dispatch` inputs.
