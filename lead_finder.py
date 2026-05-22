import requests
from bs4 import BeautifulSoup
from google import genai
import json
import time
import os
import re
import sqlite3
from datetime import datetime, timezone

# ============================================================
# CONFIG
# ============================================================
GEMINI_KEY = os.environ["GEMINI_API_KEY"]

TAGS = [
    "horror", "atmospheric", "dark-fantasy", "souls-like",
    "rpg", "action-rpg", "gacha", "cinematic",
    "sci-fi", "ambient", "fantasy", "narrative"
]

PAGES_PER_TAG = 1  # ~30 games per page

# Add itch.io display names of creators you never want to see again
BLOCKED_AUTHORS = [
    # "somedevname",
    # "thatannoyingdev",
]


# ============================================================
# DATABASE
# ============================================================
def init_db():
    conn = sqlite3.connect("leads.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_found TEXT,
            title TEXT,
            author TEXT,
            author_profile_url TEXT,
            url TEXT UNIQUE,
            source TEXT,
            description TEXT,
            explicitly_hiring INTEGER,
            hiring_quote TEXT,
            needs_sound_design INTEGER,
            needs_music INTEGER,
            confidence TEXT,
            pitch_sfx TEXT,
            pitch_music TEXT,
            genre_match TEXT,
            priority INTEGER,
            reason TEXT,
            contact TEXT
        )
    """)
    conn.commit()
    return conn


def already_seen(conn, url):
    c = conn.cursor()
    c.execute("SELECT 1 FROM leads WHERE url = ?", (url,))
    return c.fetchone() is not None


def save_lead(conn, lead, date_str):
    try:
        conn.execute("""
            INSERT INTO leads 
            (date_found, title, author, author_profile_url, url, source, description,
             explicitly_hiring, hiring_quote,
             needs_sound_design, needs_music, confidence,
             pitch_sfx, pitch_music, genre_match, priority, reason, contact)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            date_str,
            lead.get("title", ""),
            lead.get("author", ""),
            lead.get("author_profile_url", ""),
            lead.get("url", ""),
            lead.get("source", ""),
            lead.get("description", ""),
            1 if lead.get("explicitly_hiring") else 0,
            lead.get("hiring_quote", ""),
            1 if lead.get("needs_sound_design") else 0,
            1 if lead.get("needs_music") else 0,
            lead.get("confidence", ""),
            lead.get("pitch_sfx", ""),
            lead.get("pitch_music", ""),
            lead.get("genre_match", ""),
            lead.get("priority", 0),
            lead.get("reason", ""),
            lead.get("contact", "")
        ))
        conn.commit()
    except sqlite3.IntegrityError:
        pass


