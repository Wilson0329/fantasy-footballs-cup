#!/usr/bin/env python3
"""
build_news_data.py
Generates Sky Sports pundit commentary for news_data.json using GitHub Models (gpt-4o-mini).
Requires GITHUB_TOKEN environment variable (automatically present in GitHub Actions).

Data sources:
  - docs/league_data.json + docs/cup_data.json  (pre-built)
  - FPL bootstrap-static API  (player injury/availability flags)
  - FPL entry picks API       (which managers own which flagged players)
  - BBC Sport Premier League RSS  (latest news headlines and injury rumours)
"""
import json, os, sys, time, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from openai import OpenAI

FPL_BASE = "https://fantasy.premierleague.com/api"
BBC_PL_RSS = "https://feeds.bbci.co.uk/sport/football/premier-league/rss.xml"
HEADERS = {"User-Agent": "fantasy-football-cup/1.0 (personal league tracker)"}

STATUS_LABELS = {
    "d": "DOUBTFUL",
    "i": "INJURED",
    "s": "SUSPENDED",
    "u": "UNAVAILABLE",
    "n": "NOT AVAILABLE",
}

PUNDITS = [
    {
        "id": "title_race",
        "name": "Gary Neville",
        "image": "neville.jpg",
        "role": "Sky Sports analyst, ex-Manchester United captain",
        "personality": (
            "Analytical and composed, with flashes of passion. Uses phrases like 'brilliant', "
            "'nailed on', 'I genuinely believe'. Tactical, authoritative, occasionally dramatic."
        ),
        "topic": "the title race and the fight for the top 3 payout positions",
    },
    {
        "id": "relegation",
        "name": "Roy Keane",
        "image": "keane.jpg",
        "role": "Sky Sports pundit, ex-Manchester United and Ireland captain",
        "personality": (
            "Brutally blunt, no sympathy, no nonsense. Short punchy sentences. Uses phrases like "
            "'I'm not surprised', 'not good enough', 'no excuses'. Never complimentary to poor performers."
        ),
        "topic": "the relegation battle — the bottom two teams fighting to survive",
    },
    {
        "id": "cup_final",
        "name": "Jamie Carragher",
        "image": "carragher.jpg",
        "role": "Sky Sports pundit, ex-Liverpool defender",
        "personality": (
            "Passionate, direct, occasionally self-deprecating. Uses phrases like 'I'll be honest with ya', "
            "'massive occasion', 'what a tie'. Northern English energy. Talks with real conviction."
        ),
        "topic": "the cup final — who is in it, how they got there, and what to expect",
    },
    {
        "id": "form_injuries",
        "name": "Micah Richards",
        "image": "richards.jpg",
        "role": "Sky Sports pundit, ex-Manchester City defender",
        "personality": (
            "Enthusiastic, warm, upbeat. Uses phrases like 'unbelievable', 'give him some credit', "
            "'look at that form!'. Laughs easily. Very energetic and positive, occasionally surprised."
        ),
        "topic": (
            "current form — the hottest and coldest managers over the last 5 gameweeks — "
            "and how injuries to key players in squads could affect the run-in"
        ),
    },
    {
        "id": "bench_captain",
        "name": "Paul Merson",
        "image": "merson.jpg",
        "role": "Sky Sports pundit, ex-Arsenal and England forward",
        "personality": (
            "Chaotic, hyperbolic, slightly self-deprecating. Uses phrases like 'I tell ya what', "
            "'honestly mate', 'I'd have done the same thing'. Bewildered by bad decisions but sympathetic."
        ),
        "topic": "bench points left uncollected and captaincy decisions — the pain and the glory",
    },
]


# ─── FPL data fetching ────────────────────────────────────────────────────────

