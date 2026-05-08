#!/usr/bin/env python3
"""
build_news_data.py
Generates Sky Sports pundit commentary for news_data.json using GitHub Models (gpt-4o-mini).
Requires GITHUB_TOKEN environment variable (automatically present in GitHub Actions).
"""
import json, os, sys
from datetime import datetime, timezone
from openai import OpenAI

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
        "id": "form_watch",
        "name": "Micah Richards",
        "image": "richards.jpg",
        "role": "Sky Sports pundit, ex-Manchester City defender",
        "personality": (
            "Enthusiastic, warm, upbeat. Uses phrases like 'unbelievable', 'give him some credit', "
            "'look at that form!'. Laughs easily. Very energetic and positive, occasionally surprised."
        ),
        "topic": "current form — the hottest and coldest managers over the last 5 gameweeks",
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


def first_name(manager: str) -> str:
    return manager.split()[0]


def build_context(league: dict, cup: dict) -> tuple[str, int]:
    gw = league["metadata"]["current_gw"]
    gws_left = 38 - gw
    standings = league["standings"]
    form_map = {e["entry_id"]: e for e in league["form"]}

    lines = [
        f"FANTASY FOOTBALL LEAGUE DATA — After Gameweek {gw} ({gws_left} gameweeks remaining)",
        "",
        "LEAGUE RULES:",
        "  - Bottom 2 teams at the end of the season are relegated.",
        "  - Top 3 teams win prize payouts (1st, 2nd, 3rd place).",
        "  - There is a separate cup competition running alongside the league.",
        "",
        "CURRENT STANDINGS (rank | team | manager | total points | GW points | last-5-GW-avg):",
    ]
    for s in standings:
        fd = form_map.get(s["entry_id"], {})
        arrow = "↑" if s["rank"] < s["rank_last"] else ("↓" if s["rank"] > s["rank_last"] else "→")
        lines.append(
            f"  {s['rank']:2}. {s['name']} ({s['manager']}) — {s['total_points']} pts "
            f"{arrow}  | GW{gw}: {s['gw_points']} pts  | last-5 avg: {fd.get('form_avg', '?')}  "
            f"| last-5 scores: {fd.get('last5_scores', '?')}"
        )

    lines += ["", "BENCH POINTS WASTED THIS SEASON (points left on bench, never played):"]
    bench_sorted = sorted(league["bench_points"], key=lambda x: -x["total_bench_points"])
    for b in bench_sorted:
        lines.append(f"  {b['manager']}: {b['total_bench_points']} pts total, {b['avg_bench_per_gw']} avg/GW")

    lines += ["", "CAPTAIN HIT RATE (% of GWs where captain was top scorer in their XI):"]
    cap_sorted = sorted(league["captain_hit_rate"], key=lambda x: -x["hit_rate"])
    for c in cap_sorted:
        lines.append(f"  {c['manager']}: {c['hit_rate']}% ({c['captain_hits']}/{c['gws_played']} GWs)")

    final = cup["knockout"]["final"]
    team_a = final["team_a"]["name"]
    team_b = final["team_b"]["name"]
    # Map entry IDs to manager names
    mgr_map = {
        6366909: "Dominic Byrne",
        4789233: "Jason Knightly",
    }
    mgr_a = mgr_map.get(final["team_a"]["entry_id"], "")
    mgr_b = mgr_map.get(final["team_b"]["entry_id"], "")
    lines += [
        "",
        f"CUP FINAL: {team_a} ({mgr_a}) vs {team_b} ({mgr_b}) — GW{final['gw']} (status: {final.get('status', 'upcoming')})",
    ]
    if final.get("score_a") is not None:
        winner_name = (final.get("winner") or {}).get("name", "TBD")
        lines.append(f"  Result: {team_a} {final['score_a']} – {final['score_b']} {team_b}  |  Winner: {winner_name}")

    return "\n".join(lines), gw


def generate_article(client: OpenAI, pundit: dict, context: str, gw: int) -> dict:
    system_prompt = (
        f"You are {pundit['name']}, {pundit['role']}.\n"
        f"Personality: {pundit['personality']}\n\n"
        "Write exactly 2 short paragraphs of pundit commentary. Rules:\n"
        "- No headers, no bullet points, no emojis, no markdown.\n"
        "- Use first person. Reference managers by first name only.\n"
        "- Be opinionated, specific, and entertaining. Use real numbers from the data.\n"
        "- Each paragraph: 2–4 sentences. Total: 80–120 words.\n"
        "- Write entirely in your distinctive voice."
    )

    user_prompt = (
        f"Give your pundit verdict on {pundit['topic']} based on the data below. "
        f"Be direct and in character. Gameweek {gw}.\n\n{context}"
    )

    body_response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=220,
        temperature=0.85,
    )
    body = body_response.choices[0].message.content.strip()

    headline_response = client.chat.completions.create(
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
    headline = headline_response.choices[0].message.content.strip().strip('"').strip("'")

    return {
        "id": pundit["id"],
        "pundit_name": pundit["name"],
        "pundit_image": pundit["image"],
        "headline": headline,
        "body": body,
    }


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

    context, gw = build_context(league, cup)

    articles = []
    for pundit in PUNDITS:
        print(f"  Generating: {pundit['name']} ({pundit['id']})...")
        try:
            article = generate_article(client, pundit, context, gw)
            articles.append(article)
            print(f"    Headline: {article['headline']}")
        except Exception as e:
            print(f"  WARNING: Failed for {pundit['name']}: {e}", file=sys.stderr)

    news_data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "gw": gw,
        "articles": articles,
    }

    out_path = os.path.join(docs_dir, "news_data.json")
    with open(out_path, "w") as f:
        json.dump(news_data, f, indent=2)

    print(f"\nDone — news_data.json written ({len(articles)} articles, GW{gw})")


if __name__ == "__main__":
    main()
