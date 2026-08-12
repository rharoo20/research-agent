"""
On-demand research agent — two independent tracks:
  - "frontier": emerging tech/research with mainstream/commercial potential
  - "trends":   early-stage consumer/cultural/market trends

Triggered manually (via the dashboard's Run button -> GitHub Actions
workflow_dispatch, or `python agent.py` locally) — no autonomous schedule.

Each run: load the selected track(s)' memory -> ask Claude to research using
web_search, respecting any per-run topic exclusions and extra focus text ->
diff findings against memory (new / updated / skip) -> update memory -> write
a Markdown report file -> append per-run token/cost usage to usage_log.jsonl.
"""

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import anthropic

MODEL = "claude-sonnet-5"
CONFIG_FILE = "config.json"
USAGE_LOG_FILE = "usage_log.jsonl"
PROMPTS_DIR = "prompts"
DEFAULT_TRACK_CONFIG = {"max_new": 5, "max_updates": 8, "effort": "medium", "max_searches": 8}

# Claude Sonnet 5 pricing (per platform.claude.com/docs/en/about-claude/pricing,
# checked 2026-08). Update these if pricing changes.
PRICE_INPUT_PER_MTOK = 2.00
PRICE_OUTPUT_PER_MTOK = 10.00
PRICE_CACHE_READ_PER_MTOK = 0.20
PRICE_CACHE_WRITE_5M_PER_MTOK = 2.50
PRICE_PER_1000_SEARCHES = 10.00

# Structural — deliberately NOT dashboard/prompt-file editable. The output
# schema is what extract_json()/merge_memory() depend on; a hand-edited
# version of this could silently break parsing.
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

TRACKS = {
    "frontier": {
        "label": "Frontier",
        "memory_file": "frontier_memory.json",
        "report_dir": "reports/frontier",
        "prompt_file": f"{PROMPTS_DIR}/frontier.md",
    },
    "trends": {
        "label": "Trends",
        "memory_file": "trends_memory.json",
        "report_dir": "reports/trends",
        "prompt_file": f"{PROMPTS_DIR}/trends.md",
    },
}


def load_config() -> dict:
    """Non-secret, dashboard-editable tunables (per-track item caps, effort, search
    cap). Falls back to defaults for anything missing so a malformed/partial
    config.json can't crash a run."""
    p = Path(CONFIG_FILE)
    data = {}
    if p.exists():
        with p.open() as f:
            data = json.load(f)
    return {
        track_key: {**DEFAULT_TRACK_CONFIG, **data.get(track_key, {})}
        for track_key in TRACKS
    }


def load_prompt_template(path: str) -> str:
    return Path(path).read_text()


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


def memory_for_prompt(memory: list, exclude_ids: set) -> str:
    """Compact view of memory for the prompt: recent update_log entries only, to
    keep the request small as memory grows over months. Excluded ids stay
    visible (marked excluded_this_run) rather than hidden, so the model doesn't
    accidentally re-add them as new under a different id if it encounters them."""
    compact = []
    for entry in memory:
        item = {
            "id": entry["id"],
            "topic": entry["topic"],
            "status": entry["status"],
            "last_update": entry["last_update"],
            "summary": entry["summary"],
            "recent_updates": entry.get("update_log", [])[-3:],
        }
        if entry["id"] in exclude_ids:
            item["excluded_this_run"] = True
        compact.append(item)
    return json.dumps(compact, indent=2)


def build_prompt(
    template: str,
    memory: list,
    today: str,
    max_new: int,
    max_updates: int,
    exclude_ids: set,
    extra_focus: str,
) -> str:
    extra_block = (
        f"ADDITIONAL FOCUS FOR THIS RUN (from the user, optional — treat as a steer, "
        f"not a replacement for the normal brief): {extra_focus}\n"
        if extra_focus
        else ""
    )

    # STATUS_ANCHORS/OUTPUT_SCHEMA go in first — OUTPUT_SCHEMA's example fields
    # contain their own <<TODAY>> markers, so the variable substitutions below
    # must run on the fully-assembled text in one pass, or those inner markers
    # would never get filled in.
    text = template.replace("<<EXTRA_FOCUS_BLOCK>>", extra_block).replace(
        "<<STATUS_ANCHORS>>", STATUS_ANCHORS
    )
    if "<<OUTPUT_SCHEMA>>" in text:
        text = text.replace("<<OUTPUT_SCHEMA>>", OUTPUT_SCHEMA)
    else:
        # Safety net: if a hand-edited prompt file dropped this placeholder, the
        # response would be unparseable — always append the schema regardless.
        text = text.rstrip() + "\n\n" + OUTPUT_SCHEMA

    return (
        text.replace("<<MEMORY_JSON>>", memory_for_prompt(memory, exclude_ids))
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


def run_research(
    client: anthropic.Anthropic, prompt: str, max_searches: int, effort: str
) -> tuple[dict, dict]:
    # cache_control on the (large, static-per-run) initial prompt means a
    # pause_turn resend re-reads it at ~10% of input price instead of rebilling
    # the whole thing at full price.
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}
            ],
        }
    ]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]

    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "web_search_requests": 0,
        "api_calls": 0,
    }

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            tools=tools,
            messages=messages,
        )

        usage["api_calls"] += 1
        usage["input_tokens"] += response.usage.input_tokens
        usage["output_tokens"] += response.usage.output_tokens
        usage["cache_read_input_tokens"] += response.usage.cache_read_input_tokens or 0
        usage["cache_creation_input_tokens"] += response.usage.cache_creation_input_tokens or 0
        if response.usage.server_tool_use:
            usage["web_search_requests"] += (
                response.usage.server_tool_use.web_search_requests or 0
            )

        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        break

    final_text = "".join(
        block.text for block in response.content if block.type == "text"
    )
    return extract_json(final_text), usage


