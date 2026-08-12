"""
Weekly research agent — two independent tracks:
  - "frontier": emerging tech/research with mainstream/commercial potential
  - "trends":   early-stage consumer/cultural/market trends

Each run: load that track's memory -> ask Claude to research the week using
web_search -> diff findings against memory (new / updated / skip) -> update
memory -> write a Markdown report file -> email a summary (skipped if there's
nothing to report).
"""

import html
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import anthropic
import resend

RESEND_FROM_ADDRESS = "onboarding@resend.dev"

MODEL = "claude-sonnet-5"
CONFIG_FILE = "config.json"
DEFAULT_TRACK_CONFIG = {"max_new": 5, "max_updates": 8}

STATUS_ANCHORS = """STATUS DEFINITIONS (use these anchors consistently; do not invent your own scale):
- emerging: first credible signal spotted; a single source or a small cluster; no measurable momentum yet.
- developing: multiple independent sources now covering it within weeks of each other; early funding, a product launch, or a visible growth curve.
- maturing: picked up by mid-tier trade/tech press or a recognizable lab/brand; clear commercial moves (partnerships, funding rounds, competitor response).
- mainstream: covered by major consumer press, a top app-store/retail category, or a Fortune-500-scale product decision."""

OUTPUT_SCHEMA = """Respond with ONLY a single JSON object in exactly this shape, and nothing else \
— no markdown code fences, no explanation before or after, no trailing commentary:

{
  "new_entries": [
    {
      "id": "short-kebab-case-slug",
      "first_seen": "<<TODAY>>",
      "topic": "short description",
      "status": "emerging | developing | maturing | mainstream",
      "last_update": "<<TODAY>>",
      "summary": "one-line summary",
      "why_it_matters": "why this is commercially interesting or worth watching",
      "update_log": ["<<TODAY>>: <first note>"]
    }
  ],
  "updated_entries": [
    {
      "id": "<must exactly match an id from CURRENTLY TRACKED below>",
      "status": "emerging | developing | maturing | mainstream",
      "last_update": "<<TODAY>>",
      "update_note": "one-line dated note on what developed this week"
    }
  ]
}"""

FRONTIER_PROMPT = f"""You are the research analyst for a personal "Frontier" tracker. Your job: \
surface emerging technology and research breakthroughs that could plausibly break into \
mainstream awareness or become a real business — a new AI model or technique, a \
materials-science breakthrough, a novel scientific method, and similar.

Lean your search toward arXiv, bioRxiv, official research-lab announcements and blogs \
(e.g. OpenAI, DeepMind, Anthropic, Meta AI, national labs, university labs), serious \
technical press (not consumer tech blogs), and patent filings. Skip anything that's purely \
consumer news, purely hype with no technical substance, or a rehash of something already \
widely reported.

Use the web_search tool to research this week's frontier. Aim for real signal: multiple \
corroborating sources, concrete technical claims, or a named team/lab/paper — not speculation.

{STATUS_ANCHORS}

Below is everything already being tracked (JSON array). For each thing you find this week:
- If it is NOT in this list and is a real, well-evidenced finding: add it as a new_entries item.
- If it IS in this list and there's a genuinely new development (momentum growing, funding, a \
competitor move, going more mainstream, a status change): add it as an updated_entries item \
with a one-line update_note describing what changed. Do not re-report it as new.
- If it IS in this list with nothing new to report: leave it out entirely. Do not force an update.

Cap yourself at <<MAX_NEW>> new_entries and <<MAX_UPDATES>> updated_entries. If more genuinely \
qualify, keep only the strongest signals.

CURRENTLY TRACKED (JSON):
<<MEMORY_JSON>>

{OUTPUT_SCHEMA}"""

TRENDS_PROMPT = f"""You are the research analyst for a personal "Cultural/Market Trends" \
tracker. Your job: surface consumer, cultural, or market trends still in their early phase — \
a food/product craze just starting to gain traction (like matcha before it exploded), a shift \
in consumer behavior, a niche community interest that's growing — before they hit mainstream \
media.

Lean your search toward social-platform discussion, niche subreddits/forums, small-business \
and retail trade press, and momentum language ("before it went viral", "growing demand", \
"selling out", "waitlist"). You cannot query Reddit/TikTok/Google-Trends APIs directly — \
you're working from what's publicly indexed about them via web search — so prefer sources \
with concrete numbers, dates, or named businesses over vague vibes.

Use the web_search tool to research this week's early trends. Aim for real signal: a trend \
someone could act on commercially, not a one-off viral moment with no legs.

{STATUS_ANCHORS}

Below is everything already being tracked (JSON array). For each thing you find this week:
- If it is NOT in this list and is a real, well-evidenced finding: add it as a new_entries item.
- If it IS in this list and there's a genuinely new development (momentum growing, funding, a \
competitor move, going more mainstream, a status change): add it as an updated_entries item \
with a one-line update_note describing what changed. Do not re-report it as new.
- If it IS in this list with nothing new to report: leave it out entirely. Do not force an update.

Cap yourself at <<MAX_NEW>> new_entries and <<MAX_UPDATES>> updated_entries. If more genuinely \
qualify, keep only the strongest signals.

CURRENTLY TRACKED (JSON):
<<MEMORY_JSON>>

{OUTPUT_SCHEMA}"""

