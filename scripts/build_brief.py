#!/usr/bin/env python3
"""Build brief.json for Daily Intelligence.

Pipeline:
  sources.json -> fetch RSS feeds -> pick a balanced set of stories ->
  download + extract the full article text -> Gemini writes the Key Facts ->
  brief.json

Usage:
  python scripts/build_brief.py                    # full run (needs GEMINI_API_KEY)
  python scripts/build_brief.py --no-ai            # no Gemini: test feeds + extraction without a key
  python scripts/build_brief.py --per-category 2   # small test run

Environment:
  GEMINI_API_KEY   your Google AI Studio key (set as a GitHub Actions secret; never commit it)
  GEMINI_MODEL     optional, comma-separated model IDs tried in order
                   (default: gemini-3.5-flash, gemini-3.1-flash-lite, gemini-2.5-flash)
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from itertools import zip_longest
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser

import feedparser
import requests
import trafilatura
from pydantic import BaseModel

try:
    from google import genai
    from google.genai import types
except ImportError:  # lets --no-ai runs work without the SDK
    genai = None
    types = None

ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = ROOT / "sources.json"
OUTPUT_PATH = ROOT / "brief.json"

# ----------------------------- Settings -----------------------------------
# Google has been restricting the older 2.5 models for new projects, so newer models come first.
# If one returns "not found", the script moves on to the next automatically.
GEMINI_MODELS = [
    m.strip()
    for m in os.environ.get(
        "GEMINI_MODEL", "gemini-3.5-flash,gemini-3.1-flash-lite,gemini-2.5-flash"
    ).split(",")
    if m.strip()
]
PER_CATEGORY = {"politics": 12, "technology": 8, "sports": 8, "gaming": 8}
LEAN_ORDER = ["left", "center", "right"]   # politics is balanced across these
MAX_AGE_HOURS = 48                         # prefer stories from the last two days
PER_FEED_LIMIT = 6                         # newest N entries considered per feed
MIN_FULLTEXT_CHARS = 700                   # below this we treat the page text as a partial preview
MIN_ANY_TEXT_CHARS = 200                   # below this the story is skipped entirely
MAX_CHARS_TO_GEMINI = 12000
SECONDS_BETWEEN_AI_CALLS = 6.5             # keeps us under free-tier per-minute limits
SECONDS_BETWEEN_PAGE_FETCHES = 0.7
REQUEST_TIMEOUT = 20
MIN_ARTICLES_TO_PUBLISH = 5

BOT_NAME = "DailyIntelligence"
USER_AGENT = (
    f"Mozilla/5.0 (compatible; {BOT_NAME}/1.0; "
    "+https://github.com/sofia-ls-dang/tailored-news)"
)
CATEGORY_LABELS = {
    "technology": "Technology",
    "sports": "Sports",
    "gaming": "Gaming",
}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en"})


def log(*parts):
    print(*parts, flush=True)


# ----------------------------- Text helpers -------------------------------
def html_to_paragraphs(raw):
    """Turn feed HTML into a list of plain-text paragraphs."""
    if not raw:
        return []
    text = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    paras = [re.sub(r"\s+", " ", p).strip() for p in text.split("\n")]
    return [p for p in paras if p]


def clean_text(raw):
    return " ".join(html_to_paragraphs(raw))


def split_paragraphs(text):
    return [p.strip() for p in text.split("\n") if p.strip()]


def extractive_facts(paragraphs):
    """Fallback when Gemini is unavailable: first few real sentences of the story."""
    text = " ".join(paragraphs)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    facts = [s.strip() for s in sentences if 40 <= len(s.strip()) <= 300][:3]
    if not facts and text:
        facts = [text[:200].strip()]
    return facts


def clean_url(url):
    """Strip tracking parameters and fragments, keep everything else."""
    parts = urlsplit(url.strip())
    drop = {"cmp", "ocid", "ref", "taid", "fbclid", "gclid", "source", "traffic_source"}
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in drop
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def valid_image(url):
    return url if isinstance(url, str) and url.startswith(("http://", "https://")) else ""


# ----------------------------- Fetching -----------------------------------
def fetch_feed(feed):
    try:
        r = SESSION.get(feed["url"], timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
        if not parsed.entries:
            raise ValueError("feed had no entries")
        return parsed.entries, None
    except Exception as e:  # noqa: BLE001 - we want to keep going on any failure
        return [], f"{type(e).__name__}: {e}"


def entry_to_candidate(entry, feed, category, now):
    url = entry.get("link")
    title = clean_text(entry.get("title", ""))
    if not url or not title:
        return None

    published = None
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            published = datetime(*t[:6], tzinfo=timezone.utc)
            break
    if published is None or published > now + timedelta(hours=1):
        published = now

    feed_html = ""
    if entry.get("content"):
        feed_html = entry["content"][0].get("value", "")
    if not feed_html:
        feed_html = entry.get("summary", "")

    image = ""
    for m in list(entry.get("media_content", [])) + list(entry.get("media_thumbnail", [])):
        image = valid_image(m.get("url"))
        if image:
            break
    if not image:
        for enc in entry.get("enclosures", []):
            if str(enc.get("type", "")).startswith("image"):
                image = valid_image(enc.get("href"))
                if image:
                    break
    if not image and feed_html:
        m = re.search(r"""<img[^>]+src=["']([^"']+)["']""", feed_html)
        if m:
            image = valid_image(m.group(1))

    return {
        "category": category,
        "feed": feed["name"],
        "lean": feed.get("lean"),
        "scope": feed.get("scope"),
        "url": clean_url(url),
        "title": title,
        "published": published,
        "feed_html": feed_html,
        "image": image,
    }


_robots_cache = {}


def allowed_by_robots(url):
    """Respect each site's robots.txt before downloading an article page."""
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    rp = _robots_cache.get(base)
    if rp is None:
        rp = RobotFileParser()
        try:
            r = SESSION.get(base + "/robots.txt", timeout=10)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:  # noqa: BLE001
            rp.parse([])
        _robots_cache[base] = rp
    return rp.can_fetch(BOT_NAME, url)


