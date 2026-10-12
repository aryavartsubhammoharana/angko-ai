import os
import logging
import requests
from google import genai
from openai import OpenAI

ANALYSIS_SYSTEM_PROMPT = """
You are an Executive Video Intelligence Analyst. Analyze the following YouTube video transcript and break down the ground reality of what is being discussed.

CRITICAL INSTRUCTIONS:
1. LANGUAGE: Your entire output MUST be strictly in clear, professional ENGLISH only, regardless of whatever language or mix of languages (e.g. Hindi, Hinglish, regional) was spoken in the video.
2. CORE MISSION: Deliver a sharp, high-signal breakdown of the actual subject matter: tell the user exactly what is happening, what is NOT happening (misconceptions/nuances/refutations), and the real crux without any filler or fluff.
3. MATHEMATICS & FORMULAS: If formulas, equations, code, or technical metrics appear, format them using standard LaTeX notation ($...$ for inline, $$...$$ for display).
4. REPLACING PLACEHOLDERS: Replace all section titles and bullet placeholders with the actual substantive analysis from the video. Do not output generic placeholders.

Output your response in clean Markdown with the following exact structure:

# (Concise, High-Impact Title of the Video's True Subject)

## ⚡ Quick Crux (30-Second TL;DR)
- (2-3 crisp bullet points explaining the core essence and main takeaway of the video)

## 🎯 What's Happening (The Core Breakdown & Reality)
- (Detail the main arguments, events, claims, and ground reality presented)
- (Include specific facts, examples, frameworks, or data points discussed)
- (Explain the key mechanisms or insights revealed in the video)

## ❌ What's Not Happening (Misconceptions, Nuances & Warnings)
- (Clarify what is NOT true, what the speaker refuted, or where people commonly get misled)
- (Highlight limitations, counter-arguments, caveats, or missing context)

## 💡 The Bottom Line & Final Verdict
- (Direct, actionable summary: what does this actually mean for the viewer?)

Transcript:
{transcript_text}
"""


def analyze_transcript_nvidia(transcript_text: str, nvidia_key: str, model_name: str = "nvidia/nemotron-3-ultra-550b-a55b"):
    """Send transcript to NVIDIA NIM API using OpenAI-compatible client with streaming support."""
    client = OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=nvidia_key
    )

    prompt = ANALYSIS_SYSTEM_PROMPT.format(transcript_text=transcript_text[:24000])

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


def analyze_transcript_gemini(transcript_text: str, gemini_key: str):
    """Send transcript to Google Gemini API as a fallback with model resilience."""
    client = genai.Client(api_key=gemini_key)

    prompt = ANALYSIS_SYSTEM_PROMPT.format(transcript_text=transcript_text[:24000])

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


def analyze_transcript_groq(transcript_text: str, groq_key: str, model_name: str = "openai/gpt-oss-120b"):
    """Send transcript to Groq API via direct HTTP as a fallback."""
    prompt = ANALYSIS_SYSTEM_PROMPT.format(transcript_text=transcript_text[:24000])

    headers = {
        "Authorization": f"Bearer {groq_key}",
        "Content-Type": "application/json"
    }
    candidate_models = [model_name, "openai/gpt-oss-120b", "llama-3.3-70b-versatile", "qwen/qwen3.8-27b"]
    last_err = None
    for m in candidate_models:
        payload = {
            "model": m,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4096,
            "temperature": 0.3
        }
        try:
            resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=90)
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            last_err = RuntimeError(f"Groq {m} returned {resp.status_code}: {resp.text}")
        except Exception as e:
            last_err = e
            continue
    raise last_err or RuntimeError("All Groq models failed.")


def analyze_transcript_sarvam(transcript_text: str, sarvam_key: str, model_name: str = "sarvam-105b-conversations"):
    """Send transcript to Sarvam AI API for structured lecture-style notes."""
    headers = {
        "api-subscription-key": sarvam_key,
        "Content-Type": "application/json"
    }

    prompt = ANALYSIS_SYSTEM_PROMPT.format(transcript_text=transcript_text[:12000])

    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
        "temperature": 0.3
    }

    response = requests.post("https://api.sarvam.ai/v1/chat/completions", json=payload, headers=headers, timeout=180)
    response.raise_for_status()
    choice_msg = response.json()["choices"][0]["message"]
    return choice_msg.get("content") or choice_msg.get("reasoning_content") or ""


def generate_video_intelligence(transcript_text: str, keys: dict, sarvam_model: str = "sarvam-105b-conversations"):
    """Unified AI intelligence generator executing resilient fallback chain across providers:
    NVIDIA -> Gemini -> Groq -> Sarvam
    (or Sarvam first if explicitly preferred).
    Returns tuple: (analysis_markdown, engine_name)
    """
    user_sarvam_key = keys.get('sarvam', '').strip()
    user_nvidia_key = keys.get('nvidia', '').strip()
    user_gemini_key = keys.get('gemini', '').strip()
    user_groq_key = keys.get('groq', '').strip()

    error_messages = []

    # Priority 1: NVIDIA NIM (Ultra fast, high reasoning Nemotron / Llama-3.3)
    if user_nvidia_key:
        try:
            logging.info("Running NVIDIA NIM video intelligence...")
            result = analyze_transcript_nvidia(transcript_text, user_nvidia_key)
            if result:
                return result, "NVIDIA NIM (Nemotron)"
        except Exception as e:
            logging.warning("NVIDIA NIM analysis failed: %s", e)
            error_messages.append(f"NVIDIA: {str(e)}")

    # Priority 2: Gemini 2.5 Flash / 2.0 Flash
    if user_gemini_key:
        try:
            logging.info("Running Google Gemini video intelligence fallback...")
            result = analyze_transcript_gemini(transcript_text, user_gemini_key)
            if result:
                return result, "Google Gemini Flash"
        except Exception as e:
            logging.warning("Gemini analysis failed: %s", e)
            error_messages.append(f"Gemini: {str(e)}")

    # Priority 3: Groq LLaMA / OSS models
    if user_groq_key:
        try:
            logging.info("Running Groq video intelligence fallback...")
            result = analyze_transcript_groq(transcript_text, user_groq_key)
            if result:
                return result, "Groq LLaMA-3"
        except Exception as e:
            logging.warning("Groq analysis failed: %s", e)
            error_messages.append(f"Groq: {str(e)}")

    # Priority 4: Sarvam AI
    if user_sarvam_key:
        try:
            logging.info("Running Sarvam AI video intelligence fallback (%s)...", sarvam_model)
            result = analyze_transcript_sarvam(transcript_text, user_sarvam_key, sarvam_model)
            if result:
                return result, f"Sarvam AI ({sarvam_model})"
        except Exception as e:
            logging.warning("Sarvam AI analysis failed: %s", e)
            error_messages.append(f"Sarvam: {str(e)}")

    raise RuntimeError(
        f"All configured AI analysis providers failed: {' | '.join(error_messages) if error_messages else 'No API keys provided'}"
    )