TRACKS = {
    "frontier": {
        "label": "Frontier",
        "memory_file": "frontier_memory.json",
        "report_dir": "reports/frontier",
        "email_subject": "Weekly Frontier Report",
        "prompt_template": FRONTIER_PROMPT,
    },
    "trends": {
        "label": "Trends",
        "memory_file": "trends_memory.json",
        "report_dir": "reports/trends",
        "email_subject": "Weekly Trends Report",
        "prompt_template": TRENDS_PROMPT,
    },
}


def load_config() -> dict:
    """Non-secret, dashboard-editable tunables (per-track item caps). Falls back to
    defaults for anything missing so a malformed/partial config.json can't crash a run."""
    p = Path(CONFIG_FILE)
    data = {}
    if p.exists():
        with p.open() as f:
            data = json.load(f)
    return {
        track_key: {**DEFAULT_TRACK_CONFIG, **data.get(track_key, {})}
        for track_key in TRACKS
    }


def load_memory(path: str) -> list:
    p = Path(path)
    if not p.exists():
        return []
    with p.open() as f:
        return json.load(f)


def save_memory(path: str, memory: list) -> None:
    with Path(path).open("w") as f:
        json.dump(memory, f, indent=2)
        f.write("\n")


def memory_for_prompt(memory: list) -> str:
    """Compact view of memory for the prompt: recent update_log entries only,
    to keep the request small as memory grows over months."""
    compact = []
    for entry in memory:
        compact.append(
            {
                "id": entry["id"],
                "topic": entry["topic"],
                "status": entry["status"],
                "last_update": entry["last_update"],
                "summary": entry["summary"],
                "recent_updates": entry.get("update_log", [])[-3:],
            }
        )
    return json.dumps(compact, indent=2)


def build_prompt(template: str, memory: list, today: str, max_new: int, max_updates: int) -> str:
    return (
        template.replace("<<MEMORY_JSON>>", memory_for_prompt(memory))
        .replace("<<TODAY>>", today)
        .replace("<<MAX_NEW>>", str(max_new))
        .replace("<<MAX_UPDATES>>", str(max_updates))
    )


def extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def run_research(client: anthropic.Anthropic, prompt: str) -> dict:
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 15}]

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        break

    final_text = "".join(
        block.text for block in response.content if block.type == "text"
    )
    return extract_json(final_text)


def merge_memory(memory: list, parsed: dict, today: str, max_new: int, max_updates: int):
    by_id = {entry["id"]: entry for entry in memory}

    report_new = []
    for entry in parsed.get("new_entries", [])[:max_new]:
        entry.setdefault("first_seen", today)
        entry.setdefault("last_update", today)
        entry.setdefault("update_log", [f"{today}: {entry.get('summary', '')}"])
        if entry["id"] in by_id:
            # model mistakenly re-reported a tracked id as new — treat as update instead
            continue
        by_id[entry["id"]] = entry
        report_new.append(entry)

    report_updates = []
    for update in parsed.get("updated_entries", [])[:max_updates]:
        target = by_id.get(update["id"])
        if target is None:
            # model referenced an id that doesn't exist — skip rather than corrupt memory
            continue
        target["status"] = update.get("status", target["status"])
        target["last_update"] = update.get("last_update", today)
        note = update.get("update_note", "").strip()
        if note:
            target.setdefault("update_log", []).append(f"{today}: {note}")
        report_updates.append(
            {
                "id": target["id"],
                "topic": target["topic"],
                "status": target["status"],
                "update_note": note,
            }
        )

    merged = list(by_id.values())
    return merged, report_new, report_updates


