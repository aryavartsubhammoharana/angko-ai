import os
import json
import logging
from dotenv import load_dotenv

# Ensure environment variables are loaded
load_dotenv()

try:
    from upstash_redis import Redis
except ImportError:
    Redis = None

# Global in-memory cache as an ultra-fast fallback layer
_local_memory_cache = {}

_redis_client = None


def get_redis_client():
    """Lazily initialize and return the Upstash Redis client."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client

    url = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip()
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()

    if url and token:
        try:
            if Redis is not None:
                _redis_client = Redis(url=url, token=token)
                logging.info("Connected to Upstash Redis persistent cache via SDK.")
            else:
                logging.warning("upstash-redis package not installed, falling back to REST/memory.")
        except Exception as e:
            logging.warning("Failed to initialize Upstash Redis client: %s", e)
            _redis_client = None

    return _redis_client


def get_cached_transcript(video_id: str):
    """Retrieve cached transcript data for a video ID from Upstash Redis or memory."""
    if not video_id:
        return None

    cache_key = f"transcript:{video_id}"

    # 1. Check local memory first (instantaneous)
    if cache_key in _local_memory_cache:
        logging.info("Transcript Cache HIT (Memory): %s", video_id)
        return _local_memory_cache[cache_key]

    # 2. Check Upstash Redis (shared globally across all users)
    client = get_redis_client()
    if client:
        try:
            raw_val = client.get(cache_key)
            if raw_val:
                logging.info("Transcript Cache HIT (Upstash Redis): %s", video_id)
                data = json.loads(raw_val) if isinstance(raw_val, str) else raw_val
                # Sync into memory cache
                _local_memory_cache[cache_key] = data
                return data
        except Exception as e:
            logging.warning("Upstash Redis get error: %s", e)

    return None


def set_cached_transcript(video_id: str, transcript_data: dict, ttl_seconds: int = 604800):
    """Save transcript data into Upstash Redis and local memory cache.
    Default TTL: 7 days (604,800 seconds)."""
    if not video_id or not transcript_data:
        return

    cache_key = f"transcript:{video_id}"

    # 1. Save in local memory
    _local_memory_cache[cache_key] = transcript_data

    # 2. Save in Upstash Redis
    client = get_redis_client()
    if client:
        try:
            payload = json.dumps(transcript_data)
            # Store with expiration (7 days) to save storage
            client.set(cache_key, payload, ex=ttl_seconds)
            logging.info("Transcript saved to Upstash Redis: %s (TTL: %ds)", video_id, ttl_seconds)
        except Exception as e:
            logging.warning("Upstash Redis set error: %s", e)