# ============================================================
# SCRAPER
# ============================================================
def scrape_itch_tag(tag, pages=1):
    leads = []
    for page in range(1, pages + 1):
        url = f"https://itch.io/games/newest/tag-{tag}?page={page}"
        try:
            resp = requests.get(url, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
            for cell in soup.select(".game_cell"):
                title_el = cell.select_one(".title")
                link_el = cell.select_one("a.title")
                desc_el = cell.select_one(".game_text")
                author_el = cell.select_one(".game_author a")

                author_name = author_el.text.strip() if author_el else ""
                author_href = author_el.get("href", "") if author_el else ""
                # Build full profile URL
                if author_href and author_href.startswith("/"):
                    author_profile = f"https://itch.io{author_href}"
                elif author_href:
                    author_profile = author_href
                else:
                    author_profile = ""

                leads.append({
                    "title": title_el.text.strip() if title_el else "",
                    "url": link_el["href"] if link_el else "",
                    "description": desc_el.text.strip() if desc_el else "",
                    "author": author_name,
                    "author_profile_url": author_profile,
                    "source": f"itch.io/tag-{tag}"
                })
        except Exception as e:
            print(f"Error scraping tag '{tag}' page {page}: {e}")
    return leads


def collect_all_leads(conn):
    all_leads = []
    for tag in TAGS:
        print(f"Scraping: {tag}")
        all_leads.extend(scrape_itch_tag(tag, PAGES_PER_TAG))
        time.sleep(2)

    blocked_lower = [b.lower() for b in BLOCKED_AUTHORS]

    seen = set()
    unique = []
    skipped_seen = 0
    skipped_blocked = 0

    for lead in all_leads:
        if not lead["url"] or lead["url"] in seen:
            continue
        if lead["author"].lower() in blocked_lower:
            skipped_blocked += 1
            continue
        if already_seen(conn, lead["url"]):
            skipped_seen += 1
            continue
        seen.add(lead["url"])
        unique.append(lead)

    print(f"Found {len(unique)} new unique leads.")
    print(f"Skipped {skipped_seen} already-seen games.")
    print(f"Skipped {skipped_blocked} blocked authors.")
    return unique


# ============================================================
# CONTACT FINDER
# Checks THREE places in order:
#   1. The game page itself
#   2. The developer's itch.io profile page
#   3. Any linked external site on the profile (portfolio, linktree, etc.)
# ============================================================
def scrape_contact_from_html(html_text, soup):
    """Extract emails and social links from a parsed page."""
    emails = re.findall(
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        html_text
    )
    # Filter out common false positives
    emails = [
        e for e in emails
        if not any(skip in e.lower() for skip in [
            "sentry", "example", "noreply", "no-reply",
            "itch.io", "support@", "help@", "abuse@"
        ])
    ]

    socials = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if any(s in href for s in [
            "twitter.com", "x.com", "discord.gg", "discord.com",
            "mastodon", "bsky.app", "linkedin.com",
            "instagram.com", "youtube.com", "linktr.ee", "carrd.co"
        ]):
            socials.append(href)

    return list(set(emails)), list(set(socials))


def find_contact_info(game_url, author_profile_url):
    """
    Aggressively hunt for contact info across the game page,
    the author's profile, and any linked external sites.
    """
    all_emails = []
    all_socials = []

    # --- 1. Game page ---
    try:
        resp = requests.get(game_url, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        emails, socials = scrape_contact_from_html(resp.text, soup)
        all_emails.extend(emails)
        all_socials.extend(socials)
        time.sleep(1)
    except Exception:
        pass

    # --- 2. Author profile page ---
    external_links = []
    if author_profile_url:
        try:
            resp = requests.get(author_profile_url, timeout=10)
            soup = BeautifulSoup(resp.text, "html.parser")
            emails, socials = scrape_contact_from_html(resp.text, soup)
            all_emails.extend(emails)
            all_socials.extend(socials)

            # Collect external links from the profile to check next
            for a in soup.select("a[href]"):
                href = a.get("href", "")
                if (
                    href.startswith("http")
                    and "itch.io" not in href
                    and href not in all_socials
                ):
                    external_links.append(href)

            time.sleep(1)
        except Exception:
            pass

    # --- 3. First external link from profile (portfolio, linktree, etc.) ---
    if not all_emails and external_links:
        try:
            resp = requests.get(external_links[0], timeout=10)
            soup = BeautifulSoup(resp.text, "html.parser")
            emails, socials = scrape_contact_from_html(resp.text, soup)
            all_emails.extend(emails)
            all_socials.extend(socials)
            time.sleep(1)
        except Exception:
            pass

    # --- Build result string ---
    parts = []
    if all_emails:
        unique_emails = list(dict.fromkeys(all_emails))[:2]  # Preserve order, dedupe
        parts.append("Email: " + ", ".join(unique_emails))
    if all_socials:
        unique_socials = list(dict.fromkeys(all_socials))[:4]
        parts.append("Links: " + ", ".join(unique_socials))

    return " | ".join(parts) if parts else "Check page"


# ============================================================
# HIRING CHECK
# Looks at the game page description and devlog for explicit
# "looking for sound designer / composer / musician" language
# ============================================================
HIRING_KEYWORDS = [
    "looking for", "seeking", "need a", "need an", "hiring",
    "open to", "want a", "want to work with",
    "sound designer", "composer", "musician", "audio designer",
    "sfx artist", "audio artist", "music composer",
    "collaborat"  # covers collaborate/collaboration/collaborating
]

def check_if_hiring(game_url):
    """
    Returns (is_hiring: bool, quote: str)
    Scans the game page for any explicit audio hiring language.
    """
    try:
        resp = requests.get(game_url, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Get all readable text from the page
        text_blocks = []
        for el in soup.select(".game_description, .devlog_post, p, li, .body"):
            text_blocks.append(el.get_text(separator=" "))

        full_text = " ".join(text_blocks).lower()

        # Sliding window: look for hiring keywords near audio keywords
        audio_keywords = [
            "sound", "music", "audio", "sfx", "composer",
            "soundtrack", "musician"
        ]
        hiring_triggers = [
            "looking for", "seeking", "need a", "need an",
            "hiring", "open to", "want a", "collaborat"
        ]

        # Check if any hiring trigger appears within 80 characters of an audio keyword
        for trigger in hiring_triggers:
            idx = full_text.find(trigger)
            while idx != -1:
                window = full_text[max(0, idx - 80): idx + 80]
                if any(ak in window for ak in audio_keywords):
                    # Found a match — grab the original case snippet for the report
                    original_text = " ".join(text_blocks)
                    start = max(0, original_text.lower().find(trigger) - 40)
                    snippet = original_text[start: start + 160].strip()
                    snippet = " ".join(snippet.split())  # Collapse whitespace
                    return True, f'"{snippet}..."'
                idx = full_text.find(trigger, idx + 1)

        return False, ""

    except Exception:
        return False, ""


# ============================================================
# GEMINI ANALYSIS
# ============================================================
def analyze_lead(client, lead):
    """Ask Gemini to evaluate a lead. Retries on 503 and 429."""
    prompt = f"""You are helping a freelance sound designer and composer find work.

His specialties:
- ATMOSPHERIC SOUND DESIGN: Deep, immersive, environmental audio. FromSoftware-style 
  (Elden Ring, Bloodborne, Dark Souls) — brooding textures, haunting ambience, 
  tension-building soundscapes. Games that live or die by their atmosphere are 
  a perfect fit.
- PUNCHY SOUND DESIGN: High-impact, snappy, satisfying SFX. Hits that feel 
  physical. UI clicks that feel premium. Weapons and abilities that feel heavy 
  and deliberate.
- HEAVY MUSIC: Metalcore, doom metal, argent metal (think DOOM 2016/Eternal 
  style), and Thall (Meshuggah-influenced djent). Dense, rhythmically complex, 
  aggressive scoring.
- MIXING & MASTERING: Heavy and snappy. Punchy low-end, tight transients, 
  professional loudness. He can mix/master game OSTs, trailers, and cutscene music.
- GACHA GAME AUDIO: Polished UI SFX, summoning sequences, character reveal stings, 
  menu ambience, and the kind of cinematic character themes gacha players obsess over.

Analyze this game project. Be honest — if it's a bad fit, say so with low priority.
A horror game, a souls-like, a dark RPG, a gacha with cinematic flair, or any 
game that wants to feel heavy, oppressive, or epic is a strong match.
A casual puzzle game or cozy farming sim is not.

Game Title: {lead['title']}
Description: {lead['description']}
Developer: {lead['author']}
Source: {lead['source']}
URL: {lead['url']}
Explicitly Hiring for Audio: {lead.get('explicitly_hiring', False)}
Hiring Context: {lead.get('hiring_quote', 'N/A')}

Return ONLY valid JSON, no markdown, no code fences:
{{
    "needs_sound_design": true or false,
    "needs_music": true or false,
    "confidence": "high" or "medium" or "low",
    "pitch_sfx": "One sentence: specifically why HIS punchy/atmospheric style fits their SFX needs. Write N/A if false.",
    "pitch_music": "One sentence: specifically why HIS heavy/metalcore/doom style fits their music needs. Write N/A if false.",
    "genre_match": "high" or "medium" or "low",
    "priority": 1 to 10,
    "reason": "One sentence: why this is or isn't a good lead. If they are explicitly hiring, bump priority up."
}}"""

    max_retries = 4
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt
            )
            raw = response.text.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            return json.loads(raw)

        except Exception as e:
            error_str = str(e)

            if "503" in error_str:
                wait = 15 * (attempt + 1)
                print(f"  503 overloaded. Waiting {wait}s (retry {attempt+1}/{max_retries})...")
                time.sleep(wait)
                continue

            elif "429" in error_str:
                match = re.search(r'retry in (\d+\.?\d*)s', error_str)
                wait = float(match.group(1)) + 5 if match else 65
                print(f"  429 rate limit. Waiting {wait:.0f}s...")
                time.sleep(wait)
                continue

            else:
                print(f"  Gemini error on '{lead['title']}': {e}")
                return None

    print(f"  Gave up on '{lead['title']}' after {max_retries} retries.")
    return None


# ============================================================
# MARKDOWN REPORT
# ============================================================
def generate_report(leads, date_str):
    # Separate explicitly hiring leads into their own section
    hiring = [l for l in leads if l.get("explicitly_hiring") and l.get("priority", 0) >= 4]
    hot = [l for l in leads if not l.get("explicitly_hiring") and l.get("priority", 0) >= 7]
    warm = [l for l in leads if not l.get("explicitly_hiring") and 4 <= l.get("priority", 0) < 7]
    cold = [l for l in leads if l.get("priority", 0) < 4]

    total = len(leads)
    lines = [
        f"# Lead Report — {date_str}",
        f"",
        f"**{total} leads analyzed this run.**",
        f"",
    ]

    if hiring:
        lines.append(f"## 🚨 Explicitly Hiring for Audio ({len(hiring)})")
        lines.append(f"> These devs have directly stated they need a sound designer or musician.")
        lines.append(f"")
        for lead in hiring:
            lines.extend(format_lead_block(lead))

    for tier_name, tier_leads in [
        ("🔥 Hot Leads", hot),
        ("🟡 Warm Leads", warm),
        ("🔵 Cold Leads", cold)
    ]:
        if not tier_leads:
            continue
        lines.append(f"## {tier_name} ({len(tier_leads)})")
        lines.append("")
        for lead in tier_leads:
            lines.extend(format_lead_block(lead))

    report_path = f"reports/{date_str}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"Report written: {report_path}")
    return report_path


def format_lead_block(lead):
    """Format a single lead as markdown lines."""
    sfx = "✅" if lead.get("needs_sound_design") else "❌"
    mus = "✅" if lead.get("needs_music") else "❌"
    hiring_badge = " 🚨 **HIRING**" if lead.get("explicitly_hiring") else ""

    lines = [
        f"### {lead.get('title', 'Untitled')} — by {lead.get('author', '?')}{hiring_badge}",
        f"",
        f"- **Priority:** {lead.get('priority', '?')}/10  |  **Genre Match:** {lead.get('genre_match', '?')}  |  **Confidence:** {lead.get('confidence', '?')}",
        f"- **Why:** {lead.get('reason', '')}",
        f"- **Needs SFX:** {sfx} {lead.get('pitch_sfx', 'N/A')}",
        f"- **Needs Music:** {mus} {lead.get('pitch_music', 'N/A')}",
    ]

    if lead.get("explicitly_hiring") and lead.get("hiring_quote"):
        lines.append(f"- **They Said:** {lead.get('hiring_quote', '')}")

    lines += [
        f"- **Contact:** {lead.get('contact', 'Check page')}",
        f"- **Game:** [{lead.get('url', '')}]({lead.get('url', '')})",
    ]

    if lead.get("author_profile_url"):
        lines.append(f"- **Dev Profile:** [{lead.get('author_profile_url', '')}]({lead.get('author_profile_url', '')})")

    lines += [
        f"- **Source:** {lead.get('source', '')}",
        f"",
        f"---",
        f""
    ]
    return lines


# ============================================================
# MAIN
# ============================================================
def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print("=" * 50)
    print(f"LEAD FINDER — {today}")
    print("=" * 50)

    conn = init_db()
    leads = collect_all_leads(conn)
    leads = leads[:80]

    if not leads:
        print("No new leads to analyze this run.")
        os.makedirs("reports", exist_ok=True)
        with open(f"reports/{today}.md", "w") as f:
            f.write(f"# Lead Report — {today}\n\nNo new leads found this run.\n")
        conn.close()
        return

    client = genai.Client(api_key=GEMINI_KEY)
    analyzed = []

    for i, lead in enumerate(leads):
        print(f"[{i+1}/{len(leads)}] {lead['title']}")

        # Check if the dev explicitly said they need audio help
        print(f"  Checking for hiring signals...")
        is_hiring, hiring_quote = check_if_hiring(lead["url"])
        lead["explicitly_hiring"] = is_hiring
        lead["hiring_quote"] = hiring_quote
        if is_hiring:
            print(f"  ⚡ HIRING SIGNAL FOUND: {hiring_quote[:80]}...")
        time.sleep(1)

        # Gemini analysis (aware of hiring status)
        result = analyze_lead(client, lead)

        if result:
            # Grab contact info for anything worth pursuing
            if result.get("priority", 0) >= 4 or is_hiring:
                print(f"  -> Priority {result['priority']}. Hunting for contact info...")
                lead["contact"] = find_contact_info(
                    lead["url"],
                    lead.get("author_profile_url", "")
                )
            else:
                lead["contact"] = "Low priority — skipped"

            lead.update(result)
            analyzed.append(lead)
            save_lead(conn, lead, today)

        time.sleep(7)

    analyzed.sort(key=lambda x: (
        -(1 if x.get("explicitly_hiring") else 0),  # Hiring leads float to top
        -x.get("priority", 0)
    ))

    os.makedirs("reports", exist_ok=True)
    generate_report(analyzed, today)

    conn.close()
    print(f"Done. {len(analyzed)} leads analyzed and saved.")


if __name__ == "__main__":
    main()