def usage_cost_usd(usage: dict) -> float:
    return (
        usage["input_tokens"] * PRICE_INPUT_PER_MTOK / 1_000_000
        + usage["output_tokens"] * PRICE_OUTPUT_PER_MTOK / 1_000_000
        + usage["cache_read_input_tokens"] * PRICE_CACHE_READ_PER_MTOK / 1_000_000
        + usage["cache_creation_input_tokens"] * PRICE_CACHE_WRITE_5M_PER_MTOK / 1_000_000
        + usage["web_search_requests"] * PRICE_PER_1000_SEARCHES / 1000
    )


def log_usage(track_key: str, today: str, usage: dict, cost: float) -> None:
    entry = {
        "date": today,
        "track": track_key,
        "api_calls": usage["api_calls"],
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_read_input_tokens": usage["cache_read_input_tokens"],
        "cache_creation_input_tokens": usage["cache_creation_input_tokens"],
        "web_search_requests": usage["web_search_requests"],
        "cost_usd": round(cost, 4),
    }
    with Path(USAGE_LOG_FILE).open("a") as f:
        f.write(json.dumps(entry) + "\n")


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


def write_report_file(report_dir: str, today: str, markdown_text: str) -> Path:
    out_dir = Path(report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today}.md"
    out_path.write_text(markdown_text)
    return out_path


def run_track(
    client: anthropic.Anthropic,
    track_key: str,
    config: dict,
    tunables: dict,
    today: str,
    exclude_ids: set,
    extra_focus: str,
) -> float:
    print(f"[{track_key}] loading memory from {config['memory_file']}")
    memory = load_memory(config["memory_file"])

    max_new = tunables["max_new"]
    max_updates = tunables["max_updates"]
    effort = tunables["effort"]
    max_searches = tunables["max_searches"]

    if exclude_ids:
        print(f"[{track_key}] excluding this run: {', '.join(sorted(exclude_ids))}")
    if extra_focus:
        print(f"[{track_key}] extra focus: {extra_focus}")

    print(f"[{track_key}] researching via Claude + web_search (effort={effort}, max_searches={max_searches})...")
    template = load_prompt_template(config["prompt_file"])
    prompt = build_prompt(
        template, memory, today, max_new, max_updates, exclude_ids, extra_focus
    )
    parsed, usage = run_research(client, prompt, max_searches, effort)
    cost = usage_cost_usd(usage)
    log_usage(track_key, today, usage, cost)
    print(
        f"[{track_key}] usage: {usage['api_calls']} API call(s), "
        f"{usage['input_tokens']} input tok ({usage['cache_read_input_tokens']} cached), "
        f"{usage['output_tokens']} output tok, "
        f"{usage['web_search_requests']} web search(es) -> ${cost:.4f}"
    )

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

    return cost


def parse_ids(raw: str) -> set:
    return {part.strip() for part in raw.split(",") if part.strip()}


def main() -> None:
    required_env = ["ANTHROPIC_API_KEY"]
    missing = [name for name in required_env if not os.environ.get(name)]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    run_tracks_raw = os.environ.get("RUN_TRACKS", "both").strip().lower()
    if run_tracks_raw in ("", "both"):
        selected = list(TRACKS.keys())
    elif run_tracks_raw in TRACKS:
        selected = [run_tracks_raw]
    else:
        print(
            f"Unrecognized RUN_TRACKS value: {run_tracks_raw!r} "
            f"(expected 'frontier', 'trends', or 'both')",
            file=sys.stderr,
        )
        sys.exit(1)

    client = anthropic.Anthropic()
    today = date.today().isoformat()
    tunables = load_config()

    total_cost = 0.0
    for track_key in selected:
        config = TRACKS[track_key]
        exclude_ids = parse_ids(os.environ.get(f"{track_key.upper()}_EXCLUDE_IDS", ""))
        extra_focus = os.environ.get(f"{track_key.upper()}_EXTRA_FOCUS", "").strip()
        total_cost += run_track(
            client, track_key, config, tunables[track_key], today, exclude_ids, extra_focus
        )

    print(f"[total] estimated Anthropic API cost this run: ${total_cost:.4f}")


if __name__ == "__main__":
    main()