def fetch_url(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_bootstrap() -> dict:
    return json.loads(fetch_url(f"{FPL_BASE}/bootstrap-static/"))


def fetch_picks(entry_id: int, gw: int) -> dict | None:
    try:
        return json.loads(fetch_url(f"{FPL_BASE}/entry/{entry_id}/event/{gw}/picks/", timeout=10))
    except Exception as e:
        print(f"    picks({entry_id}, gw{gw}): {e}", file=sys.stderr)
        return None


# ─── BBC Sport RSS ────────────────────────────────────────────────────────────

def fetch_pl_news(max_items: int = 12) -> list[str]:
    """Fetch Premier League headlines + descriptions from BBC Sport RSS."""
    try:
        raw = fetch_url(BBC_PL_RSS, timeout=12)
        root = ET.fromstring(raw)
        headlines = []
        for item in root.findall(".//item")[:max_items]:
            title = (item.findtext("title") or "").strip()
            desc  = (item.findtext("description") or "").strip()
            if not title:
                continue
            entry = title
            if desc and len(desc) > 15:
                # Keep description brief but informative
                short_desc = desc[:150].rsplit(" ", 1)[0]
                entry = f"{title} — {short_desc}"
            headlines.append(entry)
        return headlines
    except Exception as e:
        print(f"  WARNING: Could not fetch BBC Sport RSS: {e}", file=sys.stderr)
        return []


# ─── Context assembly ─────────────────────────────────────────────────────────

def build_context(league: dict, cup: dict) -> tuple[str, int]:
    gw        = league["metadata"]["current_gw"]
    gws_left  = 38 - gw
    standings = league["standings"]
    form_map  = {e["entry_id"]: e for e in league["form"]}

    # ── FPL injury / availability data ──
    print("  Fetching FPL bootstrap...")
    bootstrap   = fetch_bootstrap()
    teams_map   = {t["id"]: t["short_name"] for t in bootstrap["teams"]}

    # Players with any flag: status != 'a' OR news text present
    flagged = {}
    for p in bootstrap["elements"]:
        if p.get("status", "a") != "a" or p.get("news"):
            flagged[p["id"]] = {
                "name":   p["web_name"],
                "status": p.get("status", "a"),
                "news":   (p.get("news") or "").strip(),
                "team":   teams_map.get(p["team"], ""),
            }
    print(f"    {len(flagged)} flagged players found")

    # Squad alerts: which managers own flagged players
    print("  Fetching manager squads...")
    squad_alerts = []
    for s in standings:
        time.sleep(0.25)  # be polite to the FPL API
        picks_data = fetch_picks(s["entry_id"], gw)
        if not picks_data:
            continue
        alerts = []
        for pick in picks_data.get("picks", []):
            pid = pick["element"]
            if pid not in flagged:
                continue
            p   = flagged[pid]
            pos = "starting XI" if pick["position"] <= 11 else "bench"
            cap = " [CAPTAIN]" if pick.get("is_captain") else (" [VICE-CAPTAIN]" if pick.get("is_vice_captain") else "")
            label = STATUS_LABELS.get(p["status"], p["status"].upper())
            news_str = f" — {p['news']}" if p["news"] else ""
            alerts.append(f"{p['name']} ({p['team']}) [{label}] [{pos}]{cap}{news_str}")
        if alerts:
            squad_alerts.append((s["manager"], s["name"], alerts))
    print(f"    {len(squad_alerts)} managers with flagged players in squad")

    # ── BBC Sport news ──
    print("  Fetching BBC Sport Premier League news...")
    news_headlines = fetch_pl_news()
    print(f"    {len(news_headlines)} headlines fetched")

    # ── Assemble context string ──
    lines = [
        f"FANTASY FOOTBALL LEAGUE — After Gameweek {gw} ({gws_left} gameweeks remaining in the season)",
        "",
        "LEAGUE RULES:",
        "  - Bottom 2 teams are relegated at the end of the season.",
        "  - Top 3 teams win prize payouts (1st, 2nd, 3rd place).",
        "  - There is a separate cup competition running alongside the league.",
        "",
        "CURRENT STANDINGS:",
    ]
    for s in standings:
        fd    = form_map.get(s["entry_id"], {})
        arrow = "↑" if s["rank"] < s["rank_last"] else ("↓" if s["rank"] > s["rank_last"] else "→")
        lines.append(
            f"  {s['rank']:2}. {s['name']} ({s['manager']}) — {s['total_points']} pts {arrow}"
            f" | GW{gw}: {s['gw_points']} pts | last-5 avg: {fd.get('form_avg','?')}"
            f" | last-5: {fd.get('last5_scores','?')}"
        )

    lines += ["", "BENCH POINTS WASTED THIS SEASON:"]
    for b in sorted(league["bench_points"], key=lambda x: -x["total_bench_points"]):
        lines.append(f"  {b['manager']}: {b['total_bench_points']} pts total, {b['avg_bench_per_gw']} avg/GW")

    lines += ["", "CAPTAIN HIT RATE:"]
    for c in sorted(league["captain_hit_rate"], key=lambda x: -x["hit_rate"]):
        lines.append(f"  {c['manager']}: {c['hit_rate']}% ({c['captain_hits']}/{c['gws_played']} GWs)")

    final   = cup["knockout"]["final"]
    mgr_cup = {6366909: "Dominic Byrne", 4789233: "Jason Knightly"}
    mgr_a   = mgr_cup.get(final["team_a"]["entry_id"], "")
    mgr_b   = mgr_cup.get(final["team_b"]["entry_id"], "")
    lines  += [
        "",
        f"CUP FINAL: {final['team_a']['name']} ({mgr_a}) vs {final['team_b']['name']} ({mgr_b})"
        f" — GW{final['gw']} (status: {final.get('status','upcoming')})",
    ]
    if final.get("score_a") is not None:
        winner = (final.get("winner") or {}).get("name", "TBD")
        lines.append(
            f"  Result: {final['team_a']['name']} {final['score_a']} – "
            f"{final['score_b']} {final['team_b']['name']} | Winner: {winner}"
        )

    if squad_alerts:
        lines += ["", "PLAYER INJURY & AVAILABILITY ALERTS IN MANAGERS' SQUADS:"]
        for manager, team_name, alerts in squad_alerts:
            lines.append(f"  {manager} ({team_name}):")
            for a in alerts:
                lines.append(f"    - {a}")

    if news_headlines:
        lines += [
            "",
            "LATEST PREMIER LEAGUE NEWS, INJURIES & RUMOURS (BBC Sport — today):",
        ]
        for h in news_headlines:
            lines.append(f"  • {h}")

    return "\n".join(lines), gw


# ─── LLM generation ──────────────────────────────────────────────────────────

def generate_article(client: OpenAI, pundit: dict, context: str, gw: int) -> dict:
    system_prompt = (
        f"You are {pundit['name']}, {pundit['role']}.\n"
        f"Personality: {pundit['personality']}\n\n"
        "Write exactly 2 short paragraphs of pundit commentary. Rules:\n"
        "- No headers, no bullet points, no emojis, no markdown.\n"
        "- Use first person. Reference managers by first name only.\n"
        "- Be opinionated, specific, and entertaining. Use real numbers and names from the data.\n"
        "- Where relevant, weave in injury news or real-world football headlines naturally.\n"
        "- Each paragraph: 2–4 sentences. Total: 80–130 words.\n"
        "- Write entirely in your distinctive voice."
    )

    user_prompt = (
        f"Give your pundit verdict on {pundit['topic']} based on the league data and "
        f"football news below. Be direct and in character. Gameweek {gw}.\n\n{context}"
    )

    body_resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        max_tokens=240,
        temperature=0.85,
    )
    body = body_resp.choices[0].message.content.strip()

    head_resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Write a punchy Sky Sports style headline for this pundit commentary. "
                    "Max 8 words. No emojis. No quotes. No full stop at the end."
                ),
            },
            {"role": "user", "content": body},
        ],
        max_tokens=25,
        temperature=0.7,
    )
    headline = head_resp.choices[0].message.content.strip().strip('"').strip("'")

    return {
        "id":           pundit["id"],
        "pundit_name":  pundit["name"],
        "pundit_image": pundit["image"],
        "headline":     headline,
        "body":         body,
    }


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: GITHUB_TOKEN environment variable not set", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(
        base_url="https://models.inference.ai.azure.com",
        api_key=token,
    )

    base_dir = os.path.dirname(os.path.abspath(__file__))
    docs_dir = os.path.join(base_dir, "docs")

    with open(os.path.join(docs_dir, "league_data.json")) as f:
        league = json.load(f)
    with open(os.path.join(docs_dir, "cup_data.json")) as f:
        cup = json.load(f)

    print("Building context...")
    context, gw = build_context(league, cup)
    print(f"Context ready ({len(context)} chars)\n")

    articles = []
    for pundit in PUNDITS:
        print(f"Generating: {pundit['name']} ({pundit['id']})...")
        try:
            article = generate_article(client, pundit, context, gw)
            articles.append(article)
            print(f"  Headline: {article['headline']}")
        except Exception as e:
            print(f"  WARNING: Failed for {pundit['name']}: {e}", file=sys.stderr)

    news_data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "gw":           gw,
        "articles":     articles,
    }

    out_path = os.path.join(docs_dir, "news_data.json")
    with open(out_path, "w") as f:
        json.dump(news_data, f, indent=2)

    print(f"\nDone — news_data.json written ({len(articles)} articles, GW{gw})")


if __name__ == "__main__":
    main()