def fetch_article_page(url):
    """Download an article page and extract its main text. Returns dict or None."""
    if not allowed_by_robots(url):
        return None
    try:
        time.sleep(SECONDS_BETWEEN_PAGE_FETCHES)
        r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        if "html" not in r.headers.get("content-type", "").lower():
            return None
        raw = trafilatura.extract(
            r.text,
            output_format="json",
            with_metadata=True,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
        if not raw:
            return None
        data = json.loads(raw)
        return {"text": data.get("text") or "", "image": valid_image(data.get("image"))}
    except Exception as e:  # noqa: BLE001
        log(f"    page fetch failed: {type(e).__name__}")
        return None


# ----------------------------- Selection ----------------------------------
def dedupe(cands):
    seen, kept = set(), []
    for c in sorted(cands, key=lambda c: c["published"], reverse=True):
        key = c["url"].rstrip("/").lower()
        if key in seen:
            continue
        title = c["title"].lower()
        if any(SequenceMatcher(None, title, k["title"].lower()).ratio() > 0.85 for k in kept):
            continue
        seen.add(key)
        kept.append(c)
    return kept


def diversify(cands):
    """Interleave outlets so one publisher can't flood the list. Newest-first within each."""
    groups = defaultdict(list)
    for c in cands:
        groups[c["feed"]].append(c)
    ordered = sorted(groups.values(), key=lambda g: g[0]["published"], reverse=True)
    out = []
    for row in zip_longest(*ordered):
        out.extend(c for c in row if c is not None)
    return out


def pick(candidates, cfg, n, now):
    pool = dedupe(candidates)
    fresh = [c for c in pool if now - c["published"] <= timedelta(hours=MAX_AGE_HOURS)]
    pool = fresh if len(fresh) >= n else pool   # relax the age limit on slow news days

    if cfg.get("balanceByLean"):
        lists = []
        for lean in LEAN_ORDER:
            lists.append(diversify([c for c in pool if c["lean"] == lean]))
        picked = []
        for row in zip_longest(*lists):
            picked.extend(c for c in row if c is not None)
        return picked[:n]
    return diversify(pool)[:n]


# ----------------------------- Gemini -------------------------------------
class KeyFacts(BaseModel):
    key_facts: list[str]


_ai_state = {"last_call": 0.0, "disabled": False, "model_index": 0}


def make_client():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or genai is None:
        return None
    return genai.Client(api_key=key)


_NOT_TEXT_MODELS = ("image", "tts", "live", "audio", "embedding", "robotics", "computer-use", "imagen", "veo")


def discover_models(client):
    """Ask the API which Flash models this key can list, best first.

    Stable models come before preview ones, newer versions before older, and
    full Flash before Flash-Lite. Listing a model does not guarantee it can be
    called (Google has kept some restricted models in the list), so the 404
    fallback in gemini_key_facts() still applies.
    """
    found = {}
    try:
        for m in client.models.list():
            name = (getattr(m, "name", "") or "").replace("models/", "")
            actions = getattr(m, "supported_actions", None) or []
            if actions and "generateContent" not in actions:
                continue
            mt = re.fullmatch(r"gemini-(\d+(?:\.\d+)*)-flash(-lite)?(?:-(.+))?", name)
            if not mt:
                continue
            suffix = mt.group(3) or ""
            if any(w in name for w in _NOT_TEXT_MODELS):
                continue
            version = tuple(int(x) for x in mt.group(1).split("."))
            lite = bool(mt.group(2))
            unstable = suffix.startswith(("preview", "exp"))
            if re.fullmatch(r"\d{3}", suffix):
                continue   # pinned snapshots such as -001: the plain alias covers them
            found[name] = (unstable, [-x for x in version], lite)
    except Exception as e:  # noqa: BLE001
        log(f"Could not list Gemini models: {type(e).__name__}")
        return []
    return sorted(found, key=lambda n: found[n])


def advance_model(reason):
    """Move to the next model in GEMINI_MODELS. Returns False when none are left."""
    i = _ai_state["model_index"]
    if i + 1 < len(GEMINI_MODELS):
        _ai_state["model_index"] = i + 1
        log(f"    Gemini model '{GEMINI_MODELS[i]}' {reason}: trying '{GEMINI_MODELS[i + 1]}'")
        return True
    return False


def gemini_key_facts(client, title, publisher, paragraphs):
    if _ai_state["disabled"]:
        return None
    article_text = "\n".join(paragraphs)[:MAX_CHARS_TO_GEMINI]
    prompt = (
        "You summarize news articles for readers with ADHD who want the essentials fast.\n"
        "Write exactly 3 key facts about the article below.\n\n"
        "Rules:\n"
        "- Use only information in the article. Do not add outside knowledge.\n"
        "- Each fact is ONE plain sentence of 25 words or fewer, most important information first.\n"
        "- Neutral, factual wording. No opinion and no loaded adjectives. Attribute claims "
        "to whoever made them (for example: 'officials said').\n"
        "- Cover what happened, who is involved or why it matters, and what happens next "
        "(only if the article says).\n\n"
        f"Headline: {title}\nPublisher: {publisher}\n\nArticle:\n\"\"\"\n{article_text}\n\"\"\""
    )
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=KeyFacts,
        temperature=0.2,
    )
    soft_fails = 0
    for attempt in range(6):
        wait = SECONDS_BETWEEN_AI_CALLS - (time.time() - _ai_state["last_call"])
        if wait > 0:
            time.sleep(wait)
        _ai_state["last_call"] = time.time()
        try:
            model = GEMINI_MODELS[_ai_state["model_index"]]
            resp = client.models.generate_content(model=model, contents=prompt, config=config)
            facts = resp.parsed.key_facts if getattr(resp, "parsed", None) else json.loads(resp.text)["key_facts"]
            facts = [clean_text(f) for f in facts if f and f.strip()][:3]
            if facts:
                _ai_state["give_ups"] = 0
                return facts
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "PerDay" in msg or "per day" in msg.lower():
                # Free-tier quotas are per model, so another model may still have room
                if advance_model("hit its daily quota"):
                    continue
                log("    Gemini daily quota reached on every model: skipping AI for the rest of this run")
                _ai_state["disabled"] = True
                return None
            if "404" in msg or "NOT_FOUND" in msg:
                if advance_model("is not available"):
                    continue
                log("    No Gemini model was available: check GEMINI_MODEL or your key's access")
                _ai_state["disabled"] = True
                return None
            if "401" in msg or "403" in msg or "API key" in msg:
                log("    Gemini rejected the API key: check the GEMINI_API_KEY secret")
                _ai_state["disabled"] = True
                return None
            soft_fails += 1
            log(f"    Gemini attempt {attempt + 1} failed ({type(e).__name__}); retrying")
            if soft_fails >= 2 and advance_model("keeps returning errors (overloaded?)"):
                soft_fails = 0       # a different model may be healthy: try it right away
                continue
            time.sleep(5 * (attempt + 1))
    # Gave up on this story. If this keeps happening, stop wasting the run.
    _ai_state["give_ups"] = _ai_state.get("give_ups", 0) + 1
    if _ai_state["give_ups"] >= 3:
        log("    Gemini failed on 3 stories in a row: skipping AI for the rest of this run")
        _ai_state["disabled"] = True
    return None


