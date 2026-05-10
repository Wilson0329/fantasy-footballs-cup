#!/usr/bin/env python3
"""
build_news_data.py
Generates Sky Sports pundit commentary for news_data.json using GitHub Models (gpt-4o-mini).
Optionally generates audio clips via ElevenLabs TTS (requires ELEVENLABS_API_KEY).

Requires:
  - GITHUB_TOKEN env var (automatically present in GitHub Actions)
  - ELEVENLABS_API_KEY env var (optional — skips audio if absent)

Data sources:
  - docs/league_data.json + docs/cup_data.json  (pre-built)
  - FPL bootstrap-static API  (player injury/availability flags, GW status)
  - FPL entry picks API       (which managers own which flagged players)
  - Fantasy Football Scout RSS  (latest FPL news, injuries & tips)
"""
import json, os, sys, time, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from openai import OpenAI

try:
    from elevenlabs.client import ElevenLabs as ElevenLabsClient
    _ELEVENLABS_SDK = True
except ImportError:
    _ELEVENLABS_SDK = False

FPL_BASE = "https://fantasy.premierleague.com/api"
FFS_RSS   = "https://www.fantasyfootballscout.co.uk/feed/"
HEADERS   = {"User-Agent": "fantasy-football-cup/1.0 (personal league tracker)"}

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
        "voice_id": "pNInz6obpgDQGcFmaJgB",   # Adam — authoritative, clear
        "role": "Sky Sports analyst, ex-Manchester United captain",
        "personality": (
            "Analytical but prone to dramatic overstatement. Uses phrases like 'I genuinely believe', "
            "'nailed on', 'absolutely brilliant'. Occasionally makes bold predictions he'll immediately "
            "walk back. Takes himself slightly too seriously. Loves a tactical explanation even when "
            "none is needed."
        ),
        "topic": "the title race and the fight for the top 3 payout positions",
    },
    {
        "id": "relegation",
        "name": "Roy Keane",
        "image": "keane.jpg",
        "voice_id": "VR6AewLTigWG4xSOukaG",   # Arnold — deep, stern
        "role": "Sky Sports pundit, ex-Manchester United and Ireland captain",
        "personality": (
            "Brutally contemptuous of weakness. Short, withering sentences. Uses phrases like "
            "'not good enough', 'I'm not surprised', 'no excuses', 'embarrassing'. "
            "Genuinely baffled that anyone could perform this badly. Has zero sympathy. "
            "Occasionally makes it personal in a way that feels slightly over the top."
        ),
        "topic": "the relegation battle — the bottom two teams fighting to survive",
    },
    {
        "id": "cup_final",
        "name": "Jamie Carragher",
        "image": "carragher.jpg",
        "voice_id": "TxGEqnHWrfWFTfGW9XjX",   # Josh — warm, expressive
        "role": "Sky Sports pundit, ex-Liverpool defender",
        "personality": (
            "Passionate and excitable, with a habit of contradicting himself mid-sentence. "
            "Uses phrases like 'I'll be honest with ya', 'massive occasion', 'what a tie', "
            "'but listen'. Gets carried away with big moments. Occasionally compares things "
            "to Champions League finals even when completely unnecessary."
        ),
        "topic": "the cup final — who is in it, how they got there, and what to expect",
    },
    {
        "id": "form_injuries",
        "name": "Micah Richards",
        "image": "richards.jpg",
        "voice_id": "yoZ06aMxZJJ28mfd3POQ",   # Sam — energetic, upbeat
        "role": "Sky Sports pundit, ex-Manchester City defender",
        "personality": (
            "Irrepressibly enthusiastic to the point of being slightly exhausting. Uses phrases like "
            "'UNBELIEVABLE', 'give him some credit!', 'look at that!'. Laughs at his own observations. "
            "Finds everything amazing. Occasionally expresses genuine surprise that someone is doing "
            "badly, as if expecting the best from everyone at all times."
        ),
        "topic": (
            "current form — the hottest and coldest managers over the last 5 gameweeks — "
            "and how injuries to key players could affect the run-in"
        ),
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


# ─── Fantasy Football Scout RSS ───────────────────────────────────────────────

def fetch_ffs_news(max_items: int = 12) -> list[str]:
    """Fetch FPL headlines from Fantasy Football Scout RSS."""
    try:
        raw = fetch_url(FFS_RSS, timeout=12)
        root = ET.fromstring(raw)
        headlines = []
        for item in root.findall(".//item")[:max_items]:
            title = (item.findtext("title") or "").strip()
            desc  = (item.findtext("description") or "").strip()
            if not title:
                continue
            entry = title
            if desc and len(desc) > 15:
                short_desc = desc[:150].rsplit(" ", 1)[0]
                entry = f"{title} — {short_desc}"
            headlines.append(entry)
        return headlines
    except Exception as e:
        print(f"  WARNING: Could not fetch FFS RSS: {e}", file=sys.stderr)
        return []


# ─── GW status detection ─────────────────────────────────────────────────────

def get_gw_status(bootstrap: dict, current_gw: int) -> dict:
    """Returns dict with is_live and is_finished for the current GW."""
    for event in bootstrap["events"]:
        if event["id"] == current_gw:
            is_finished = event.get("finished", False)
            is_current  = event.get("is_current", False)
            is_live     = is_current and not is_finished
            return {"is_live": is_live, "is_finished": is_finished, "is_current": is_current}
    return {"is_live": False, "is_finished": True, "is_current": False}


# ─── Change detection ─────────────────────────────────────────────────────────

def needs_regeneration(current_gw: int, squad_alerts: list, out_path: str) -> bool:
    """
    Returns True if commentary should be regenerated.
    - Always regenerate if GW has changed.
    - Within the same GW (non-live): only regenerate if new injuries/flags appeared.
    """
    if not os.path.exists(out_path):
        return True
    try:
        with open(out_path) as f:
            existing = json.load(f)
    except Exception:
        return True

    last_gw = existing.get("gw")
    if last_gw != current_gw:
        print(f"  GW changed ({last_gw} → {current_gw}) — regenerating commentary.")
        return True

    # Same GW — check if injury situation has meaningfully changed
    prev_alerts_set = set(existing.get("squad_alert_fingerprint", []))
    curr_alerts_set = set(
        f"{manager}:{alert}"
        for manager, team_name, alerts in squad_alerts
        for alert in alerts
    )
    new_alerts = curr_alerts_set - prev_alerts_set
    if new_alerts:
        print(f"  {len(new_alerts)} new squad alert(s) — regenerating commentary.")
        return True

    print(f"  No significant changes since last generation (GW{current_gw}) — skipping.")
    return False


# ─── Context assembly ─────────────────────────────────────────────────────────

def build_context(league: dict, cup: dict, bootstrap: dict) -> tuple[str, int, list, dict]:
    """Returns (context_string, current_gw, squad_alerts, gw_status)."""
    gw        = league["metadata"]["current_gw"]
    gws_left  = 38 - gw
    standings = league["standings"]
    form_map  = {e["entry_id"]: e for e in league["form"]}

    # GW status
    gw_status = get_gw_status(bootstrap, gw)

    teams_map   = {t["id"]: t["short_name"] for t in bootstrap["teams"]}

    # Players with any flag
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
        time.sleep(0.25)
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

    # FFS news
    print("  Fetching Fantasy Football Scout news...")
    news_headlines = fetch_ffs_news()
    print(f"    {len(news_headlines)} headlines fetched")

    # ── Assemble context ──
    gw_label = "LIVE" if gw_status["is_live"] else ("complete" if gw_status["is_finished"] else "upcoming")
    lines = [
        f"FANTASY FOOTBALL LEAGUE — Gameweek {gw} [{gw_label}] ({gws_left} gameweeks remaining)",
        "",
        "LEAGUE RULES:",
        "  - Bottom 2 teams are RELEGATED at end of season.",
        "  - Top 3 teams win prize payouts (1st, 2nd, 3rd place).",
        "  - There is a separate cup competition running alongside the league.",
        "",
        "CURRENT STANDINGS:",
    ]

    for s in standings:
        fd    = form_map.get(s["entry_id"], {})
        arrow = "↑" if s["rank"] < s["rank_last"] else ("↓" if s["rank"] > s["rank_last"] else "→")
        last5 = fd.get('last5_scores', [])
        last5_str = ', '.join(str(x) for x in last5) if last5 else '?'
        lines.append(
            f"  {s['rank']:2}. {s['name']} ({s['manager'].split()[0]}) — "
            f"season: {s['total_points']} pts {arrow}"
            f" | GW{gw}: {s['gw_points']} pts"
            f" | 5-GW avg: {fd.get('form_avg','?')}"
            f" | last 5 (old→new): {last5_str}"
        )

    final   = cup["knockout"]["final"]
    mgr_cup = {6366909: "Dominic", 4789233: "Jason"}
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
        lines += ["", "INJURY & AVAILABILITY ALERTS (in managers' squads this GW):"]
        for manager, team_name, alerts in squad_alerts:
            first = manager.split()[0]
            lines.append(f"  {first} ({team_name}):")
            for a in alerts:
                lines.append(f"    - {a}")

    if news_headlines:
        lines += ["", "LATEST FPL NEWS & INJURIES (Fantasy Football Scout):"]
        for h in news_headlines:
            lines.append(f"  • {h}")

    return "\n".join(lines), gw, squad_alerts, gw_status


# ─── LLM generation ──────────────────────────────────────────────────────────

def generate_article(client: OpenAI, pundit: dict, context: str, gw: int, is_live: bool) -> dict:
    if is_live:
        live_instruction = (
            "\n\nIMPORTANT: This is a LIVE gameweek currently in progress. "
            "Your commentary should focus on what's happening RIGHT NOW this gameweek — "
            "who is flying, who is flopping, surprise scores, nightmare captains. "
            "Make it feel like a live studio reaction, not a season review."
        )
    else:
        live_instruction = ""

    system_prompt = (
        f"You are {pundit['name']}, {pundit['role']}.\n"
        f"Personality: {pundit['personality']}\n\n"
        "Write exactly 2 short paragraphs of pundit commentary. Rules:\n"
        "- No headers, no bullet points, no emojis, no markdown.\n"
        "- Use first person. Reference all managers by FIRST NAME ONLY.\n"
        "- Be funny, opinionated and in character. Personality over statistics.\n"
        "- You may mention 1–2 specific numbers if they make a point land harder, but don't list stats.\n"
        "- Weave in injury news or FPL headlines naturally if relevant — don't just list them.\n"
        "- Each paragraph: 2–4 sentences. Total: 80–130 words.\n"
        "- Write entirely in your distinctive voice."
        + live_instruction
    )

    user_prompt = (
        f"Give your pundit verdict on {pundit['topic']} based on the league data and "
        f"FPL news below. Be direct, funny and in character. Gameweek {gw}.\n\n{context}"
    )

    body_resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        max_tokens=240,
        temperature=0.9,
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


# ─── Podcast script generation ───────────────────────────────────────────────

# Map pundit name (uppercase) → pundit dict for script parsing
PUNDIT_BY_NAME = {p["name"].upper(): p for p in [
    {"name": "NEVILLE",   "voice_id": "onwK4e9ZLuTAKqWW03F9"},  # Daniel – British male, steady broadcaster
    {"name": "KEANE",     "voice_id": "JBFqnCBsd6RMkjVDRZzb"},  # George – British male, warm/captivating
    {"name": "CARRAGHER", "voice_id": "IKne3meq5aSn9XLyUdCD"},  # Charlie – Australian male, closest to British available
    {"name": "RICHARDS",  "voice_id": "SOYHLrjzK2X1ezoPC6cr"},  # Harry – energetic, suits Richards' personality
]}


def generate_podcast_script(client: OpenAI, context: str, gw: int, is_live: bool) -> str:
    """
    Ask the LLM to write a multi-speaker podcast discussion.
    Returns a raw script string with [SPEAKER]: lines.
    """
    live_note = (
        " This is a LIVE gameweek — focus on what's happening RIGHT NOW: "
        "surprise scores, nightmare captains, who's flying and who's flopping."
        if is_live else ""
    )

    system_prompt = (
        "You are writing a script for a Fantasy Football podcast hosted by four Sky Sports pundits: "
        "Gary Neville (analytical, dramatic, takes himself too seriously), "
        "Roy Keane (brutally contemptuous, short withering sentences, no sympathy), "
        "Jamie Carragher (passionate, excitable, contradicts himself, 'I'll be honest with ya'), "
        "and Micah Richards (irrepressibly enthusiastic, finds everything amazing, 'UNBELIEVABLE').\n\n"
        "Rules:\n"
        "- Format every line as [NEVILLE]: text, [KEANE]: text, [CARRAGHER]: text, or [RICHARDS]: text.\n"
        "- The pundits talk TO each other — they react, agree sarcastically, cut each other off.\n"
        "- Cover: title race / top 3 payout fight, relegation danger, cup final, and form/injuries.\n"
        "- Use first names only for managers. Be funny, opinionated, in character.\n"
        "- Keep it punchy: 280–320 words total (fits within ElevenLabs free tier).\n"
        "- No stage directions, no asterisks, no markdown. Just speaker lines.\n"
        "- Start with Neville opening, end with Richards on an enthusiastic note."
    )

    user_prompt = (
        f"Write the GW{gw} Fantasy Footballs podcast discussion based on this league data.{live_note}\n\n"
        f"{context}"
    )

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        max_tokens=520,
        temperature=0.92,
    )
    return resp.choices[0].message.content.strip()


