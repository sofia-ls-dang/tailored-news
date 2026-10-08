# tailored-news
Atonomous, zero-cost, cross-platform Progressive Web App (PWA) designed to deliver curated, noise-free morning and evening news digests covering World/Politics, Technology, and Sports. The platform specifically targets readers with ADHD by eliminating "wall-of-text" fatigue and cognitive overload through structured visual hierarchy, executive key takeaway anchors, and distraction-free typography.

It ingests breaking world/politics, technology, and sports news via RSS, summarizes each story with Gemini 2.5 Flash, and presents it in a distraction-free feed with an executive “Key Facts” box, clear paragraph rhythm, and one-tap bookmarks. Built with HTML, Tailwind CSS, and vanilla JavaScript, with a Python and GitHub Actions pipeline generating the daily feed.

---

## 📌 Problem & Vision

Traditional news readers and aggregators present dense columns, distracting banners, and inconsistent pacing that trigger cognitive friction and decision fatigue. 

The **Daily Intelligence Platform** solves this by:
* **Curating & Filtering:** Autonomously aggregating multi-source RSS feeds and using **Google Gemini 2.5 Flash** to deduplicate redundant coverage and extract high-signal facts.
* **Structuring for Focus (ADHD-Optimized UX):** Pre-digesting articles into high-contrast **Key Takeaway** anchor boxes, narrow reading measures (max 640px), generous line spacing, and clean category tagging before linking to the source material.
* **Frictionless Delivery:** Packaging the client as a zero-cost PWA that installs seamlessly to the iOS Home Screen and Windows desktop without third-party app store fees or provisioning limits.

---

## 🏗️ System Architecture

```text
[ Multi-Source RSS Feeds ] (BBC World, Techmeme, ESPN)
              │
              ▼ (Python / Feedparser)
[ Serverless Ingestion Pipeline ] (GitHub Actions Cron: 6:30 AM & 8:30 PM PT)
              │
              ▼ (Strict Schema via Pydantic)
[ Google Gemini 2.5 Flash API ] (Deduplication, Fact Extraction, 2-Sentence Briefs)
              │
              ▼ (Automated Git Commit)
[ Static Storage & Delivery ] (docs/brief.json via GitHub Pages CDN)
              │
              ▼ (Asynchronous Client Fetch & LocalStorage Cache)
[ Cross-Platform PWA Interface ] (Installed on iOS & Windows Desktop)
```

| Component | Technology | Role & Function |
| :--- | :--- | :--- |
| **AI Summarization** | Google Gemini 2.5 Flash | Deduplicates headlines and generates concise 2-sentence briefs with strict JSON schemas |
| **Data Ingestion** | Python 3.11 (`feedparser`, `pydantic`) | Fetches RSS feeds (BBC, Techmeme, ESPN) and validates structured output |
| **Automation** | GitHub Actions | Serverless cron job running twice daily (morning & night) at zero cost |
| **Hosting & CDN** | GitHub Pages | Serves static frontend files and `brief.json` directly from the `/docs` folder |
| **Frontend UI** | HTML5, Tailwind CSS, Vanilla JS | ADHD-friendly reading view with narrow measure, key takeaways, and bookmarking |
| **App Delivery** | Progressive Web App (PWA) | Installs natively on iOS and Windows without app store fees or weekly re-signing |

## ✨ Key Features

* **🧠 ADHD-Optimized Article Reader:**
  * **Executive "Key Facts" Anchor:** High-contrast callout summarizing essential takeaways in 2–3 punchy bullet points before the main text.
  * **Cognitive-Load Safeguards:** Narrow line measure (max 640px) paired with generous `1.75` line spacing to prevent visual wander and eye fatigue.
  * **Direct Attribution:** Immediate "View Original Source ↗" action links for seamless deep-dives and credibility verification.

* **⚡ Autonomous News Ingestion:**
  * Scheduled serverless cron pipeline running unattended twice daily (morning & night).
  * Automatically pulls, filters, and deduplicates multi-source feeds across **World & Politics**, **Technology**, and **Sports**.
  * Structured output enforcement via Pydantic to guarantee zero runtime breaking changes.

* **📲 Frictionless PWA Deployment:**
  * Native-like installation on **iOS (Safari)** and **Windows (Edge/Chrome)**.
  * Completely bypasses App Store paywalls, developer program fees, and 7-day personal certificate expirations.

* **📑 Local Bookmark Management:**
  * One-tap bookmark toggle saving articles directly to browser `localStorage` for offline reference without account creation or database overhead.

* **💸 100% Free & Serverless Stack:**
  * Zero recurring operational costs leveraging GitHub Actions, GitHub Pages, and Google Gemini Flash free tiers.
