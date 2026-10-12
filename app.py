import os
import re
import time
import logging
from collections import defaultdict
from threading import Lock
from urllib.parse import urlparse, parse_qs, quote_plus
import requests
from flask import Flask, render_template, request, jsonify, send_file
from dotenv import load_dotenv

# Modular service imports
from redis_cache import get_cached_transcript, set_cached_transcript
from transcripts import get_transcript_with_fallback
from llm import generate_video_intelligence, chat_with_transcript
from pdf_generator import generate_pdf_report

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
)

# Load environment variables once at startup
load_dotenv()

# Base directory for reliable static and template resolution on Vercel serverless
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(
    __name__,
    static_folder=os.path.join(BASE_DIR, 'static'),
    template_folder=os.path.join(BASE_DIR, 'templates')
)


@app.route('/static/<path:filename>')
def serve_static(filename):
    from flask import send_from_directory
    return send_from_directory(os.path.join(BASE_DIR, 'static'), filename)


@app.route('/favicon.ico')
def favicon():
    from flask import send_from_directory
    return send_from_directory(os.path.join(BASE_DIR, 'static'), 'favicon.ico', mimetype='image/vnd.microsoft.icon')


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
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
        "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
        "img-src 'self' data: blob: https://i.ytimg.com https://*.ytimg.com https://images.unsplash.com; "
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

    video_id = extract_video_id(url)
    if not video_id:
        return metadata
    safe_canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        # 1. Get basic metadata from oEmbed
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
                parsed = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
                if parsed:
                    hours = int(parsed.group(1) or 0)
                    minutes = int(parsed.group(2) or 0)
                    seconds = int(parsed.group(3) or 0)
                    metadata['duration'] = hours * 3600 + minutes * 60 + seconds
    except Exception as e:
        logging.warning("Error fetching exact duration: %s", e)

    # 3. Supadata universal metadata fallback (if duration or title is missing)
    supadata_key = os.environ.get('SUPADATA_API', '').strip()
    if supadata_key and (metadata['duration'] is None or metadata['title'] == 'YouTube Video'):
        try:
            logging.info("Attempting Supadata universal metadata fallback for %s...", video_id)
            meta_resp = requests.get(
                f"https://api.supadata.ai/v1/metadata?url={quote_plus(safe_canonical_url)}",
                headers={'x-api-key': supadata_key},
                timeout=12
            )
            if meta_resp.status_code == 200:
                s_data = meta_resp.json()
                if isinstance(s_data, dict):
                    if s_data.get('title') and metadata['title'] == 'YouTube Video':
                        metadata['title'] = s_data['title']
                    if s_data.get('author') and metadata['author'] == 'Unknown Creator':
                        metadata['author'] = s_data['author']
                    if s_data.get('duration') and metadata['duration'] is None:
                        try:
                            metadata['duration'] = float(s_data['duration'])
                        except (ValueError, TypeError):
                            pass
        except Exception as e:
            logging.warning("Supadata metadata fallback error: %s", e)

    return metadata


# Flask Page Routes
@app.route('/')
@app.route('/home')
@app.route('/home/<lecture_id>')
def home(lecture_id=None):
    return render_template('index.html')


@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/api/generate-pdf', methods=['POST'])
def generate_pdf():
    """Server-side PDF generation using Python ReportLab.
    Eliminates HTML-to-canvas rendering bugs, broken tables, and awkward page splits."""
    data = request.json or {}
    markdown_text = data.get('markdown') or ''
    metadata = data.get('metadata') or {}
    engine_name = data.get('engine') or 'Angkor Executive AI'

    if not markdown_text.strip():
        return jsonify({'error': 'Markdown content is required to generate PDF.'}), 400

    try:
        import io
        pdf_bytes = generate_pdf_report(metadata, markdown_text, engine_name)
        title = metadata.get('title') or 'report'
        clean_title = re.sub(r'[^a-zA-Z0-9_-]', '_', title)[:32].lower()
        filename = f"angkor_report_{clean_title}.pdf"

        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype='application/pdf',
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        logging.error("Server-side PDF generation error: %s", e, exc_info=True)
        return jsonify({'error': f'Failed to generate PDF: {str(e)}'}), 500


