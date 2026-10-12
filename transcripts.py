import os
import logging
import requests
from redis_cache import get_cached_transcript, set_cached_transcript


def fetch_transcript_via_supadata(video_url: str, supadata_key: str, lang=None):
    """Fetch transcript exclusively via Supadata AI API endpoint.
    Handles JSON segments and plain-text fallbacks."""
    if not supadata_key:
        raise ValueError("SUPADATA_API key is not configured in environment.")

    try:
        logging.info("Calling Supadata API for transcript: %s...", video_url)
        params = {'url': video_url, 'text': 'false'}
        if lang:
            params['lang'] = lang

        headers = {'x-api-key': supadata_key}
        resp = requests.get(
            'https://api.supadata.ai/v1/transcript',
            headers=headers,
            params=params,
            timeout=30
        )

        if resp.status_code != 200:
            logging.warning("Supadata transcript API returned HTTP %s: %s", resp.status_code, resp.text)
            return None

        data = resp.json()
        if not isinstance(data, dict):
            return None

        content = data.get('content')
        detected_lang = data.get('lang', 'auto')

        normalized_segments = []
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    txt = str(item.get('text', '') or '').strip()
                    if not txt:
                        continue
                    # Supadata offset and duration are in milliseconds
                    offset_ms = float(item.get('offset', 0.0) or 0.0)
                    dur_ms = float(item.get('duration', 0.0) or 0.0)
                    normalized_segments.append({
                        'text': txt,
                        'start': round(offset_ms / 1000.0, 2),
                        'duration': round(dur_ms / 1000.0, 2)
                    })
        elif isinstance(content, str) and content.strip():
            normalized_segments.append({
                'text': content.strip(),
                'start': 0.0,
                'duration': 0.0
            })

        if not normalized_segments:
            return None

        full_text = " ".join([seg['text'] for seg in normalized_segments if seg['text']])
        if not full_text.strip():
            return None

        return {
            'text': full_text,
            'segments': normalized_segments,
            'language': f"Supadata Transcript ({detected_lang})",
            'language_code': detected_lang,
            'source': 'Supadata AI'
        }
    except Exception as e:
        logging.error("Supadata API transcript error: %s", e)
        return None


def get_transcript_with_fallback(video_id: str, canonical_url: str, supadata_key: str = "", **kwargs):
    """Clean, dedicated transcript orchestrator:
    1. Checks Upstash Redis / memory cache first (0 API cost, instant).
    2. If cache miss, calls Supadata API directly (100% reliable, zero bot block issues on Vercel).
    3. As soon as Supadata returns the transcript, it is immediately persisted to Upstash Redis.
    """
    # 1. Check Redis Cache
    cached = get_cached_transcript(video_id)
    if cached and cached.get('text'):
        logging.info("Using cached transcript for %s (Zero API credits used)", video_id)
        cached['cached'] = True
        return cached

    # 2. Call Supadata API directly
    if not supadata_key:
        raise RuntimeError("Supadata API key is not configured. Please add SUPADATA_API to environment.")

    transcript_data = fetch_transcript_via_supadata(canonical_url, supadata_key)

    if not transcript_data or not transcript_data.get('text'):
        raise RuntimeError("Failed to fetch transcript from Supadata API. The video might not have captions or speech.")

    # 3. Immediately save to Upstash Redis cache (7 days TTL)
    set_cached_transcript(video_id, transcript_data)

    return transcript_data