# ----------------------------- Assembly -----------------------------------
def load_previous():
    try:
        data = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        return {a["id"]: a for a in data.get("articles", []) if "id" in a}
    except Exception:  # noqa: BLE001
        return {}


def category_label(c):
    if c["category"] == "politics":
        return "World" if c.get("scope") == "world" else "Politics"
    return CATEGORY_LABELS.get(c["category"], c["category"].title())


def build_article(c, previous, client):
    art_id = hashlib.sha1(c["url"].encode("utf-8")).hexdigest()[:10]

    old = previous.get(art_id)
    if old and old.get("keyFacts") and (old.get("aiSummary") or client is None):
        return old   # already processed on an earlier run: reuse, no new Gemini call

    page = fetch_article_page(c["url"])
    page_text = page["text"] if page else ""
    feed_paras = html_to_paragraphs(c["feed_html"])
    feed_text = " ".join(feed_paras)

    if len(page_text) >= len(feed_text):
        paragraphs = split_paragraphs(page_text)
    else:
        paragraphs = feed_paras
    full_text = len(page_text) >= MIN_FULLTEXT_CHARS

    if sum(len(p) for p in paragraphs) < MIN_ANY_TEXT_CHARS:
        return None

    image = c["image"] or (page["image"] if page else "")

    facts, ai = None, False
    if client is not None:
        facts = gemini_key_facts(client, c["title"], c["feed"], paragraphs)
        ai = bool(facts)
    if not facts:
        facts = extractive_facts(paragraphs)

    words = sum(len(p.split()) for p in paragraphs)
    article = {
        "id": art_id,
        "category": c["category"],
        "categoryLabel": category_label(c),
        "headline": c["title"],
        "publisher": c["feed"],
        "readMinutes": max(1, round(words / 220)),
        "publishedAt": c["published"].isoformat(),
        "image": image,
        "imageLarge": image,
        "url": c["url"],
        "keyFacts": facts,
        "body": paragraphs,
        "fullText": full_text,
        "aiSummary": ai,
    }
    if c.get("lean"):
        article["lean"] = c["lean"]
    return article