def render_markdown(label: str, today: str, new_entries: list, updates: list) -> str:
    lines = [f"# Weekly {label} Report — {today}", ""]

    lines.append("## New this week")
    lines.append("")
    if new_entries:
        for e in new_entries:
            lines.append(f"### {e['topic']} ({e['status']})")
            lines.append("")
            lines.append(e["summary"])
            lines.append("")
            lines.append(f"**Why it matters:** {e['why_it_matters']}")
            lines.append("")
    else:
        lines.append("_Nothing new this week._")
        lines.append("")

    lines.append("## Updates on tracked signals")
    lines.append("")
    if updates:
        for u in updates:
            lines.append(f"- **{u['topic']}** ({u['status']}): {u['update_note']}")
        lines.append("")
    else:
        lines.append("_No updates on tracked signals this week._")
        lines.append("")

    return "\n".join(lines)


def render_html(label: str, today: str, new_entries: list, updates: list) -> str:
    def esc(s: str) -> str:
        return html.escape(s or "")

    parts = [
        f"<h2 style='font-family:sans-serif'>Weekly {esc(label)} Report — {esc(today)}</h2>",
        "<h3 style='font-family:sans-serif'>New this week</h3>",
    ]
    if new_entries:
        for e in new_entries:
            parts.append(
                "<div style='font-family:sans-serif;margin-bottom:16px'>"
                f"<b>{esc(e['topic'])}</b> <i>({esc(e['status'])})</i><br>"
                f"{esc(e['summary'])}<br>"
                f"<span style='color:#555'>Why it matters: {esc(e['why_it_matters'])}</span>"
                "</div>"
            )
    else:
        parts.append("<p style='font-family:sans-serif'><i>Nothing new this week.</i></p>")

    parts.append("<h3 style='font-family:sans-serif'>Updates on tracked signals</h3>")
    if updates:
        parts.append("<ul style='font-family:sans-serif'>")
        for u in updates:
            parts.append(
                f"<li><b>{esc(u['topic'])}</b> ({esc(u['status'])}): {esc(u['update_note'])}</li>"
            )
        parts.append("</ul>")
    else:
        parts.append(
            "<p style='font-family:sans-serif'><i>No updates on tracked signals this week.</i></p>"
        )

    return "\n".join(parts)


def write_report_file(report_dir: str, today: str, markdown_text: str) -> Path:
    out_dir = Path(report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today}.md"
    out_path.write_text(markdown_text)
    return out_path


def send_email(subject: str, html_body: str, plain_body: str) -> None:
    resend.api_key = os.environ["RESEND_API_KEY"]
    recipient = os.environ["RECIPIENT_EMAIL"]

    resend.Emails.send(
        {
            "from": RESEND_FROM_ADDRESS,
            "to": [recipient],
            "subject": subject,
            "html": html_body,
            "text": plain_body,
        }
    )


def run_track(
    client: anthropic.Anthropic,
    track_key: str,
    config: dict,
    tunables: dict,
    today: str,
) -> None:
    print(f"[{track_key}] loading memory from {config['memory_file']}")
    memory = load_memory(config["memory_file"])

    max_new = tunables["max_new"]
    max_updates = tunables["max_updates"]

    print(f"[{track_key}] researching via Claude + web_search...")
    prompt = build_prompt(config["prompt_template"], memory, today, max_new, max_updates)
    parsed = run_research(client, prompt)

    merged_memory, report_new, report_updates = merge_memory(
        memory, parsed, today, max_new, max_updates
    )
    save_memory(config["memory_file"], merged_memory)
    print(
        f"[{track_key}] {len(report_new)} new, {len(report_updates)} updated, "
        f"{len(merged_memory)} total tracked"
    )

    markdown_text = render_markdown(config["label"], today, report_new, report_updates)
    report_path = write_report_file(config["report_dir"], today, markdown_text)
    print(f"[{track_key}] report written to {report_path}")

    if not report_new and not report_updates:
        print(f"[{track_key}] nothing to report — skipping email")
        return

    html_body = render_html(config["label"], today, report_new, report_updates)
    subject = f"{config['email_subject']} — {today}"
    send_email(subject, html_body, markdown_text)
    print(f"[{track_key}] email sent")


def main() -> None:
    required_env = ["ANTHROPIC_API_KEY", "RESEND_API_KEY", "RECIPIENT_EMAIL"]
    missing = [name for name in required_env if not os.environ.get(name)]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic()
    today = date.today().isoformat()
    tunables = load_config()

    for track_key, config in TRACKS.items():
        run_track(client, track_key, config, tunables[track_key], today)


if __name__ == "__main__":
    main()
