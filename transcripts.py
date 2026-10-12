import os
import glob
import shutil
import tempfile
import uuid
import logging
import requests
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from redis_cache import get_cached_transcript, set_cached_transcript


def find_ffmpeg_binaries():
    """Locate ffmpeg and ffprobe on Windows or other systems."""
    candidates = []
    for name in ["ffmpeg", "ffprobe"]:
        path = shutil.which(name)
        if path:
            candidates.append((name, path))
            continue

        common_paths = [
            os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "yt-dlp", "FFmpeg", "bin"),
            os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "ffmpeg", "bin"),
            os.path.join(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"), "ffmpeg", "bin"),
            r"C:\Program Files\KMPlayer 64X\LAVFilters64",
            r"C:\Program Files\Git\usr\bin",
        ]
        for folder in common_paths:
            candidate = os.path.join(folder, name + ".exe" if os.name == "nt" else name)
            if os.path.exists(candidate):
                candidates.append((name, candidate))
                break

    ffmpeg = next((path for name, path in candidates if name == "ffmpeg"), None)
    ffprobe = next((path for name, path in candidates if name == "ffprobe"), None)
    return ffmpeg, ffprobe


def extract_transcript_via_ytdlp(video_id: str):
    """Fallback subtitle extractor using yt-dlp with client spoofing.
    Safely bypassed in cloud serverless environments (like Vercel) where YouTube blocks datacenter IPs."""
    # On serverless hosts (Vercel, AWS Lambda), YouTube blocks datacenter IPs with "Sign in to confirm you're not a bot"
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        logging.info("Serverless environment detected (Vercel/Lambda). Skipping local yt-dlp to avoid datacenter bot block.")
        return None

    try:
        canonical_url = f"https://www.youtube.com/watch?v={video_id}"
        ydl_opts = {
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'quiet': True,
            'no_warnings': True,
            'extractor_args': {'youtube': ['client=ANDROID,IOS']}
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(canonical_url, download=False)
            sub_dict = info.get('subtitles', {}) or {}
            auto_dict = info.get('automatic_captions', {}) or {}

            # Prioritize standard English/Hindi or any first available
            target_track = None
            source_lang = 'YouTube Subtitles'
            for lang in ['en', 'hi', 'en-US', 'en-GB']:
                if lang in sub_dict:
                    target_track = sub_dict[lang]
                    source_lang = f'YouTube Subtitles ({lang})'
                    break
                if lang in auto_dict:
                    target_track = auto_dict[lang]
                    source_lang = f'YouTube Auto-Captions ({lang})'
                    break

            if not target_track and sub_dict:
                lang_key = next(iter(sub_dict))
                target_track = sub_dict[lang_key]
                source_lang = f'YouTube Subtitles ({lang_key})'
            if not target_track and auto_dict:
                lang_key = next(iter(auto_dict))
                target_track = auto_dict[lang_key]
                source_lang = f'YouTube Auto-Captions ({lang_key})'

            if not target_track:
                return None

            # Prefer json3 format URL
            json3_url = next((f['url'] for f in target_track if f.get('ext') == 'json3'), None)
            if not json3_url:
                json3_url = target_track[0].get('url')

            if not json3_url:
                return None

            resp = requests.get(json3_url, timeout=12)
            if resp.status_code != 200:
                return None

            data = resp.json()
            segments = []
            for event in data.get('events', []):
                if 'segs' in event:
                    seg_text = ''.join(s.get('utf8', '') for s in event.get('segs', [])).strip()
                    if seg_text and seg_text != '\n':
                        start_sec = float(event.get('tStartMs', 0)) / 1000.0
                        dur_sec = float(event.get('dDurationMs', 0)) / 1000.0
                        segments.append({
                            'text': seg_text,
                            'start': start_sec,
                            'duration': dur_sec
                        })

            if not segments:
                return None

            full_text = " ".join([seg['text'] for seg in segments if seg['text']])
            if not full_text.strip():
                return None

            return {
                'text': full_text,
                'segments': segments,
                'language': source_lang,
                'language_code': 'auto',
                'source': 'YouTube Subtitles (InnerTube)'
            }
    except Exception as e:
        logging.warning("yt-dlp subtitle extraction fallback failed or blocked by YouTube: %s", e)
        return None


def fetch_youtube_transcript(video_id: str):
    """Attempt to fetch transcript from YouTube directly using multi-tier fallback.
    Tries YouTubeTranscriptApi first, then yt-dlp if available."""
    raw_data = None
    source_language = 'YouTube Subtitles'

    # Priority 2: Modern YouTubeTranscriptApi instance (1.x+)
    try:
        ytt = YouTubeTranscriptApi()

        # Priority 1: Request preferred languages (en, hi, ur)
        try:
            fetched = ytt.fetch(video_id, languages=['en', 'en-US', 'hi', 'ur'])
            raw_data = fetched.to_raw_data() if hasattr(fetched, 'to_raw_data') else list(fetched)
            source_language = 'YouTube Subtitles'
        except Exception as e_pref:
            logging.debug("Preferred languages fetch failed: %s", e_pref)

        # Priority 2: Request default transcript without language filter
        if not raw_data:
            try:
                fetched = ytt.fetch(video_id)
                raw_data = fetched.to_raw_data() if hasattr(fetched, 'to_raw_data') else list(fetched)
                source_language = 'YouTube Subtitles'
            except Exception as e_def:
                logging.debug("Default transcript fetch failed: %s", e_def)

        # Priority 3: Iterate through available transcript list (manual or auto-generated)
        if not raw_data:
            try:
                t_list = ytt.list(video_id)
                target_transcript = None
                try:
                    target_transcript = t_list.find_transcript(['en', 'en-US', 'hi', 'ur'])
                except Exception:
                    pass

                if not target_transcript:
                    try:
                        target_transcript = t_list.find_generated_transcript(['en', 'en-US', 'hi', 'ur'])
                    except Exception:
                        pass

                if not target_transcript:
                    for t in t_list:
                        target_transcript = t
                        break

                if target_transcript:
                    fetched = target_transcript.fetch()
                    raw_data = fetched.to_raw_data() if hasattr(fetched, 'to_raw_data') else list(fetched)
                    source_language = getattr(target_transcript, 'language', 'YouTube Subtitles')
            except Exception as e_list:
                logging.debug("Modern transcript list iteration failed: %s", e_list)
    except Exception as e_init:
        logging.debug("Modern YouTubeTranscriptApi instance failed: %s", e_init)

    # 2. Legacy class methods fallback (0.6.x)
    if not raw_data and hasattr(YouTubeTranscriptApi, 'get_transcript'):
        try:
            raw_data = YouTubeTranscriptApi.get_transcript(video_id, languages=['en', 'hi', 'ur'])
            source_language = 'YouTube Subtitles'
        except Exception:
            try:
                raw_data = YouTubeTranscriptApi.get_transcript(video_id)
                source_language = 'YouTube Subtitles'
            except Exception:
                try:
                    transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
                    for t in transcript_list:
                        raw_data = t.fetch()
                        source_language = getattr(t, 'language', 'YouTube Subtitles')
                        break
                except Exception as e_leg:
                    logging.debug("Legacy transcript list failed: %s", e_leg)

    # 3. If standard transcript API failed, retry yt-dlp InnerTube extractor
    if not raw_data:
        logging.info("YouTubeTranscriptApi returned no data. Trying yt-dlp InnerTube subtitle extractor...")
        ytdlp_result = extract_transcript_via_ytdlp(video_id)
        if ytdlp_result and ytdlp_result.get('text'):
            return ytdlp_result

    if not raw_data:
        return None

    if hasattr(raw_data, 'to_raw_data'):
        try:
            raw_data = raw_data.to_raw_data()
        except Exception:
            pass

    # Normalize segment data into uniform Python dictionaries
    normalized_segments = []
    for item in raw_data:
        if isinstance(item, dict):
            text = str(item.get('text', '') or '')
            start = float(item.get('start', 0.0) or 0.0)
            duration = float(item.get('duration', 0.0) or 0.0)
        else:
            text = str(getattr(item, 'text', '') or '')
            start = float(getattr(item, 'start', 0.0) or 0.0)
            duration = float(getattr(item, 'duration', 0.0) or 0.0)

        if text.strip():
            normalized_segments.append({
                'text': text,
                'start': start,
                'duration': duration
            })

    full_text = " ".join([seg['text'] for seg in normalized_segments if seg['text']])
    if not full_text.strip():
        return None

    return {
        'text': full_text,
        'segments': normalized_segments,
        'language': source_language,
        'language_code': 'auto',
        'source': 'YouTube Subtitles'
    }


def fetch_transcript_via_supadata(video_url: str, supadata_key: str, lang=None):
    """Fetch transcript via Supadata AI universal transcript endpoint.
    Handles both direct JSON segments and plain-text fallbacks."""
    if not supadata_key:
        return None

    try:
        logging.info("Attempting Supadata universal transcript API for %s...", video_url)
        params = {'url': video_url, 'text': 'false'}
        if lang:
            params['lang'] = lang

        headers = {'x-api-key': supadata_key}
        resp = requests.get(
            'https://api.supadata.ai/v1/transcript',
            headers=headers,
            params=params,
            timeout=25
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
            # If string returned, create a single baseline segment
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
        logging.warning("Supadata transcript error: %s", e)
        return None


def transcribe_audio_fallback(url: str, groq_key: str):
    """Download audio and transcribe using Groq Whisper if no subtitles are available on YouTube."""
    if not groq_key:
        raise RuntimeError(
            "This video does not have subtitles/transcripts available on YouTube. "
            "Please configure a valid Groq API Key in .env to enable audio transcription fallback."
        )

    # On serverless hosts without external proxies, yt-dlp audio download is blocked by YouTube datacenter protection
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        raise RuntimeError("Audio extraction directly on serverless datacenter IP is restricted by YouTube.")

    # Disk space pre-check (require at least 100 MB free)
    temp_dir = tempfile.gettempdir()
    free_bytes = shutil.disk_usage(temp_dir).free
    if free_bytes < 100 * 1024 * 1024:
        raise RuntimeError("Insufficient server disk space for audio processing.")

    unique_suffix = uuid.uuid4().hex[:8]
    output_template = os.path.join(temp_dir, f'veda_temp_audio_{unique_suffix}_%(id)s.%(ext)s')

    # Streamlined yt-dlp config: prefers m4a/webm directly without requiring ffmpeg
    ydl_opts = {
        'format': 'ba[ext=m4a]/ba[ext=webm]/bestaudio/best',
        'outtmpl': output_template,
        'quiet': True,
        'noplaylist': True,
        'no_warnings': True,
        'extractor_args': {'youtube': ['client=IOS,ANDROID,WEB']}
    }

    ffmpeg_path, _ = find_ffmpeg_binaries()
    if ffmpeg_path:
        ydl_opts['ffmpeg_location'] = os.path.dirname(ffmpeg_path)
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',
        }]

    logging.info("Downloading audio for transcription fallback...")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_id = info.get('id')

        # Locate downloaded audio file (.m4a, .webm, or .mp3)
        candidate_files = glob.glob(os.path.join(temp_dir, f'veda_temp_audio_{unique_suffix}_*'))
        if not candidate_files:
            raise FileNotFoundError("Audio file download failed.")
        audio_file = candidate_files[0]

        logging.info("Transcribing via Groq Whisper HTTP API...")
        with open(audio_file, "rb") as file:
            audio_bytes = file.read()

        ext = os.path.splitext(audio_file)[1].lstrip('.').lower() or 'm4a'
        mime = f'audio/{ext}' if ext in ('mp3', 'wav', 'ogg', 'webm') else 'audio/mp4'

        headers = {'Authorization': f'Bearer {groq_key}'}
        files = {'file': (os.path.basename(audio_file), audio_bytes, mime)}
        data = {'model': 'whisper-large-v3', 'response_format': 'verbose_json'}

        groq_resp = requests.post(
            'https://api.groq.com/openai/v1/audio/transcriptions',
            headers=headers,
            files=files,
            data=data,
            timeout=180
        )

        if groq_resp.status_code != 200:
            raise RuntimeError(f"Groq API error ({groq_resp.status_code}): {groq_resp.text}")

        translation = groq_resp.json()

        def get_field(item, name, default=None):
            if isinstance(item, dict):
                return item.get(name, default)
            return getattr(item, name, default)

        segments = []
        translation_segments = get_field(translation, 'segments')
        if translation_segments:
            for seg in translation_segments:
                start = float(get_field(seg, 'start', 0.0) or 0.0)
                end = float(get_field(seg, 'end', start) or start)
                segments.append({
                    'text': get_field(seg, 'text', ''),
                    'start': start,
                    'duration': max(0.0, end - start)
                })
            full_text = get_field(translation, 'text', '') or " ".join(
                segment['text'] for segment in segments if segment.get('text')
            )
        else:
            full_text = translation.get('text', '') if isinstance(translation, dict) else str(translation)
            segments = [{'text': full_text, 'start': 0.0, 'duration': info.get('duration', 0.0)}]

        return {
            'text': full_text,
            'segments': segments,
            'language': 'English (Auto-transcribed)',
            'language_code': 'en',
            'source': 'Groq Whisper V3'
        }
    finally:
        # Secure cleanup: remove all temporary audio files generated with this unique suffix
        for pattern_file in glob.glob(os.path.join(temp_dir, f'veda_temp_audio_{unique_suffix}_*')):
            try:
                os.remove(pattern_file)
            except Exception:
                pass


