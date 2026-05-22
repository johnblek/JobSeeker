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

# Itch.io tags to scrape — add/remove whatever fits your vibe
TAGS = [
    "horror", "atmospheric", "dark-fantasy", "souls-like",
    "rpg", "action-rpg", "gacha", "cinematic",
    "sci-fi", "ambient", "fantasy", "narrative"
]

PAGES_PER_TAG = 1  # ~30 games per page


# ============================================================
# DATABASE SETUP
# ============================================================
def init_db():
    """Create the SQLite database and table if they don't exist."""
    conn = sqlite3.connect("leads.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_found TEXT,
            title TEXT,
            author TEXT,
            url TEXT UNIQUE,
            source TEXT,
            description TEXT,
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
    """Check if we've already logged this game before."""
    c = conn.cursor()
    c.execute("SELECT 1 FROM leads WHERE url = ?", (url,))
    return c.fetchone() is not None


def save_lead(conn, lead, date_str):
    """Insert a lead into the database."""
    try:
        conn.execute("""
            INSERT INTO leads 
            (date_found, title, author, url, source, description,
             needs_sound_design, needs_music, confidence,
             pitch_sfx, pitch_music, genre_match, priority, reason, contact)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            date_str,
            lead.get("title", ""),
            lead.get("author", ""),
            lead.get("url", ""),
            lead.get("source", ""),
            lead.get("description", ""),
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
        pass  # Duplicate URL, skip


# ============================================================
# SCRAPER
# ============================================================
def scrape_itch_tag(tag, pages=1):
    """Grab games from an itch.io tag page."""
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
                leads.append({
                    "title": title_el.text.strip() if title_el else "",
                    "url": link_el["href"] if link_el else "",
                    "description": desc_el.text.strip() if desc_el else "",
                    "author": author_el.text.strip() if author_el else "",
                    "source": f"itch.io/tag-{tag}"
                })
        except Exception as e:
            print(f"Error scraping tag '{tag}' page {page}: {e}")
    return leads


def collect_all_leads(conn):
    """Scrape all tags, deduplicate, skip already-seen URLs."""
    all_leads = []
    for tag in TAGS:
        print(f"Scraping: {tag}")
        all_leads.extend(scrape_itch_tag(tag, PAGES_PER_TAG))
        time.sleep(2)

    # Deduplicate within this run
    seen = set()
    unique = []
    for lead in all_leads:
        if lead["url"] and lead["url"] not in seen:
            # Skip if already in the database from a previous week
            if not already_seen(conn, lead["url"]):
                seen.add(lead["url"])
                unique.append(lead)

    print(f"Found {len(unique)} new unique leads.")
    return unique


# ============================================================
# CONTACT FINDER
# ============================================================
def find_contact_info(url):
    """Visit the game's page and look for emails / social links."""
    try:
        resp = requests.get(url, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")

        emails = re.findall(
            r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
            resp.text
        )

        socials = []
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if any(s in href for s in [
                "twitter.com", "x.com", "discord.gg", "discord.com",
                "mastodon", "bsky.app", "linkedin.com"
            ]):
                socials.append(href)

        parts = []
        if emails:
            parts.append("Email: " + ", ".join(set(list(emails)[:2])))
        if socials:
            parts.append("Links: " + ", ".join(set(list(socials)[:3])))
        return " | ".join(parts) if parts else "Check page"

    except Exception:
        return "Check page"


# ============================================================
# GEMINI ANALYSIS
# ============================================================
def analyze_lead(client, lead):
    """Ask Gemini to evaluate a lead."""
    prompt = f"""You are helping a freelance sound designer and composer 
find work. He specializes in:
- Cinematic atmospheric audio (FromSoftware-style tone and texture)
- Horror soundscapes (heavy, oppressive, tense)
- Gacha game audio (polished UI SFX, summoning sequences, character themes)
- Ambient and immersive worldbuilding audio

Analyze this game project. Be honest — if it's a bad fit, say so.

Game Title: {lead['title']}
Description: {lead['description']}
Developer: {lead['author']}
Source: {lead['source']}
URL: {lead['url']}

Return ONLY valid JSON, no markdown, no code fences:
{{
    "needs_sound_design": true or false,
    "needs_music": true or false,
    "confidence": "high" or "medium" or "low",
    "pitch_sfx": "One sentence: why they need a sound designer. Write N/A if false.",
    "pitch_music": "One sentence: why they need a composer. Write N/A if false.",
    "genre_match": "high" or "medium" or "low",
    "priority": 1 to 10,
    "reason": "One sentence summary of why this is or isn't a good lead."
}}"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        raw = response.text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"  Gemini error on '{lead['title']}': {e}")
        return None


# ============================================================
# MARKDOWN REPORT
# ============================================================
def generate_report(leads, date_str):
    """Write a markdown report for this week's leads."""
    lines = [
        f"# Lead Report — {date_str}",
        f"",
        f"**{len(leads)} leads found this week.**",
        f"",
    ]

    # Split into tiers
    hot = [l for l in leads if l.get("priority", 0) >= 7]
    warm = [l for l in leads if 4 <= l.get("priority", 0) < 7]
    cold = [l for l in leads if l.get("priority", 0) < 4]

    for tier_name, tier_leads in [("🔥 Hot Leads", hot), ("🟡 Warm Leads", warm), ("🔵 Cold Leads", cold)]:
        if not tier_leads:
            continue
        lines.append(f"## {tier_name}")
        lines.append("")

        for lead in tier_leads:
            sfx = "✅" if lead.get("needs_sound_design") else "❌"
            mus = "✅" if lead.get("needs_music") else "❌"

            lines.append(f"### {lead.get('title', 'Untitled')} — by {lead.get('author', '?')}")
            lines.append(f"")
            lines.append(f"- **Priority:** {lead.get('priority', '?')}/10")
            lines.append(f"- **Genre Match:** {lead.get('genre_match', '?')}")
            lines.append(f"- **Why:** {lead.get('reason', '')}")
            lines.append(f"- **Needs SFX:** {sfx} — {lead.get('pitch_sfx', 'N/A')}")
            lines.append(f"- **Needs Music:** {mus} — {lead.get('pitch_music', 'N/A')}")
            lines.append(f"- **Contact:** {lead.get('contact', 'Check page')}")
            lines.append(f"- **Link:** [{lead.get('url', '')}]({lead.get('url', '')})")
            lines.append(f"- **Source:** {lead.get('source', '')}")
            lines.append(f"")
            lines.append(f"---")
            lines.append(f"")

    report_path = f"reports/{date_str}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"Report written: {report_path}")
    return report_path


# ============================================================
# MAIN
# ============================================================
def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print("=" * 50)
    print(f"LEAD FINDER — {today}")
    print("=" * 50)

    # 1. Set up database
    conn = init_db()

    # 2. Scrape itch.io
    leads = collect_all_leads(conn)

    # 3. Cap at 80 for free tier safety
    leads = leads[:80]

    # 4. Set up Gemini
    client = genai.Client(api_key=GEMINI_KEY)

    # 5. Analyze each lead
    analyzed = []
    for i, lead in enumerate(leads):
        print(f"[{i+1}/{len(leads)}] {lead['title']}")
        result = analyze_lead(client, lead)

        if result:
            if result.get("priority", 0) >= 4:
                print(f"  -> Priority {result['priority']}. Grabbing contact info...")
                lead["contact"] = find_contact_info(lead["url"])
                time.sleep(1)
            else:
                lead["contact"] = "Low priority — skipped"

            lead.update(result)
            analyzed.append(lead)

            # Save to database immediately
            save_lead(conn, lead, today)

        time.sleep(7)  # Stay under 10 RPM

    # 6. Sort by priority
    analyzed.sort(key=lambda x: x.get("priority", 0), reverse=True)

    # 7. Generate markdown report
    if analyzed:
        generate_report(analyzed, today)
    else:
        # Write an empty report so the commit still works
        with open(f"reports/{today}.md", "w") as f:
            f.write(f"# Lead Report — {today}\n\nNo new leads found this week.\n")

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
