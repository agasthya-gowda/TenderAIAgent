"""
AI Learner
Uses DeepSeek to analyze CEO search patterns
and expand keywords from search #1 instantly.
"""

import json
import requests
import os
from dotenv import load_dotenv
from database.db import get_all_ceo_searches, save_learned_patterns, get_learned_patterns

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

def call_deepseek(prompt: str) -> str:
    """Call DeepSeek API."""
    try:
        response = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2000
            },
            timeout=30
        )
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"DeepSeek failed: {e}")
        return call_anthropic(prompt)

def call_anthropic(prompt: str) -> str:
    """Fallback to Claude if DeepSeek fails."""
    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        return response.json()["content"][0]["text"]
    except Exception as e:
        print(f"Anthropic failed: {e}")
        return ""

def expand_keyword_instantly(keyword: str) -> dict:
    """
    From search #1 — instantly expand one keyword
    into 15 related search terms for both portals.
    """
    prompt = f"""You are a government tender search expert in India.

A CEO just searched for: "{keyword}"

Expand this into related search terms for government tender portals (GeM and KPPP Karnataka).

Respond ONLY in this exact JSON format, no extra text:
{{
  "category": "Main category name",
  "original": "{keyword}",
  "expanded_keywords": [
    "keyword1",
    "keyword2",
    "keyword3",
    "keyword4",
    "keyword5",
    "keyword6",
    "keyword7",
    "keyword8",
    "keyword9",
    "keyword10",
    "keyword11",
    "keyword12",
    "keyword13",
    "keyword14",
    "keyword15"
  ]
}}"""

    response = call_deepseek(prompt)

    try:
        response = response.strip()
        if "```json" in response:
            response = response.split("```json")[1].split("```")[0]
        elif "```" in response:
            response = response.split("```")[1].split("```")[0]

        data = json.loads(response)

        # Save to learned patterns immediately
        all_keywords = [keyword] + data.get("expanded_keywords", [])
        save_learned_patterns(
            category=data.get("category", keyword.title()),
            keywords=json.dumps(all_keywords),
            weight=1.0
        )

        print(f"[Learner] Expanded '{keyword}' into {len(all_keywords)} keywords")
        return data

    except Exception as e:
        print(f"[Learner] Expansion failed: {e}")
        return {
            "category": keyword.title(),
            "original": keyword,
            "expanded_keywords": [keyword]
        }

def analyze_patterns() -> dict:
    """
    After multiple CEO searches — analyze full patterns
    and update learned categories with weights.
    """
    searches = get_all_ceo_searches()
    if not searches:
        return {}

    search_summary = "\n".join([
        f"- '{s['keyword']}' on {s['portal']} (searched {s['frequency']} times)"
        for s in searches
    ])

    prompt = f"""You are analyzing a CEO's government tender search behavior in India.

CEO searches so far:
{search_summary}

Identify patterns and generate smart search queries.

Respond ONLY in this exact JSON format:
{{
  "categories": [
    {{
      "name": "Category Name",
      "keywords": ["kw1", "kw2", "kw3", "kw4", "kw5"],
      "weight": 0.9
    }}
  ],
  "summary": "One sentence summary"
}}"""

    response = call_deepseek(prompt)

    try:
        response = response.strip()
        if "```json" in response:
            response = response.split("```json")[1].split("```")[0]
        elif "```" in response:
            response = response.split("```")[1].split("```")[0]

        data = json.loads(response)

        for cat in data.get("categories", []):
            save_learned_patterns(
                category=cat["name"],
                keywords=json.dumps(cat["keywords"]),
                weight=cat.get("weight", 0.5)
            )

        return data

    except Exception as e:
        print(f"[Learner] Pattern analysis failed: {e}")
        return {}

def get_search_queries() -> list:
    """
    Return list of search queries to run on portals.
    Based on learned patterns.
    """
    patterns = get_learned_patterns()
    queries = []

    for pattern in patterns:
        try:
            keywords = json.loads(pattern["keywords"])
            # Each pattern is tagged with the portal it was actually
            # searched on (see save_learned_patterns) — a keyword typed
            # into KPPP's Tender Name box must never get cross-applied to
            # GeM (or vice versa) just because they share the same
            # learned_patterns table. Previously this emitted both
            # unconditionally, so e.g. a KPPP-only "camera" search quietly
            # re-ran on GeM too on every future Start Agent click.
            pattern_portal = pattern.get("portal") or "GeM"
            for kw in keywords[:8]:
                queries.append({
                    "keyword": kw,
                    "portal": pattern_portal,
                    "category": pattern["category"]
                })
        except Exception:
            pass

    return queries