def get_transcript_with_fallback(video_id: str, canonical_url: str, supadata_key: str = "", groq_key: str = ""):
    """Master transcript orchestrator with Redis persistent caching.
    1. Checks Upstash Redis / local memory cache first (0 API credits spent, instant).
    2. Tier 1: YouTube subtitles via InnerTube / YouTubeTranscriptApi (100% Free).
    3. Tier 2: Audio download + Groq Whisper V3 fallback (Fast & Free).
    4. Tier 3: Supadata AI universal transcript endpoint (Final fallback to preserve credits).
       -> As soon as any transcript is fetched, it is immediately committed to Redis.
    """
    # 1. Check Redis Cache
    cached = get_cached_transcript(video_id)
    if cached and cached.get('text'):
        logging.info("Using cached transcript for %s (Zero API calls)", video_id)
        cached['cached'] = True
        return cached

    transcript_data = None

    # 2. Tier 1: YouTube direct subtitles (Free)
    try:
        transcript_data = fetch_youtube_transcript(video_id)
    except Exception as e:
        logging.warning("Tier 1 direct transcript failed: %s", e)

    # 3. Tier 2 (Step 3): Groq Whisper V3 audio download fallback (Free)
    if not transcript_data and groq_key:
        logging.info("YouTube direct subtitles unavailable. Attempting Groq Whisper audio fallback...")
        try:
            transcript_data = transcribe_audio_fallback(canonical_url, groq_key)
            if transcript_data and transcript_data.get('text'):
                set_cached_transcript(video_id, transcript_data)
        except Exception as whisper_err:
            logging.warning("Groq Whisper transcription fallback failed: %s", whisper_err)

    # 4. Tier 3 (Step 4): Supadata AI fallback (Ultimate final fallback - protects credits)
    if not transcript_data and supadata_key:
        logging.info("Attempting final fallback via Supadata AI transcript API for %s...", video_id)
        try:
            transcript_data = fetch_transcript_via_supadata(canonical_url, supadata_key)
            if transcript_data and transcript_data.get('text'):
                # IMMEDIATELY persist to Redis so future requests don't burn credits
                set_cached_transcript(video_id, transcript_data)
        except Exception as supa_err:
            logging.warning("Supadata transcript fallback error: %s", supa_err)

    if not transcript_data or not transcript_data.get('text'):
        raise RuntimeError("All transcript methods (YouTube subtitles, Groq Whisper, Supadata) were unable to fetch a transcript for this video.")

    # Cache Tier 1 result as well for future instant access
    if transcript_data and transcript_data.get('text') and not transcript_data.get('cached'):
        set_cached_transcript(video_id, transcript_data)

    return transcript_data
