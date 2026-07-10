import os
import re
import shutil
import tempfile
import requests
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai
from groq import Groq

# Load environment variables
load_dotenv()

app = Flask(__name__)

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
    """Extract YouTube video ID from various YouTube URL formats."""
    if not url:
        return None
    patterns = [
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/watch\?v=([^&\s?#]+)',
        r'(?:https?:\/\/)?(?:www\.)?youtu\.be\/([^&\s?#]+)',
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/embed\/([^&\s?#]+)',
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/v\/([^&\s?#]+)',
        r'(?:https?:\/\/)?(?:www\.)?youtube\.com\/shorts\/([^&\s?#]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    # Basic fallback split
    if "youtu.be/" in url:
        return url.split("youtu.be/")[-1].split("?")[0].split("/")[0]
    if "/shorts/" in url:
        return url.split("/shorts/")[-1].split("?")[0].split("/")[0]
    return None

def get_youtube_metadata(url):
    """Fetch video metadata using YouTube oEmbed API and raw HTML for exact duration."""
    metadata = {
        'title': 'YouTube Video',
        'author': 'Unknown Creator',
        'thumbnail': None,
        'duration': None
    }
    
    try:
        # 1. Get basic metadata from oEmbed
        response = requests.get(f"https://www.youtube.com/oembed?url={url}&format=json", timeout=10)
        if response.status_code == 200:
            data = response.json()
            metadata['title'] = data.get('title', 'YouTube Video')
            metadata['author'] = data.get('author_name', 'Unknown Creator')
            metadata['thumbnail'] = data.get('thumbnail_url')
    except Exception as e:
        print(f"Error fetching oEmbed metadata: {e}")
        
    try:
        # 2. Get exact duration from page HTML microdata
        html_response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
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
        print(f"Error fetching exact duration: {e}")
        
    return metadata

def fetch_youtube_transcript(video_id):
    """Attempt to fetch transcript from YouTube directly."""
    try:
        data = YouTubeTranscriptApi.get_transcript(video_id, languages=['en', 'hi', 'ur'])
        full_text = " ".join([item['text'] for item in data])
        return {
            'text': full_text,
            'segments': data,
            'language': 'Unknown',
            'language_code': 'Unknown',
            'source': 'YouTube Subtitles'
        }
    except Exception as e:
        print(f"YouTube transcript API failed: {e}")
        return None

def transcribe_audio_fallback(url, groq_key):
    """Download audio and transcribe using Groq Whisper if no subtitles are available on YouTube."""
    if not groq_key:
        raise RuntimeError(
            "This video does not have subtitles/transcripts available on YouTube. "
            "Please configure a Groq API Key in settings to enable fallback audio transcription."
        )

    client = Groq(api_key=groq_key)
    temp_dir = tempfile.gettempdir()
    output_template = os.path.join(temp_dir, 'veda_temp_audio_%(id)s.%(ext)s')

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

    ffmpeg_path, ffprobe_path = find_ffmpeg_binaries()
    if ffmpeg_path:
        ydl_opts['ffmpeg_location'] = os.path.dirname(ffmpeg_path)
    if ffprobe_path:
        ydl_opts['ffprobe_location'] = os.path.dirname(ffprobe_path)

    print("Downloading audio for transcription fallback...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        video_id = info.get('id')
        audio_file = os.path.join(temp_dir, f'veda_temp_audio_{video_id}.mp3')

    try:
        if not os.path.exists(audio_file):
            raise FileNotFoundError("Audio file download failed.")

        print("Transcribing via Groq Whisper...")
        with open(audio_file, "rb") as file:
            translation = client.audio.transcriptions.create(
                file=(audio_file, file.read()),
                model="whisper-large-v3",
                response_format="verbose_json"
            )

        # Parse verbose response for segments. SDKs may return dicts, Pydantic
        # objects, or plain objects depending on the package version.
        def get_field(item, name, default=None):
            if isinstance(item, dict):
                return item.get(name, default)
            return getattr(item, name, default)

        segments = []
        full_text = ""
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
        # Clean up temp file
        if os.path.exists(audio_file):
            try:
                os.remove(audio_file)
            except Exception:
                pass


def analyze_transcript_sarvam(transcript_text, sarvam_key, model_name="sarvam-105b"):
    """Send transcript to Sarvam AI API for structured lecture-style notes."""
    headers = {
        "api-subscription-key": sarvam_key,
        "Content-Type": "application/json"
    }
    
    # Safe truncation to prevent crashing
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
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "max_tokens": 4096,
        "temperature": 0.3
    }
    
    response = requests.post("https://api.sarvam.ai/v1/chat/completions", json=payload, headers=headers, timeout=180)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]

def analyze_transcript_groq(transcript_text, groq_key, model_name="llama-3.3-70b-versatile"):
    """Send transcript to Groq API as a fallback."""
    client = Groq(api_key=groq_key)
    
    # Safe truncation to prevent crashing
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
    
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text


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
    data = request.json or {}
    url = data.get('url')
    model_name = 'sarvam-105b'
    
    # Use keys directly from environment
    user_sarvam_key = os.environ.get('SARVAM_API_KEY', '').strip()
    user_gemini_key = os.environ.get('GEMINI_API_KEY', '').strip()
    user_groq_key = os.environ.get('GROQ_API_KEY', '').strip()

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

        # Strict Duration Limit Check (Extremely Fast, Pre-Transcript)
        video_duration = metadata.get('duration', 0)
        if video_duration and float(video_duration) > 1220:
            return jsonify({
                'error': 'Video is too long! We currently cannot generate notes for videos exceeding 20 minutes and 20 seconds.'
            }), 400

        # Step 2: Fetch or Transcribe Audio
        transcript_data = fetch_youtube_transcript(video_id)
        if not transcript_data:
            print("YouTube Transcript API failed. Attempting audio download and Groq Whisper transcription fallback...")
            try:
                # Use safe_canonical_url for yt-dlp to prevent injection
                transcript_data = transcribe_audio_fallback(safe_canonical_url, user_groq_key)
            except Exception as whisper_err:
                print(f"Groq Whisper transcription fallback failed: {whisper_err}")
                return jsonify({
                    'error': f"Failed to get transcript. {str(whisper_err)}"
                }), 400

        # Calculate duration from transcript if missing (due to oEmbed limitations)
        if not metadata.get('duration') and transcript_data and transcript_data.get('segments'):
            try:
                last_segment = transcript_data['segments'][-1]
                metadata['duration'] = float(last_segment.get('start', 0)) + float(last_segment.get('duration', 0))
                
                # Second fallback check just in case metadata failed to get duration but transcript has it
                if float(metadata['duration']) > 1220:
                    return jsonify({
                        'error': 'Video is too long! We currently cannot generate notes for videos exceeding 20 minutes and 20 seconds.'
                    }), 400
            except Exception:
                pass

        # Step 3: Run AI Analysis
        analysis_result = None
        analysis_engine = None
        error_messages = []

        # 1. Try Sarvam API
        if user_sarvam_key:
            print(f"Running Sarvam AI analysis ({model_name})...")
            try:
                analysis_result = analyze_transcript_sarvam(transcript_data['text'], user_sarvam_key, model_name)
                analysis_engine = f"Sarvam AI ({model_name})"
            except Exception as e:
                print(f"Sarvam AI analysis failed: {e}")
                error_messages.append(f"Sarvam: {str(e)}")
        else:
            error_messages.append("Sarvam Key missing.")

        # 2. Try Groq API (Fallback 1)
        if not analysis_result and user_groq_key:
            print("Running Groq analysis fallback...")
            try:
                analysis_result = analyze_transcript_groq(transcript_data['text'], user_groq_key)
                analysis_engine = "Groq LLaMA-3 (Fallback)"
            except Exception as e:
                print(f"Groq analysis failed: {e}")
                error_messages.append(f"Groq: {str(e)}")
        elif not analysis_result:
            error_messages.append("Groq Key missing.")

        # 3. Try Gemini API (Fallback 2)
        if not analysis_result and user_gemini_key:
            print("Running Gemini analysis fallback...")
            try:
                analysis_result = analyze_transcript_gemini(transcript_data['text'], user_gemini_key)
                analysis_engine = "Gemini (Fallback)"
            except Exception as e:
                print(f"Gemini analysis failed: {e}")
                error_messages.append(f"Gemini: {str(e)}")
        elif not analysis_result:
            error_messages.append("Gemini Key missing.")


        if not analysis_result:
            return jsonify({
                'error': f"All analysis engines failed. Details: {' | '.join(error_messages)}"
            }), 400

        return jsonify({
            'metadata': metadata,
            'transcript': transcript_data,
            'analysis': analysis_result,
            'engine': analysis_engine
        })

    except Exception as e:
        print(f"Analysis error: {e}")
        return jsonify({'error': f"An unexpected error occurred during processing: {str(e)}"}), 500

if __name__ == '__main__':
    # Default port is 5000, let's run locally
    app.run(debug=True, host='127.0.0.1', port=5000)
