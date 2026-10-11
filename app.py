import os
import re
import glob
import shutil
import tempfile
import uuid
import time
import logging
from collections import defaultdict
from threading import Lock
from urllib.parse import urlparse, parse_qs, quote_plus
import requests
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai
from groq import Groq
from openai import OpenAI

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
)

# Load environment variables once at startup
load_dotenv()

app = Flask(__name__)

# Security: Set secret key and maximum request body size (64 KB)
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or os.urandom(32).hex()
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024


@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({'error': 'Request payload too large. Maximum size is 64 KB.'}), 413


@app.after_request
def set_security_headers(response):
    """Inject strict HTTP security headers on all responses."""
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'geolocation=(), camera=(), microphone=()'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https://i.ytimg.com https://*.ytimg.com https://images.unsplash.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    return response


class SimpleRateLimiter:
    """Thread-safe in-memory sliding window rate limiter."""
    def __init__(self, max_requests=10, window_seconds=60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = defaultdict(list)
        self.lock = Lock()

    def is_allowed(self, client_ip: str) -> bool:
        now = time.time()
        with self.lock:
            cutoff = now - self.window_seconds
            timestamps = [t for t in self.requests[client_ip] if t > cutoff]
            if len(timestamps) >= self.max_requests:
                self.requests[client_ip] = timestamps
                return False
            timestamps.append(now)
            self.requests[client_ip] = timestamps
            return True


rate_limiter = SimpleRateLimiter(max_requests=10, window_seconds=60)


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


def extract_video_id(url):
    """Extract YouTube video ID from various YouTube URL formats or direct 11-char IDs."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()

    # Direct 11-char base64url Video ID
    if re.match(r'^[a-zA-Z0-9_-]{11}$', url):
        return url

    try:
        parsed = urlparse(url if '://' in url else 'https://' + url)
        netloc = (parsed.netloc or '').lower()

        # Handle youtube.com domains (including m.youtube.com, www.youtube.com)
        if 'youtube.com' in netloc:
            if parsed.path == '/watch':
                qs = parse_qs(parsed.query)
                if 'v' in qs and qs['v']:
                    vid = qs['v'][0]
                    if re.match(r'^[a-zA-Z0-9_-]{11}$', vid):
                        return vid
            parts = [p for p in parsed.path.split('/') if p]
            if parts and parts[0] in ('shorts', 'embed', 'v', 'live') and len(parts) >= 2:
                vid = parts[1]
                if re.match(r'^[a-zA-Z0-9_-]{11}$', vid):
                    return vid

        # Handle youtu.be short links
        elif 'youtu.be' in netloc:
            parts = [p for p in parsed.path.split('/') if p]
            if parts:
                vid = parts[0].split('?')[0]
                if re.match(r'^[a-zA-Z0-9_-]{11}$', vid):
                    return vid
    except Exception:
        pass

    # Regex fallbacks
    patterns = [
        r'(?:https?:\/\/)?(?:www\.|m\.)?youtube\.com\/watch\?.*?\bv=([a-zA-Z0-9_-]{11})',
        r'(?:https?:\/\/)?(?:www\.)?youtu\.be\/([a-zA-Z0-9_-]{11})',
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/(?:embed|v|shorts|live)\/([a-zA-Z0-9_-]{11})',
        r'(?:v=|\/v\/|\/embed\/|\/shorts\/|\/live\/|youtu\.be\/)([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def get_youtube_metadata(url):
    """Fetch video metadata using YouTube oEmbed API and raw HTML for exact duration.
    Internally validates the URL to prevent SSRF vulnerabilities."""
    metadata = {
        'title': 'YouTube Video',
        'author': 'Unknown Creator',
        'thumbnail': None,
        'duration': None
    }

    # Strict internal assertion against SSRF: ensure it's a valid canonical YouTube URL
    video_id = extract_video_id(url)
    if not video_id:
        return metadata
    safe_canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        # 1. Get basic metadata from oEmbed with URL encoding
        encoded_url = quote_plus(safe_canonical_url)
        response = requests.get(f"https://www.youtube.com/oembed?url={encoded_url}&format=json", timeout=10)
        if response.status_code == 200:
            data = response.json()
            metadata['title'] = data.get('title', 'YouTube Video')
            metadata['author'] = data.get('author_name', 'Unknown Creator')
            metadata['thumbnail'] = data.get('thumbnail_url')
    except Exception as e:
        logging.warning("Error fetching oEmbed metadata: %s", e)

    try:
        # 2. Get exact duration from page HTML microdata
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9'
        }
        html_response = requests.get(safe_canonical_url, headers=headers, timeout=10)
        if html_response.status_code == 200:
            duration_match = re.search(r'<meta itemprop="duration" content="([^"]+)">', html_response.text)
            if duration_match:
                duration_str = duration_match.group(1)
                # Parse ISO 8601 duration (e.g. PT1H5M33S)
                parsed = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
                if parsed:
                    hours = int(parsed.group(1) or 0)
                    minutes = int(parsed.group(2) or 0)
                    seconds = int(parsed.group(3) or 0)
                    metadata['duration'] = hours * 3600 + minutes * 60 + seconds
    except Exception as e:
        logging.warning("Error fetching exact duration: %s", e)

    return metadata


def fetch_youtube_transcript(video_id):
    """Attempt to fetch transcript from YouTube directly, supporting both 0.6.x and 1.x APIs."""
    raw_data = None
    source_language = 'YouTube Subtitles'

    # Support both 1.x (instance methods) and 0.6.x (class methods)
    if hasattr(YouTubeTranscriptApi, 'get_transcript'):
        try:
            raw_data = YouTubeTranscriptApi.get_transcript(video_id, languages=['en', 'hi', 'ur'])
            source_language = 'YouTube Subtitles (en/hi/ur)'
        except Exception:
            try:
                transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
                for t in transcript_list:
                    raw_data = t.fetch()
                    source_language = getattr(t, 'language', 'YouTube Subtitles')
                    break
            except Exception as e:
                logging.debug("Legacy transcript list failed: %s", e)
    else:
        try:
            ytt = YouTubeTranscriptApi()
            try:
                raw_data = ytt.fetch(video_id, languages=['en', 'hi', 'ur'])
                source_language = 'YouTube Subtitles (en/hi/ur)'
            except Exception:
                try:
                    t_list = ytt.list(video_id)
                    for t in t_list:
                        raw_data = t.fetch()
                        source_language = getattr(t, 'language', 'YouTube Subtitles')
                        break
                except Exception as e:
                    logging.debug("Modern transcript list iteration failed: %s", e)
        except Exception as e:
            logging.debug("Modern transcript API failed: %s", e)

    if not raw_data:
        return None

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


def transcribe_audio_fallback(url, groq_key):
    """Download audio and transcribe using Groq Whisper if no subtitles are available on YouTube."""
    if not groq_key:
        raise RuntimeError(
            "This video does not have subtitles/transcripts available on YouTube. "
            "Please configure a Groq API Key in .env to enable fallback audio transcription."
        )

    # Disk space pre-check (require at least 100 MB free)
    temp_dir = tempfile.gettempdir()
    free_bytes = shutil.disk_usage(temp_dir).free
    if free_bytes < 100 * 1024 * 1024:
        raise RuntimeError("Insufficient server disk space for audio processing.")

    client = Groq(api_key=groq_key)
    unique_suffix = uuid.uuid4().hex[:8]
    output_template = os.path.join(temp_dir, f'veda_temp_audio_{unique_suffix}_%(id)s.%(ext)s')

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',
        }],
        'quiet': True,
        'noplaylist': True,
        'no_warnings': True,
        'extractor_args': {'youtube': ['client=IOS,ANDROID,WEB']}
    }

    ffmpeg_path, _ = find_ffmpeg_binaries()
    if ffmpeg_path:
        ydl_opts['ffmpeg_location'] = os.path.dirname(ffmpeg_path)

    logging.info("Downloading audio for transcription fallback...")
    audio_file = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_id = info.get('id')
            audio_file = os.path.join(temp_dir, f'veda_temp_audio_{unique_suffix}_{video_id}.mp3')

        if not audio_file or not os.path.exists(audio_file):
            raise FileNotFoundError("Audio file download failed.")

        logging.info("Transcribing via Groq Whisper...")
        with open(audio_file, "rb") as file:
            translation = client.audio.transcriptions.create(
                file=(os.path.basename(audio_file), file.read()),
                model="whisper-large-v3",
                response_format="verbose_json"
            )

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


def analyze_transcript_sarvam(transcript_text, sarvam_key, model_name="sarvam-105b"):
    """Send transcript to Sarvam AI API for structured lecture-style notes."""
    headers = {
        "api-subscription-key": sarvam_key,
        "Content-Type": "application/json"
    }

    transcript_text = transcript_text[:12000]

    prompt = f"""
    Analyze the following YouTube video lecture transcript. Explain the concepts point-by-point, acting like a teacher explaining a topic to a student.
    IMPORTANT: Your entire output MUST be strictly in the English language only, regardless of the language used in the transcript.

    Output your response in Markdown format. Use the following structure, but REPLACE the text in parentheses with the actual information from the video:

    # (Write the Main Video Topic Here)
    ## (Write a Short Overview Here)

    ### (Write the First Main Concept Here)
    - (Write a detailed bullet point explanation here)
    - (Add more bullet points as needed)

    ### (Write the Second Main Concept Here)
    - (Write explanation...)

    ### Conclusion
    - (Write a comprehensive summary and final takeaways from the lecture here)

    CRITICAL: DO NOT output the literal template text like "(Write the Main Video Topic Here)". You must replace it with the actual content.

    Transcript:
    {transcript_text}
    """

    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
        "temperature": 0.3
    }

    response = requests.post("https://api.sarvam.ai/v1/chat/completions", json=payload, headers=headers, timeout=180)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def analyze_transcript_groq(transcript_text, groq_key, model_name="llama-3.3-70b-versatile"):
    """Send transcript to Groq API as a fallback."""
    client = Groq(api_key=groq_key)
    transcript_text = transcript_text[:24000]

    prompt = f"""
    Analyze the following YouTube video lecture transcript. Explain the concepts point-by-point, acting like a teacher explaining a topic to a student.
    IMPORTANT: Your entire output MUST be strictly in the English language only, regardless of the language used in the transcript.

    Output your response in Markdown format. Use the following structure, but REPLACE the text in parentheses with the actual information from the video:

    # (Write the Main Video Topic Here)
    ## (Write a Short Overview Here)

    ### (Write the First Main Concept Here)
    - (Write a detailed bullet point explanation here)
    - (Add more bullet points as needed)

    ### (Write the Second Main Concept Here)
    - (Write explanation...)

    ### Conclusion
    - (Write a comprehensive summary and final takeaways from the lecture here)

    CRITICAL: DO NOT output the literal template text like "(Write the Main Video Topic Here)". You must replace it with the actual content.

    Transcript:
    {transcript_text}
    """

    response = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=6000,
        temperature=0.3,
    )
    return response.choices[0].message.content


def analyze_transcript_gemini(transcript_text, gemini_key):
    """Send transcript to Google Gemini API as a fallback with model resilience."""
    client = genai.Client(api_key=gemini_key)

    prompt = f"""
    Analyze the following YouTube video lecture transcript. Explain the concepts point-by-point, acting like a teacher explaining a topic to a student.
    IMPORTANT: Your entire output MUST be strictly in the English language only, regardless of the language used in the transcript.

    Output your response in Markdown format. Use the following structure, but REPLACE the text in parentheses with the actual information from the video:

    # (Write the Main Video Topic Here)
    ## (Write a Short Overview Here)

    ### (Write the First Main Concept Here)
    - (Write a detailed bullet point explanation here)
    - (Add more bullet points as needed)

    ### (Write the Second Main Concept Here)
    - (Write explanation...)

    ### Conclusion
    - (Write a comprehensive summary and final takeaways from the lecture here)

    CRITICAL: DO NOT output the literal template text like "(Write the Main Video Topic Here)". You must replace it with the actual content.

    Transcript:
    {transcript_text}
    """

    candidate_models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    last_err = None
    for model_name in candidate_models:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            if response and response.text:
                return response.text
        except Exception as e:
            last_err = e
            logging.warning("Gemini model %s failed: %s. Trying next model...", model_name, e)
            continue
    raise last_err or RuntimeError("All Gemini models failed to generate content.")


def analyze_transcript_nvidia(transcript_text, nvidia_key, model_name="nvidia/nemotron-3-ultra-550b-a55b"):
    """Send transcript to NVIDIA NIM API using OpenAI client with streaming support."""
    client = OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=nvidia_key
    )

    transcript_text = transcript_text[:24000]

    prompt = f"""
    Analyze the following YouTube video lecture transcript. Explain the concepts point-by-point, acting like a teacher explaining a topic to a student.
    IMPORTANT: Your entire output MUST be strictly in the English language only, regardless of the language used in the transcript.

    Output your response in Markdown format. Use the following structure, but REPLACE the text in parentheses with the actual information from the video:

    # (Write the Main Video Topic Here)
    ## (Write a Short Overview Here)

    ### (Write the First Main Concept Here)
    - (Write a detailed bullet point explanation here)
    - (Add more bullet points as needed)

    ### (Write the Second Main Concept Here)
    - (Write explanation...)

    ### Conclusion
    - (Write a comprehensive summary and final takeaways from the lecture here)

    CRITICAL: DO NOT output the literal template text like "(Write the Main Video Topic Here)". You must replace it with the actual content.

    Transcript:
    {transcript_text}
    """

    candidate_models = [
        model_name,
        "meta/llama-3.3-70b-instruct",
        "nvidia/llama-3.1-nemotron-70b-instruct",
        "mistralai/mistral-large-2-instruct"
    ]
    models_to_try = []
    for m in candidate_models:
        if m and m not in models_to_try:
            models_to_try.append(m)

    last_err = None
    for model in models_to_try:
        try:
            extra_body = {}
            if "nemotron" in model:
                extra_body = {"chat_template_kwargs": {"enable_thinking": True}}

            completion = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=6000,
                extra_body=extra_body if extra_body else None,
                stream=True
            )

            collected_content = []
            for chunk in completion:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if getattr(delta, "content", None) is not None:
                    collected_content.append(delta.content)

            output_text = "".join(collected_content).strip()
            if output_text:
                return output_text
        except Exception as e:
            last_err = e
            logging.warning("NVIDIA model %s failed: %s. Trying next candidate model...", model, e)
            continue

    raise last_err or RuntimeError("All NVIDIA models failed to generate content.")


# Flask Routes
@app.route('/')
@app.route('/home')
@app.route('/home/<lecture_id>')
def home(lecture_id=None):
    return render_template('index.html')


@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/api/analyze', methods=['POST'])
def analyze():
    # Rate limiting: maximum 10 requests per minute per IP
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if client_ip:
        client_ip = client_ip.split(',')[0].strip()
    if not rate_limiter.is_allowed(client_ip or 'unknown'):
        return jsonify({'error': 'Rate limit exceeded. Please wait a moment before analyzing another lecture.'}), 429

    data = request.json or {}
    url = data.get('url')
    if not url or not isinstance(url, str):
        return jsonify({'error': 'A valid YouTube URL is required.'}), 400

    url = url.strip()
    if len(url) > 500:
        return jsonify({'error': 'URL length exceeds maximum permitted limit.'}), 400

    model_name = 'sarvam-105b'

    user_sarvam_key = os.environ.get('SARVAM_API_KEY', '').strip()
    user_groq_key = os.environ.get('GROQ_API_KEY', '').strip()
    user_nvidia_key = os.environ.get('NVIDIA_API_KEY', '').strip()
    user_gemini_key = os.environ.get('GEMINI_API_KEY', '').strip()

    video_id = extract_video_id(url)

    # Strict validation: YouTube IDs are exactly 11 characters (base64url)
    if not video_id or not re.match(r'^[a-zA-Z0-9_-]{11}$', video_id):
        return jsonify({'error': 'Invalid YouTube URL or Video ID. Please provide a legitimate link.'}), 400

    # Sanitize and strictly reconstruct the URL to prevent SSRF attacks
    safe_canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        # Step 1: Fetch Metadata using the strictly sanitized URL
        metadata = get_youtube_metadata(safe_canonical_url)
        metadata['video_id'] = video_id
        metadata['url'] = safe_canonical_url

        # Strict Duration Limit Check (20m 20s = 1220s)
        video_duration = metadata.get('duration')
        if video_duration is not None and float(video_duration) > 1220:
            return jsonify({
                'error': 'Video is too long! We currently cannot generate notes for videos exceeding 20 minutes and 20 seconds.'
            }), 400

        # Step 2: Fetch or Transcribe Audio
        transcript_data = fetch_youtube_transcript(video_id)
        if not transcript_data:
            logging.info("YouTube Transcript API failed. Attempting audio download and Groq Whisper fallback...")
            try:
                transcript_data = transcribe_audio_fallback(safe_canonical_url, user_groq_key)
            except Exception as whisper_err:
                logging.error("Groq Whisper transcription fallback failed: %s", whisper_err, exc_info=True)
                return jsonify({
                    'error': 'This video does not have subtitles available and audio transcription failed. Please verify your Groq API key or try another video.'
                }), 400

        # Calculate duration from transcript if missing from metadata
        if (metadata.get('duration') is None or metadata.get('duration') == 0) and transcript_data and transcript_data.get('segments'):
            try:
                last_segment = transcript_data['segments'][-1]
                metadata['duration'] = float(last_segment.get('start', 0)) + float(last_segment.get('duration', 0))
            except Exception:
                pass

        # Strict duration limit enforcement:
        # If duration is still unknown or over 1220 seconds (20m 20s), abort processing
        video_duration = metadata.get('duration')
        if video_duration is None:
            return jsonify({
                'error': 'Could not safely determine video duration. Processing aborted to protect server limits.'
            }), 400

        if float(video_duration) > 1220:
            return jsonify({
                'error': 'Video is too long! We currently cannot generate notes for videos exceeding 20 minutes and 20 seconds.'
            }), 400

        # Step 3: Run AI Analysis
        analysis_result = None
        analysis_engine = None
        error_messages = []

        if not any([user_sarvam_key, user_groq_key, user_nvidia_key, user_gemini_key]):
            return jsonify({
                'error': 'No AI API keys configured. Please add SARVAM_API_KEY, GROQ_API_KEY, NVIDIA_API_KEY, or GEMINI_API_KEY to your .env file.'
            }), 400

        # 1. Try NVIDIA API (Primary)
        if user_nvidia_key:
            logging.info("Running NVIDIA NIM analysis (Primary)...")
            try:
                analysis_result = analyze_transcript_nvidia(transcript_data['text'], user_nvidia_key)
                analysis_engine = "NVIDIA NIM (Nemotron)"
            except Exception as e:
                logging.warning("NVIDIA analysis failed: %s", e)
                error_messages.append(f"NVIDIA: {str(e)}")
        else:
            error_messages.append("NVIDIA Key missing.")

        # 2. Try Gemini API (Fallback 1)
        if not analysis_result and user_gemini_key:
            logging.info("Running Gemini analysis fallback...")
            try:
                analysis_result = analyze_transcript_gemini(transcript_data['text'], user_gemini_key)
                analysis_engine = "Gemini Flash (Fallback)"
            except Exception as e:
                logging.warning("Gemini analysis failed: %s", e)
                error_messages.append(f"Gemini: {str(e)}")
        elif not analysis_result:
            error_messages.append("Gemini Key missing.")

        # 3. Try Groq API (Fallback 2)
        if not analysis_result and user_groq_key:
            logging.info("Running Groq analysis fallback...")
            try:
                analysis_result = analyze_transcript_groq(transcript_data['text'], user_groq_key)
                analysis_engine = "Groq LLaMA-3 (Fallback)"
            except Exception as e:
                logging.warning("Groq analysis failed: %s", e)
                error_messages.append(f"Groq: {str(e)}")
        elif not analysis_result:
            error_messages.append("Groq Key missing.")

        # 4. Try Sarvam API (Fallback 3)
        if not analysis_result and user_sarvam_key:
            logging.info("Running Sarvam AI analysis fallback (%s)...", model_name)
            try:
                analysis_result = analyze_transcript_sarvam(transcript_data['text'], user_sarvam_key, model_name)
                analysis_engine = f"Sarvam AI ({model_name})"
            except Exception as e:
                logging.warning("Sarvam AI analysis failed: %s", e)
                error_messages.append(f"Sarvam: {str(e)}")
        elif not analysis_result:
            error_messages.append("Sarvam Key missing.")

        if not analysis_result:
            logging.error("All AI analysis providers failed: %s", " | ".join(error_messages))
            return jsonify({
                'error': 'All configured AI analysis providers failed to respond. Please check your network and API keys in .env.'
            }), 502

        return jsonify({
            'metadata': metadata,
            'transcript': transcript_data,
            'analysis': analysis_result,
            'engine': analysis_engine
        })

    except Exception as e:
        logging.error("Analysis route error: %s", e, exc_info=True)
        return jsonify({'error': 'An unexpected error occurred while analyzing the lecture. Please try again.'}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() in ('true', '1', 'yes')
    app.run(debug=debug_mode, host='127.0.0.1', port=port)