def choose_featured(articles):
    def score(a):
        return (
            a["category"] == "politics",
            a.get("fullText", False),
            bool(a.get("image")),
            a["publishedAt"],
        )
    return max(articles, key=score)["id"] if articles else None


def parse_args():
    ap = argparse.ArgumentParser(description="Build brief.json")
    ap.add_argument("--no-ai", action="store_true", help="skip Gemini (extractive Key Facts instead)")
    ap.add_argument("--per-category", type=int, help="override stories per category (testing)")
    return ap.parse_args()


def main():
    args = parse_args()
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))["categories"]
    previous = load_previous()
    now = datetime.now(timezone.utc)

    client = None
    if not args.no_ai:
        client = make_client()
        if client is None:
            log("No GEMINI_API_KEY (or google-genai missing): continuing without AI summaries")
        elif "GEMINI_MODEL" not in os.environ:
            found = discover_models(client)
            if found:
                GEMINI_MODELS[:] = found
                log("Gemini models this key can list (best first): " + ", ".join(found[:6]))
            else:
                log("Using default model list: " + ", ".join(GEMINI_MODELS))

    report, articles = {}, []
    for category, cfg in sources.items():
        n = args.per_category or PER_CATEGORY.get(category, 8)
        log(f"\n== {category} (target {n}) ==")

        candidates = []
        for feed in cfg["feeds"]:
            entries, err = fetch_feed(feed)
            report[f"{category}/{feed['name']}"] = err or f"ok ({len(entries)} entries)"
            if err:
                log(f"  feed failed: {feed['name']}: {err}")
            for entry in entries[:PER_FEED_LIMIT]:
                c = entry_to_candidate(entry, feed, category, now)
                if c:
                    candidates.append(c)

        for c in pick(candidates, cfg, n, now):
            log(f"  [{c['feed']}] {c['title'][:70]}")
            art = build_article(c, previous, client)
            if art:
                articles.append(art)
            else:
                log("    skipped: not enough article text")

    if len(articles) < MIN_ARTICLES_TO_PUBLISH:
        log(f"\nOnly {len(articles)} stories were built: keeping the existing brief.json")
        return 1

    articles.sort(key=lambda a: a["publishedAt"], reverse=True)
    for a in articles:
        a.pop("featured", None)
    featured_id = choose_featured(articles)
    for a in articles:
        if a["id"] == featured_id:
            a["featured"] = True

    out = {
        "generatedAt": now.isoformat(),
        "model": GEMINI_MODELS[_ai_state["model_index"]] if any(a.get("aiSummary") for a in articles) else None,
        "articles": articles,
        "feedReport": report,
    }
    tmp = OUTPUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(OUTPUT_PATH)

    failed = [k for k, v in report.items() if not v.startswith("ok")]
    log(f"\nWrote {len(articles)} stories to {OUTPUT_PATH.name}")
    log(f"  full text: {sum(a['fullText'] for a in articles)}/{len(articles)}")
    log(f"  AI summaries: {sum(a['aiSummary'] for a in articles)}/{len(articles)}")
    if failed:
        log(f"  feeds that failed ({len(failed)}): " + ", ".join(failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())