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
    """Fetch video metadata using yt-dlp without downloading the video."""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'extract_flat': True,
        'extractor_args': {'youtube': ['client=ANDROID']}
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            return {
                'title': info.get('title', 'YouTube Video'),
                'author': info.get('uploader') or info.get('channel') or 'Unknown Creator',
                'thumbnail': info.get('thumbnail'),
                'duration': info.get('duration'),  # in seconds
            }
        except Exception as e:
            print(f"Error fetching metadata: {e}")
            return {
                'title': 'YouTube Video',
                'author': 'Unknown Creator',
                'thumbnail': None,
                'duration': None
            }

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
        'extractor_args': {'youtube': ['client=ANDROID']}
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

        # Parse translation verbose response for segments
        segments = []
        full_text = ""
        if hasattr(translation, 'segments'):
            for seg in translation.segments:
                segments.append({
                    'text': seg.get('text', ''),
                    'start': seg.get('start', 0.0),
                    'duration': seg.get('end', 0.0) - seg.get('start', 0.0)
                })
            full_text = translation.text
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
    
    prompt = f"""
    Analyze the following YouTube video lecture transcript. Explain the concepts point-by-point, acting like a teacher explaining a topic to a student.
    IMPORTANT: Your entire output MUST be strictly in the English language only, regardless of the language used in the transcript.
    
    Output your response using the following structured format:
    # [Main Video Title / Subject]
    ## [Short Subtitle / Overview]
    
    ### [Heading: Main Concept 1]
    #### [Subheading: Detail/Subtopic 1a]
    - [Body: Point-by-point explanation, formulas, key arguments, or examples]
    - [Body: Continuing detail...]
    
    #### [Subheading: Detail/Subtopic 1b]
    - [Body: Point-by-point explanation...]
    
    ### [Heading: Main Concept 2]
    ...
    
    Be clear, educational, and structured. Use Markdown formatting.
    
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
    
    prompt = f"""
    Analyze the following YouTube video lecture transcript. Explain the concepts point-by-point, acting like a teacher explaining a topic to a student.
    IMPORTANT: Your entire output MUST be strictly in the English language only, regardless of the language used in the transcript.
    
    Output your response using the following structured format:
    # [Main Video Title / Subject]
    ## [Short Subtitle / Overview]
    
    ### [Heading: Main Concept 1]
    #### [Subheading: Detail/Subtopic 1a]
    - [Body: Point-by-point explanation, formulas, key arguments, or examples]
    - [Body: Continuing detail...]
    
    #### [Subheading: Detail/Subtopic 1b]
    - [Body: Point-by-point explanation...]
    
    ### [Heading: Main Concept 2]
    ...
    
    Be clear, educational, and structured. Use Markdown formatting.
    
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
    """Send transcript to Gemini API as a fallback."""
    client = genai.Client(api_key=gemini_key)
    
    prompt = f"""
    Analyze the following YouTube video lecture transcript. Explain the concepts point-by-point, acting like a teacher explaining a topic to a student.
    IMPORTANT: Your entire output MUST be strictly in the English language only, regardless of the language used in the transcript.
    
    Output your response using the following structured format:
    # [Main Video Title / Subject]
    ## [Short Subtitle / Overview]
    
    ### [Heading: Main Concept 1]
    #### [Subheading: Detail/Subtopic 1a]
    - [Body: Point-by-point explanation, formulas, key arguments, or examples]
    - [Body: Continuing detail...]
    
    #### [Subheading: Detail/Subtopic 1b]
    - [Body: Point-by-point explanation...]
    
    ### [Heading: Main Concept 2]
    ...
    
    Be clear, educational, and structured. Use Markdown formatting.
    
    Transcript:
    {transcript_text}
    """
    
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            max_output_tokens=6000,
            temperature=0.3,
        ),
    )
    return response.text


# Flask Routes
@app.route('/')
@app.route('/home')
def home():
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
    if not video_id:
        return jsonify({'error': 'Invalid YouTube URL. Please make sure it is a valid link.'}), 400

    try:
        # Step 1: Fetch Metadata
        metadata = get_youtube_metadata(url)
        metadata['video_id'] = video_id
        metadata['url'] = url

        # Step 2: Fetch or Transcribe Audio
        transcript_data = fetch_youtube_transcript(video_id)
        if not transcript_data:
            print("YouTube Transcript API failed. Attempting audio download and Groq Whisper transcription fallback...")
            try:
                transcript_data = transcribe_audio_fallback(url, user_groq_key)
            except Exception as whisper_err:
                return jsonify({
                    'error': f"Failed to get transcript. {str(whisper_err)}"
                }), 400

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