@app.route('/api/chat', methods=['POST'])
def chat():
    """Interactive conversational endpoint grounded in video transcript.
    Stateless and user-isolated: uses client session memory to prevent memory bleed between users."""
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if client_ip:
        client_ip = client_ip.split(',')[0].strip()
    if not rate_limiter.is_allowed(client_ip or 'unknown'):
        return jsonify({'error': 'Rate limit exceeded. Please wait a moment.'}), 429

    data = request.json or {}
    video_id = data.get('video_id', '').strip()
    message = data.get('message', '').strip()
    history = data.get('history', [])

    if not video_id:
        return jsonify({'error': 'video_id is required.'}), 400
    if not message:
        return jsonify({'error': 'Message content cannot be empty.'}), 400

    # 1. Fetch transcript from persistent cache (0 external API cost)
    cached_transcript = get_cached_transcript(video_id)
    transcript_text = cached_transcript.get('text', '') if cached_transcript else ''

    if not transcript_text:
        # Fallback to direct client-provided transcript if not in Redis
        transcript_text = data.get('transcript_text', '').strip()

    if not transcript_text:
        return jsonify({'error': 'Transcript context not found for this video. Please analyze the video first.'}), 404

    keys_dict = {
        'nvidia': os.environ.get('NVIDIA_API_KEY', '').strip(),
        'gemini': os.environ.get('GEMINI_API_KEY', '').strip(),
        'groq': os.environ.get('GROQ_API_KEY', '').strip()
    }

    try:
        reply = chat_with_transcript(transcript_text, message, history, keys_dict)
        return jsonify({'reply': reply})
    except Exception as e:
        logging.error("Chat route error: %s", e, exc_info=True)
        return jsonify({'error': f'Failed to generate response: {str(e)}'}), 500


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

    video_id = extract_video_id(url)
    if not video_id or not re.match(r'^[a-zA-Z0-9_-]{11}$', video_id):
        return jsonify({'error': 'Invalid YouTube URL or Video ID. Please provide a legitimate link.'}), 400

    safe_canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    user_sarvam_key = os.environ.get('SARVAM_API_KEY', '').strip()
    user_groq_key = os.environ.get('GROQ_API_KEY', '').strip()
    user_nvidia_key = os.environ.get('NVIDIA_API_KEY', '').strip()
    user_gemini_key = os.environ.get('GEMINI_API_KEY', '').strip()
    user_supadata_key = os.environ.get('SUPADATA_API', '').strip()
    sarvam_model = os.environ.get('SARVAM_MODEL', 'sarvam-105b-conversations')

    try:
        # Step 1: Fetch Video Metadata
        metadata = get_youtube_metadata(safe_canonical_url)
        metadata['video_id'] = video_id
        metadata['url'] = safe_canonical_url

        # Check Duration Limit Early if duration is already known
        video_duration = metadata.get('duration')
        if video_duration is not None and float(video_duration) > 1220:
            return jsonify({
                'error': 'Video is too long! We currently cannot generate notes for videos exceeding 20 minutes and 20 seconds.'
            }), 400

        # Step 2: Fetch Transcript with Redis Cache & Multi-tier Fallback
        # Checks Redis cache -> YouTube direct subtitles (Free) -> Supadata -> Groq Whisper
        try:
            transcript_data = get_transcript_with_fallback(
                video_id=video_id,
                canonical_url=safe_canonical_url,
                supadata_key=user_supadata_key,
                groq_key=user_groq_key
            )
        except Exception as transcript_err:
            logging.error("Transcript retrieval failed: %s", transcript_err)
            return jsonify({
                'error': f'Failed to retrieve video transcript: {str(transcript_err)}'
            }), 400

        if not transcript_data or not transcript_data.get('text'):
            return jsonify({
                'error': 'Could not extract transcript for this video. Please check if the video has speech or captions.'
            }), 400

        # Calculate duration from transcript if missing from metadata
        if (metadata.get('duration') is None or metadata.get('duration') == 0) and transcript_data.get('segments'):
            try:
                last_segment = transcript_data['segments'][-1]
                metadata['duration'] = float(last_segment.get('start', 0)) + float(last_segment.get('duration', 0))
            except Exception:
                pass

        # Strict duration limit enforcement (20m 20s = 1220s)
        video_duration = metadata.get('duration')
        if video_duration is None:
            return jsonify({
                'error': 'Could not safely determine video duration. Processing aborted to protect server limits.'
            }), 400

        if float(video_duration) > 1220:
            return jsonify({
                'error': 'Video is too long! We currently cannot generate notes for videos exceeding 20 minutes and 20 seconds.'
            }), 400

        # Step 3: Run AI Intelligence Analysis (NVIDIA -> Gemini -> Groq -> Sarvam)
        keys_dict = {
            'nvidia': user_nvidia_key,
            'gemini': user_gemini_key,
            'groq': user_groq_key,
            'sarvam': user_sarvam_key
        }

        analysis_result, analysis_engine = generate_video_intelligence(
            transcript_text=transcript_data['text'],
            keys=keys_dict,
            sarvam_model=sarvam_model
        )

        return jsonify({
            'metadata': metadata,
            'transcript': transcript_data,
            'analysis': analysis_result,
            'engine': analysis_engine
        })

    except Exception as e:
        logging.error("Analysis route error: %s", e, exc_info=True)
        return jsonify({'error': f'An unexpected error occurred while analyzing the lecture: {str(e)}'}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() in ('true', '1', 'yes')
    app.run(debug=debug_mode, host='127.0.0.1', port=port)