def parse_podcast_script(script: str) -> list[tuple[str, str]]:
    """
    Parse a script like '[NEVILLE]: text...' into [(speaker, text), ...].
    Returns list of (uppercase_speaker_key, spoken_text) tuples.
    """
    import re
    segments = []
    for line in script.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r'\[([A-Z]+)\]:\s*(.+)', line)
        if m:
            segments.append((m.group(1), m.group(2).strip()))
    return segments


# ─── ElevenLabs TTS ──────────────────────────────────────────────────────────

def tts_segment(el_client, voice_id: str, text: str) -> bytes | None:
    """Call ElevenLabs TTS via SDK and return raw MP3 bytes, or None on failure."""
    try:
        audio = el_client.text_to_speech.convert(
            voice_id=voice_id,
            text=text,
            model_id="eleven_turbo_v2_5",
            voice_settings={"stability": 0.45, "similarity_boost": 0.75},
        )
        # SDK returns a generator of bytes chunks
        return b"".join(audio)
    except Exception as e:
        print(f"    WARNING: TTS failed ({voice_id}): {e}", file=sys.stderr)
        return None


def generate_podcast_audio(el_client, script: str, out_path: str) -> bool:
    """
    Parse the podcast script, TTS each speaker turn, concatenate MP3 bytes,
    write to out_path. Returns True on success.
    """
    segments = parse_podcast_script(script)
    if not segments:
        print("  WARNING: No segments parsed from script.", file=sys.stderr)
        return False

    print(f"  {len(segments)} speaker turns to synthesise...")
    mp3_chunks = []
    for i, (speaker, text) in enumerate(segments):
        pundit = PUNDIT_BY_NAME.get(speaker)
        if not pundit:
            print(f"    Skipping unknown speaker: {speaker}")
            continue
        print(f"    [{i+1}/{len(segments)}] {speaker}: {text[:60]}...")
        chunk = tts_segment(el_client, pundit["voice_id"], text)
        if chunk:
            mp3_chunks.append(chunk)
        time.sleep(0.4)   # stay within rate limits

    if not mp3_chunks:
        return False

    combined = b"".join(mp3_chunks)
    with open(out_path, "wb") as f:
        f.write(combined)
    print(f"  Podcast saved: {len(combined)//1024} KB → {os.path.basename(out_path)}")
    return True


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: GITHUB_TOKEN environment variable not set", file=sys.stderr)
        sys.exit(1)

    elevenlabs_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    el_client = None
    if elevenlabs_key:
        if not _ELEVENLABS_SDK:
            print("WARNING: elevenlabs SDK not installed — audio skipped.", file=sys.stderr)
        else:
            print("ElevenLabs API key found — validating...")
            try:
                el_client = ElevenLabsClient(api_key=elevenlabs_key)
                voices = el_client.voices.get_all()
                available = {v.voice_id: v.name for v in voices.voices}
                print(f"  Key valid. {len(available)} voices available.")
                # Verify/remap hardcoded voice IDs
                for p in PUNDIT_BY_NAME.values():
                    vid = p["voice_id"]
                    if vid in available:
                        print(f"  ✓ {p['name']}: {available[vid]}")
                    else:
                        fallback_id = next(iter(available), None)
                        if fallback_id:
                            print(f"  ! {p['name']}: {vid} not found → using '{available[fallback_id]}'")
                            p["voice_id"] = fallback_id
            except Exception as e:
                print(f"ElevenLabs validation failed: {e}", file=sys.stderr)
                el_client = None
    else:
        print("No ELEVENLABS_API_KEY — audio generation skipped.")

    client = OpenAI(
        base_url="https://models.inference.ai.azure.com",
        api_key=token,
    )

    base_dir = os.path.dirname(os.path.abspath(__file__))
    docs_dir = os.path.join(base_dir, "docs")
    out_path = os.path.join(docs_dir, "news_data.json")

    with open(os.path.join(docs_dir, "league_data.json")) as f:
        league = json.load(f)
    with open(os.path.join(docs_dir, "cup_data.json")) as f:
        cup = json.load(f)

    print("Fetching FPL bootstrap for injury data and GW status...")
    bootstrap = fetch_bootstrap()

    print("Building context...")
    context, gw, squad_alerts, gw_status = build_context(league, cup, bootstrap)
    print(f"Context ready ({len(context)} chars) | Live GW: {gw_status['is_live']}\n")

    # Skip regeneration if nothing meaningful has changed (non-live GW only)
    if not gw_status["is_live"] and not needs_regeneration(gw, squad_alerts, out_path):
        print("Commentary is up to date — no regeneration needed.")
        return

    articles = []
    for pundit in PUNDITS:
        print(f"Generating: {pundit['name']} ({pundit['id']})...")
        try:
            article = generate_article(client, pundit, context, gw, gw_status["is_live"])
            articles.append(article)
            print(f"  Headline: {article['headline']}")
        except Exception as e:
            print(f"  WARNING: Failed for {pundit['name']}: {e}", file=sys.stderr)

    # Generate podcast as a multi-speaker discussion
    podcast_file = None
    if el_client:
        print("\nGenerating podcast script...")
        try:
            script = generate_podcast_script(client, context, gw, gw_status["is_live"])
            char_count = len(script)
            print(f"  Script ready ({char_count} chars, ~{char_count} ElevenLabs chars)")
            print("\nSynthesising podcast audio...")
            podcast_path = os.path.join(docs_dir, "podcast.mp3")
            ok = generate_podcast_audio(el_client, script, podcast_path)
            if ok:
                podcast_file = "podcast.mp3"
        except Exception as e:
            print(f"  WARNING: Podcast generation failed: {e}", file=sys.stderr)

    # Build squad alert fingerprint for change detection on next run
    alert_fingerprint = [
        f"{manager}:{alert}"
        for manager, team_name, alerts in squad_alerts
        for alert in alerts
    ]

    news_data = {
        "generated_at":            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "gw":                      gw,
        "is_live":                 gw_status["is_live"],
        "squad_alert_fingerprint": alert_fingerprint,
        "podcast_file":            podcast_file,
        "articles":                articles,
    }

    with open(out_path, "w") as f:
        json.dump(news_data, f, indent=2)

    print(f"\nDone — news_data.json written ({len(articles)} articles, podcast={'yes' if podcast_file else 'no'}, GW{gw}, live={gw_status['is_live']})")


if __name__ == "__main__":
    main()
