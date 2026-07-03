import os
import sys
import json
import time
import hashlib
import zipfile
import subprocess
import asyncio
import requests
import static_ffmpeg
import re
from pathlib import Path
from dotenv import load_dotenv

def clipboard_copy(text: str) -> bool:
    """
    Attempts to copy text to the clipboard using a chain of fallbacks:
    1. copykitten (Rust-backed, no external system dependencies like xclip)
    2. pyperclip (uses xclip/xsel/pbcopy/pbpaste or python GUI frameworks)
    3. platform-specific shell tools via subprocess (pbcopy, clip, xclip, xsel, wl-copy)
    
    Returns True if successfully copied, False otherwise.
    """
    # ── Try copykitten ──
    try:
        import copykitten
        # Use detach=True so the clipboard content persists after the python process exits (only relevant on Linux).
        copykitten.copy(text, detach=True)
        return True
    except Exception as e:
        print(f"[DEBUG] copykitten failed: {e}")
        pass

    # ── Try pyperclip ──
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception as e:
        print(f"[DEBUG] pyperclip failed: {e}")
        pass

    # ── Try native subprocess shell utilities ──
    try:
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode('utf-8'), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        elif sys.platform == "win32":
            subprocess.run(["clip"], input=text.encode('utf-8'), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        elif sys.platform.startswith("linux"):
            for cmd in [
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
                ["wl-copy"]
            ]:
                try:
                    subprocess.run(cmd, input=text.encode('utf-8'), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
                except (subprocess.CalledProcessError, FileNotFoundError):
                    continue
    except Exception as e:
        print(f"[DEBUG] subprocess failed: {e}")
        pass

    return False

# Load environment variables
load_dotenv()

# Initialize static FFmpeg binaries (adds ffmpeg and ffprobe to PATH)
static_ffmpeg.add_paths()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
NVIDIA_NIM_API_KEY = os.getenv("NVIDIA_NIM_API_KEY")

from openai import OpenAI
from EnvatoDownloader import EnvatoElementsDownloader, BASE_DOWNLOAD_DIR, human_delay

def check_and_install_whisper():
    try:
        import faster_whisper
        return True
    except ImportError:
        pass
    
    print("\n[!] faster-whisper is not installed. Checking system specs...")
    import platform
    print(f"   OS: {platform.system()} {platform.release()} ({platform.machine()})")
    
    # Check RAM if psutil is available
    try:
        import psutil
        total_ram_gb = psutil.virtual_memory().total / (1024**3)
        print(f"   Total RAM: {total_ram_gb:.1f} GB")
        if total_ram_gb < 3.5:
            print("   ⚠️ Warning: System has less than 4GB RAM. Whisper will be unstable. Falling back to old generation flow.")
            return False
    except ImportError:
        print("   Total RAM: Unknown (psutil not installed)")
        
    # Check GPU if torch is available
    try:
        import torch
        if torch.cuda.is_available():
            print(f"   GPU: CUDA available ({torch.cuda.get_device_name(0)})")
        elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            print("   GPU: Apple MPS available")
        else:
            print("   GPU: None detected. Whisper will run on CPU.")
    except ImportError:
        print("   GPU: Unknown (torch not installed)")

    print("   Installing faster-whisper (this may take a minute)...")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "faster-whisper"], check=True)
        print("   ✅ faster-whisper installed successfully!")
        return True
    except Exception as e:
        print(f"   ❌ Failed to install faster-whisper: {e}")
        return False

# Initialize NIM client
nim_client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=NVIDIA_NIM_API_KEY
)

def get_script_and_segments():
    print("Step 1: Topic & Script Generation")
    script_text = ""
    if os.path.exists("CurrentScript.md"):
        with open("CurrentScript.md", "r") as f:
            script_text = f.read().strip()
            
    if not script_text:
        print("CurrentScript.md not found or empty. Generating topic and script via NIM...")
        prompt = """
        You are a professional video producer. Generate a short script for a YouTube Shorts video (under 60 seconds).
        Topic: A fascinating and constant topic (e.g. interesting facts, history, or science).
        Output ONLY the raw script text. Do not include any formatting or conversational text.
        """
        response = nim_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024
        )
        script_text = response.choices[0].message.content.strip()
        with open("CurrentScript.md", "w") as f:
            f.write(script_text)
        print("Generated and saved script to CurrentScript.md")
    else:
        print("Loaded existing script from CurrentScript.md")
        
    import re
    # Extract dynamic sub-themes from the script
    dynamic_subthemes = re.findall(r'~([^~]+)~', script_text)
    
    # Clean script text (remove ~) for hashing and LLM processing
    clean_script_text = script_text.replace('~', '')
    
    script_hash = hashlib.md5(clean_script_text.encode('utf-8')).hexdigest()
    
    # Check if we already have valid segments for this exact script
    if os.path.exists("segments.json"):
        try:
            with open("segments.json", "r") as f:
                cache_data = json.load(f)
            
            if cache_data.get("script_hash") == script_hash:
                print("Found cached segments.json for this script. Skipping LLM segment generation.")
                theme_word = cache_data.get("theme_word", "Bio-concrete")
                theme_hex_color = cache_data.get("theme_hex_color", "00FF00")
                return cache_data.get("segments", []), theme_word, theme_hex_color, script_hash, dynamic_subthemes
        except Exception as e:
            print(f"Error checking cache: {e}")
            
    print("Segmenting script...")
    segment_prompt = f"""
    You are a video editor. Break the following script down into distinct visual segments.
    Each segment should correspond to 1-3 sentences.
    You must also identify ONE primary theme concept (1-3 words max) that is the absolute core subject of the video. 
    The theme_word MUST be an exact substring of the script text.
    You must also select a vibrant Hex color code (6 characters, no #) that matches this theme.
    
    Output ONLY a JSON object with this exact structure:
    {{
      "theme_word": "The core concept",
      "theme_hex_color": "FF0000",
      "segments": [
        {{
          "segment_id": "seg_1",
          "voiceover_text": "the exact text to be spoken",
          "envato_search_query": "3-5 word search query"
        }}
      ]
    }}
    
    Script:
    {clean_script_text}
    """
    
    for attempt in range(3):
        try:
            response = nim_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=[{"role": "user", "content": segment_prompt}],
                max_tokens=2048,
                response_format={"type": "json_object"} # Try to enforce JSON if supported, otherwise rely on prompt
            )
            
            content = response.choices[0].message.content.strip()
            
            # Try to parse JSON. Sometimes LLMs wrap in ```json
            if content.startswith("```json"):
                content = content[7:-3]
            elif content.startswith("```"):
                content = content[3:-3]
                
            data = json.loads(content)
            segments = data.get("segments", [])
            theme_word = data.get("theme_word", "Concept")
            theme_hex_color = data.get("theme_hex_color", "00FFFF")
                
            # Deterministic Validation: Ensure theme_word is exactly in the script
            if theme_word.lower() not in clean_script_text.lower():
                import string
                # Find all words > 4 chars to pick a meaningful fallback
                valid_words = [w.strip(string.punctuation) for w in clean_script_text.split() if len(w.strip(string.punctuation)) > 4]
                if valid_words:
                    theme_word = max(valid_words, key=len) # Fallback to longest word
                else:
                    theme_word = clean_script_text.split()[0] if clean_script_text else "Concept"
                print(f"⚠️  LLM hallucinated theme word. Falling back to exact substring: '{theme_word}'")
                
            # Save to cache
            with open("segments.json", "w") as f:
                json.dump({
                    "script_hash": script_hash, 
                    "theme_word": theme_word,
                    "theme_hex_color": theme_hex_color,
                    "segments": segments
                }, f, indent=2)
                
            return segments, theme_word, theme_hex_color, script_hash, dynamic_subthemes
        except Exception as e:
            print(f"Attempt {attempt+1} failed to parse JSON. Retrying...")
            if attempt == 2:
                print("Final attempt failed. Raw output:")
                print(content)
                raise e

def generate_voiceover_api(text, audio_path):
    # Check if audio already exists
    if os.path.exists(audio_path):
        print(f"   Audio already exists -> {audio_path}")
        return True
        
    print(f"   Generating voiceover via ElevenLabs...")
    
    # Using a default pleasant voice: Rachel
    voice_id = "21m00Tcm4TlvDq8ikWAM" 
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }
    
    data = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75
        }
    }
    
    try:
        response = requests.post(url, json=data, headers=headers)
        response.raise_for_status()
        
        with open(audio_path, "wb") as f:
            f.write(response.content)
            
        return True
    except requests.exceptions.RequestException as e:
        print(f"   [!] ElevenLabs API failed (Error: {e})")
        return False

def generate_voiceover_edge_tts(text, audio_path, voice="en-US-GuyNeural"):
    """Generate voiceover using Microsoft Edge TTS (free, no API key)."""
    import edge_tts

    if os.path.exists(audio_path):
        print(f"   Audio already exists -> {audio_path}")
        return True

    print(f"   Generating voiceover via Edge TTS (voice: {voice})...")
    try:
        communicate = edge_tts.Communicate(text, voice)
        asyncio.run(communicate.save(audio_path))
        print(f"   ✓ Saved {audio_path}")
        return True
    except Exception as e:
        print(f"   [!] Edge TTS failed (Error: {e})")
        return False
        
def transcribe_and_align(audio_path, original_text):
    from faster_whisper import WhisperModel
    import string
    
    print("   Loading faster-whisper model (base.en)...")
    # Determine compute_type based on platform. cpu typically only supports int8 or float32.
    compute_type = "int8"
    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cuda":
            compute_type = "float16" # GPUs support float16
    except:
        device = "cpu"
        
    model = WhisperModel("base.en", device=device, compute_type=compute_type)
    print("   Extracting word-level timestamps...")
    transcribed_segments, _ = model.transcribe(audio_path, word_timestamps=True)
    
    whisper_words = []
    for segment in transcribed_segments:
        for word in segment.words:
            clean_w = word.word.strip().strip(string.punctuation).lower()
            if clean_w:
                whisper_words.append({
                    "word": word.word.strip(), 
                    "clean": clean_w,
                    "start": word.start, 
                    "end": word.end
                })
                
    # Sequence Alignment to Original Script
    orig_words = original_text.split()
    aligned_timestamps = []
    
    w_idx = 0
    for o_word in orig_words:
        clean_o = o_word.strip(string.punctuation).lower()
        if not clean_o:
            # If the original word is purely punctuation, give it a tiny duration mapped to previous word
            prev_end = aligned_timestamps[-1]["end"] if aligned_timestamps else 0.0
            aligned_timestamps.append({"word": o_word, "start": prev_end, "end": prev_end + 0.01})
            continue
            
        match = None
        for i in range(w_idx, min(w_idx + 8, len(whisper_words))):
            if whisper_words[i]["clean"] == clean_o:
                match = whisper_words[i]
                w_idx = i + 1
                break
                
        if match:
            aligned_timestamps.append({"word": o_word, "start": match["start"], "end": match["end"]})
        else:
            # Interpolate if whisper missed it
            prev_end = aligned_timestamps[-1]["end"] if aligned_timestamps else 0.0
            aligned_timestamps.append({"word": o_word, "start": prev_end + 0.05, "end": prev_end + 0.2})
            
    return aligned_timestamps

def get_audio_duration(audio_path):
    cmd = [
        "ffprobe", "-v", "error", 
        "-show_entries", "format=duration", 
        "-of", "default=noprint_wrappers=1:nokey=1", 
        audio_path
    ]
    output = subprocess.check_output(cmd).decode().strip()
    return float(output)

def process_segment_video(segment, video_path, audio_path, duration, script_hash):
    seg_id = segment["segment_id"]
    output_path = f"temp_{script_hash}_{seg_id}.mp4"
    
    if os.path.exists(output_path):
        print(f"Step 4: Processed video already exists for {seg_id} -> {output_path}")
        return output_path
        
    print(f"Step 4: Processing video for {seg_id}...")
    
    # Crop to 9:16 (1080x1920) and trim to exact padded duration. Also normalize fps to 30.
    # We use a 0.05s fade-in and 0.1s fade-out on the original audio to eliminate any pops, clicks, or abruptly cut breaths.
    # We then use the 'apad' filter to pad the audio with silence at the end so it matches the video duration.
    original_duration = duration - 0.4
    fade_out_start = max(0, original_duration - 0.1)
    af_filter = f"afade=t=in:ss=0:d=0.05,afade=t=out:st={fade_out_start}:d=0.1,apad"
    
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-t", str(duration),
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,setsar=1,format=yuv420p",
        "-c:v", "libx264",
        "-af", af_filter,
        "-c:a", "aac",
        output_path
    ]
    
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_path

def stitch_videos(video_paths, script_hash):
    print("Step 5: Stitching segments together (with crossfade transitions)...")
    output_path = f"merged_output_{script_hash}.mp4"
    
    CROSSFADE_DURATION = 0.3  # seconds of overlap between segments

    if len(video_paths) == 1:
        # Single segment — just copy it
        import shutil as _shutil
        _shutil.copy2(video_paths[0], output_path)
        return output_path

    # Build xfade filter chain for smooth transitions between segments
    # Each xfade needs the offset = (cumulative duration so far) - (crossfade * transition_index)
    # We need durations of each segment to compute offsets.
    durations = []
    for vp in video_paths:
        dur = float(subprocess.check_output([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            vp
        ]).decode().strip())
        durations.append(dur)

    # Build the filter_complex for N-1 crossfades
    n = len(video_paths)
    filter_parts = []
    cumulative_dur = durations[0]

    for i in range(n - 1):
        offset = cumulative_dur - CROSSFADE_DURATION
        offset = max(0, offset)  # safety clamp

        if i == 0:
            src_a = "[0:v]"
        else:
            src_a = f"[xf{i-1}]"

        src_b = f"[{i+1}:v]"

        if i < n - 2:
            out_label = f"[xf{i}]"
        else:
            out_label = "[vout]"

        filter_parts.append(
            f"{src_a}{src_b}xfade=transition=fade:duration={CROSSFADE_DURATION}:offset={offset:.3f}{out_label}"
        )

        cumulative_dur = offset + durations[i + 1]

    # Audio: concat all audio streams
    audio_inputs = "".join(f"[{i}:a]" for i in range(n))
    filter_parts.append(f"{audio_inputs}concat=n={n}:v=0:a=1[aout]")

    filter_complex = ";\n".join(filter_parts)

    cmd = ["ffmpeg", "-y"]
    for vp in video_paths:
        cmd.extend(["-i", vp])
    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-movflags", "+faststart",
        output_path
    ])

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        # Fallback to simple concat if xfade fails (e.g. mismatched formats)
        print("   ⚠️  Crossfade failed, falling back to simple concat...")
        concat_txt = f"concat_{script_hash}.txt"
        with open(concat_txt, "w") as f:
            for vp in video_paths:
                f.write(f"file '{vp}'\n")
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", concat_txt,
            "-c", "copy",
            output_path
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return output_path

import re

def create_ass(segment_durations, theme_word, theme_hex_color, script_hash):
    theme_start = None
    theme_end = None

    if theme_hex_color and len(theme_hex_color) == 6:
        R, G, B = theme_hex_color[0:2], theme_hex_color[2:4], theme_hex_color[4:6]
        ass_color = f"&H00{B}{G}{R}"
    else:
        ass_color = "&H0000FFFF"

    ass_content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,42,&H00FFFFFF,&H000000FF,&H00000000,&H90000000,1,0,0,0,100,100,0,0,3,18,0,2,100,100,250,1
Style: Highlight,Impact,140,{ass_color},&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,8,12,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    current_time = 0.0
    
    def format_time_ass(seconds):
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        cs = int(round((seconds - int(seconds)) * 100))
        return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"
        
    def write_chunks(words_data, global_offset):
        nonlocal ass_content
        if not words_data: return
        CHUNK_SIZE = 4
        chunks = [words_data[i:i + CHUNK_SIZE] for i in range(0, len(words_data), CHUNK_SIZE)]
        for chunk in chunks:
            c_start = global_offset + chunk[0]["start"]
            c_end = global_offset + chunk[-1]["end"]
            text = " ".join([w["word"] for w in chunk])
            ass_content += f"Dialogue: 0,{format_time_ass(c_start)},{format_time_ass(c_end)},Default,,0,0,0,,{text}\n"

    for seg, duration in segment_durations:
        start_time = current_time
        end_time = current_time + duration
        current_time = end_time
        
        words_data = seg.get("word_timestamps", [])
        if not words_data:
            # Fallback to character-level timing interpolation if Whisper is unavailable
            def char_weight(s):
                if not s: return 0
                weight = len(s)
                weight += s.count(',') * 8
                weight += s.count('.') * 12
                weight += s.count('?') * 12
                weight += s.count('!') * 12
                return weight
                
            words = seg["voiceover_text"].split()
            total_weight = max(1, sum(char_weight(w) for w in words))
            time_per_weight = duration / total_weight
            
            words_data = []
            curr_start = 0.0
            for w in words:
                # Minimum weight of 1 so short words don't flash instantly
                w_dur = max(1, char_weight(w)) * time_per_weight
                words_data.append({"word": w, "start": curr_start, "end": curr_start + w_dur})
                curr_start += w_dur
            
        theme_matched = False
        if theme_word and theme_word.lower() in seg["voiceover_text"].lower():
            # Find which word indices correspond to the theme word
            idx = seg["voiceover_text"].lower().find(theme_word.lower())
            if idx != -1:
                words_before = len(seg["voiceover_text"][:idx].split())
                theme_len = len(theme_word.split())
                
                before_words = words_data[:words_before]
                theme_words = words_data[words_before : words_before + theme_len]
                after_words = words_data[words_before + theme_len:]
                
                write_chunks(before_words, start_time)
                
                if theme_words:
                    t_start = start_time + theme_words[0]["start"]
                    t_dur = max(0.1, theme_words[-1]["end"] - theme_words[0]["start"])
                    
                    theme_text = " ".join([w["word"] for w in theme_words]).upper()
                    chars = list(theme_text)
                    type_speed = min(0.12, t_dur / max(1, len(chars)))
                    
                    # Calculate extended duration for the pop animation to linger
                    a_dur = max(0.1, after_words[-1]["end"] - after_words[0]["start"]) if after_words else 0
                    extended_dur = 0
                    if after_words:
                        words_to_keep = min(6, len(after_words))
                        extended_dur = (words_to_keep / len(after_words)) * a_dur
                        extended_dur = min(3.0, extended_dur)
                        
                    final_end_time = t_start + t_dur + extended_dur
                    
                    c_start = t_start
                    for i in range(1, len(chars) + 1):
                        partial = "".join(chars[:i])
                        is_last = (i == len(chars))
                        c_end = final_end_time if is_last else c_start + type_speed
                        
                        if is_last:
                            stylized_text = f"{{\\fscx115\\fscy115\\t(0,250,\\fscx100\\fscy100)\\blur2}}{partial}"
                        else:
                            stylized_text = partial
                            
                        ass_content += f"Dialogue: 0,{format_time_ass(c_start)},{format_time_ass(c_end)},Highlight,,0,0,0,,{stylized_text}\n"
                        c_start = c_end
                        
                    write_chunks(after_words, start_time)
                    
                    theme_start = t_start
                    theme_end = final_end_time
                    theme_word = None
                    theme_matched = True
        
        if not theme_matched:
            write_chunks(words_data, start_time)
        
    ass_path = f"subtitles_{script_hash}.ass"
    with open(ass_path, "w") as f:
        f.write(ass_content)
        
    return theme_start, theme_end, ass_path

def generate_and_burn_subtitles(video_path, theme_word, theme_hex_color, theme_start, theme_end, script_hash, ass_path):
    print("Step 6: Burning pixel-perfect ASS subtitles and overlaying theme images...")
    output_path = f"final_video_{script_hash}.mp4"
    
    # Setup theme images
    images_dir = Path(f"theme_images_{script_hash}")
    images_dir.mkdir(exist_ok=True)
    valid_exts = {".png", ".jpg", ".jpeg", ".webp", ".tiff", ".tif"}
    theme_images = [p for p in images_dir.iterdir() if p.suffix.lower() in valid_exts][:3]
    
    try:
        if not theme_images or theme_start is None or theme_end is None:
            # Simple subtitle burn if no images exist
            cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-vf", f"subtitles={ass_path}",
                "-c:v", "libx264",
                "-c:a", "copy",
                output_path
            ]
        else:
            print(f"Overlaying {len(theme_images)} image(s) from {theme_start:.2f}s to {theme_end:.2f}s...")
            # Calculate explicit duration for image inputs to prevent infinite stream hangs
            # Must be long enough to reach theme_end because the stream timestamp starts at 0
            img_input_duration = theme_end + 1.0
            
            cmd = ["ffmpeg", "-y", "-i", video_path]
            for img in theme_images:
                # Limit looped image stream to only the needed duration to prevent FFmpeg hanging
                cmd.extend(["-loop", "1", "-framerate", "30", "-t", f"{img_input_duration:.2f}", "-i", str(img)])
                
            filter_complex = ""
            last_bg = "[0:v]"
            
            for i, img in enumerate(theme_images):
                img_idx = i + 1
                
                # Stack from top to bottom
                if len(theme_images) == 1:
                    y_pos = "(H-h)/2"
                elif len(theme_images) == 2:
                    y_pos = "(H-h)/3" if i == 0 else "2*(H-h)/3"
                else: # 3 images
                    y_pos = "(H-h)/4" if i == 0 else ("(H-h)/2" if i == 1 else "3*(H-h)/4")
                    
                x_pos = "(W-w)/2"
                
                t_s = f"{theme_start:.2f}"
                t_e = f"{theme_end:.2f}"
                
                # Scale cleanly, apply 45% alpha, and fade in/out
                img_filter = f"[{img_idx}:v]format=rgba,colorchannelmixer=aa=0.45,scale='min(800,iw)':'min(600,ih)':force_original_aspect_ratio=decrease,fade=t=in:st={t_s}:d=0.3:alpha=1,fade=t=out:st={t_e}:d=0.3:alpha=1[img{i}];"
                filter_complex += img_filter
                
                next_bg = f"[bg{i+1}]" if i < len(theme_images) - 1 else "[v_overlaid]"
                overlay_filter = f"{last_bg}[img{i}]overlay=x={x_pos}:y={y_pos}:enable='between(t,{t_s},{theme_end + 0.3:.2f})'{next_bg};"
                filter_complex += overlay_filter
                
                last_bg = next_bg
                
            filter_complex += f"[v_overlaid]subtitles={ass_path}[final_v]"
            
            cmd.extend([
                "-filter_complex", filter_complex,
                "-map", "[final_v]",
                "-map", "0:a",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                "-shortest",
                output_path
            ])

        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"Success! Final video saved to {output_path}")
    except Exception as e:
        print(f"Could not burn subtitles. Error: {e}")
        os.rename(video_path, output_path)

def main():
    if not NVIDIA_NIM_API_KEY:
        print("Please set NVIDIA_NIM_API_KEY in the .env file. It is required for script generation.")
        return

    # Check and install Whisper for timestamp generation
    whisper_available = check_and_install_whisper()
    if not whisper_available:
        print("⚠️ Whisper is unavailable. System will use legacy fallback generation (segment-by-segment).")

    elevenlabs_available = bool(ELEVENLABS_API_KEY and ELEVENLABS_API_KEY.strip())
    if not elevenlabs_available:
        print("⚠️  ELEVENLABS_API_KEY is empty — will use fallback TTS for voiceovers.")
        
    segments_data = get_script_and_segments()
    if not segments_data: return
    segments, theme_word, theme_hex_color, script_hash, dynamic_subthemes = segments_data
    
    print(f"Generated {len(segments)} segments. Theme: {theme_word} ({theme_hex_color})")
    
    # ---------------------------------------------------------
    # TAG & ISOLATE THEME IMAGES FOR THIS SCRIPT
    # ---------------------------------------------------------
    import shutil
    src_theme_dir = Path("theme_images")
    dst_theme_dir = Path(f"theme_images_{script_hash}")
    
    # Clean up old orphaned tagged directories from previous scripts
    base_dir = Path(".")
    for d in base_dir.glob("theme_images_*"):
        if d.is_dir() and d.name != f"theme_images_{script_hash}":
            shutil.rmtree(str(d), ignore_errors=True)
            print(f"Cleaned up old script cache: {d.name}/")
        
    dst_theme_dir.mkdir(exist_ok=True)
    
    valid_img_exts = {".png", ".jpg", ".jpeg", ".webp", ".tiff", ".tif"}
    
    if src_theme_dir.exists():
        for f in src_theme_dir.iterdir():
            if f.is_file() and f.suffix.lower() in valid_img_exts:
                shutil.move(str(f), str(dst_theme_dir / f.name))
                print(f"Tagged theme image: {f.name} -> {dst_theme_dir.name}/")
                
        src_subthemes = src_theme_dir / "SubThemes"
        dst_subthemes = dst_theme_dir / "SubThemes"
        if src_subthemes.exists():
            dst_subthemes.mkdir(exist_ok=True)
            for f in src_subthemes.iterdir():
                if f.is_file() and f.suffix.lower() in valid_img_exts:
                    shutil.move(str(f), str(dst_subthemes / f.name))
                    print(f"Tagged SubTheme image: {f.name} -> {dst_subthemes.name}/")
        
        # Clean up the source theme_images/ directory if only .DS_Store or nothing remains
        remaining = [f for f in src_theme_dir.iterdir() if f.name != ".DS_Store" and not (f.is_dir() and not any(f.iterdir()))]
        if not remaining:
            shutil.rmtree(str(src_theme_dir), ignore_errors=True)
            print(f"Cleaned up empty source directory: {src_theme_dir}/")
    
    # ---------------------------------------------------------
    # PHASE 1: DOWNLOAD ALL VIDEOS (Single Browser Session)
    # ---------------------------------------------------------
    print("\n=== PHASE 1: ENVATO VIDEO DOWNLOADS ===")
    videos_to_download = []
    video_paths = {}
    valid_exts = (".mp4", ".mov", ".webm", ".mkv", ".avi")
    
    for seg in segments:
        seg_id = seg["segment_id"]
        download_dir = BASE_DOWNLOAD_DIR / script_hash / seg_id
        download_dir.mkdir(parents=True, exist_ok=True)
        
        # Unzip any stranded zip files before checking
        zip_files = [f for f in os.listdir(download_dir) if f.lower().endswith('.zip')]
        for zf in zip_files:
            zip_path = download_dir / zf
            print(f"Unzipping existing archive {zf} in {seg_id}...")
            try:
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(download_dir)
                os.remove(zip_path)
            except Exception as e:
                print(f"Failed to unzip {zf}: {e}")
                
        files = [f for f in os.listdir(download_dir) if f.lower().endswith(valid_exts)]
        if files:
            video_paths[seg_id] = str(download_dir / files[0])
            print(f"Video already exists for {seg_id} -> {video_paths[seg_id]}")
        else:
            videos_to_download.append(seg)
            
    if videos_to_download:
        downloader = EnvatoElementsDownloader(download_dir=BASE_DOWNLOAD_DIR, headless=False)
        try:
            downloader.start()
            downloader.ensure_logged_in()
            
            failed_downloads = videos_to_download.copy()
            max_retries = 3
            current_retry = 0
            
            while failed_downloads and current_retry <= max_retries:
                if current_retry > 0:
                    print(f"\n🔄 Auto-retry pass {current_retry}/{max_retries} for {len(failed_downloads)} failed segments...")
                    
                retry_queue = failed_downloads.copy()
                failed_downloads.clear()
                
                for seg in retry_queue:
                    seg_id = seg["segment_id"]
                    query = seg["envato_search_query"]
                    print(f"\nSearching for {seg_id}: {query}")
                    
                    seg_dir = BASE_DOWNLOAD_DIR / script_hash / seg_id
                    # Modify downloader's destination dynamically
                    downloader.download_dir = seg_dir
                    
                    links = downloader.search_and_get_links(query, 1)
                    success = False
                    if links:
                        success = downloader.download_item(1, links[0])
                        if success:
                            # Handle zip files
                            downloaded_zips = [f for f in os.listdir(seg_dir) if f.lower().endswith('.zip')]
                            for zf in downloaded_zips:
                                zip_path = seg_dir / zf
                                print(f"   Unzipping {zf}...")
                                try:
                                    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                                        zip_ref.extractall(seg_dir)
                                    os.remove(zip_path)
                                    print(f"   Deleted {zf}")
                                except Exception as e:
                                    print(f"   Error unzipping {zf}: {e}")
                            
                            files = [f for f in os.listdir(seg_dir) if f.lower().endswith(valid_exts)]
                            if files:
                                video_paths[seg_id] = str(seg_dir / files[0])
                            else:
                                success = False
                    
                    if not success:
                        failed_downloads.append(seg)
                        
                    human_delay(3, 5)
                    
                current_retry += 1
        finally:
            downloader.stop()
            
    # Check if all videos are present
    missing_videos = [s for s in segments if s["segment_id"] not in video_paths]
    if missing_videos:
        print(f"Warning: Could not download videos for {len(missing_videos)} segments.")
        # Proceed with what we have
        segments = [s for s in segments if s["segment_id"] in video_paths]
        
    if not segments:
        print("No segments have videos. Exiting.")
        return

    # ---------------------------------------------------------
    # PHASE 1.5: SUB-THEME PROCESSING
    # ---------------------------------------------------------
    print("\n=== PHASE 1.5: SUB-THEME PROCESSING ===")
    from subtheme_processor import (
        discover_subthemes, match_subthemes_to_segments,
        generate_subtheme_videos, compute_interleave_plan,
        build_interleaved_segment,
    )
    
    # Process dynamic subthemes (Option 2) before discovery
    dst_subthemes = dst_theme_dir / "SubThemes"
    dst_subthemes.mkdir(exist_ok=True)
    
    missing_dynamic = []
    for phrase in dynamic_subthemes:
        # Check if already cached (ignore extension)
        existing = list(dst_subthemes.glob(f"{phrase}.*"))
        if not existing:
            missing_dynamic.append(phrase)
            
    if missing_dynamic:
        print(f"Found {len(missing_dynamic)} dynamic sub-theme(s) needing download...")
        downloader = EnvatoElementsDownloader(download_dir=dst_subthemes, headless=False, media_type="photo")
        try:
            downloader.start()
            downloader.ensure_logged_in()
            for phrase in missing_dynamic:
                links = downloader.search_and_get_links(phrase, 1)
                if links:
                    success = downloader.download_item(1, links[0])
                    if success:
                        # Rename the downloaded photo to the exact phrase
                        downloaded_photos = [f for f in os.listdir(dst_subthemes) if f.startswith("1_photo_item")]
                        for photo in downloaded_photos:
                            ext = os.path.splitext(photo)[1]
                            old_path = dst_subthemes / photo
                            new_path = dst_subthemes / f"{phrase}{ext}"
                            if new_path.exists():
                                os.remove(new_path)
                            os.rename(old_path, new_path)
                            print(f"✅ Renamed {photo} to {phrase}{ext}")
                human_delay(2, 4)
        finally:
            downloader.stop()

    subthemes = discover_subthemes(script_hash)
    subtheme_matches = {}
    subtheme_video_map = {}

    if subthemes:
        print(f"Found {len(subthemes)} sub-theme image(s):")
        for st in subthemes:
            print(f"   - {st.keywords_str} ({Path(st.image_path).name})")

        subtheme_matches = match_subthemes_to_segments(subthemes, segments)

        if subtheme_matches:
            print(f"\nMatched sub-themes to {len(subtheme_matches)} segment(s):")
            for seg_id, matches in subtheme_matches.items():
                for st, pos in matches:
                    print(f"   {seg_id}: '{st.keywords_str}' at char {pos}")

            subtheme_video_map = generate_subtheme_videos(subtheme_matches)
        else:
            print("No sub-theme keywords matched any segment text.")
    else:
        print("No sub-theme images found in theme_images/SubThemes/")

    # ---------------------------------------------------------
    # PHASE 2: GENERATE ALL AUDIO
    # ---------------------------------------------------------
    print("\n=== PHASE 2: AUDIO GENERATION ===")
    
    if whisper_available:
        full_audio_path = f"full_audio_{script_hash}.mp3"
        full_text = " ".join([seg["voiceover_text"] for seg in segments])
        
        audio_ready = False
        if elevenlabs_available:
            audio_ready = generate_voiceover_api(full_text, full_audio_path)
        
        if not audio_ready:
            print("\n[!] ElevenLabs unavailable or failed. Switching to fallback.")
            print("\n" + "="*60)
            print(" FALLBACK — CHOOSE TTS METHOD")
            print("="*60)
            
            clipboard_works = clipboard_copy("test")
            if clipboard_works:
                print("  [1] 📋 Manual Voiceover (Full script copied to your clipboard)")
            else:
                print("  [1] 📋 Manual Voiceover (Full script printed to terminal)")
            print("  [2] 🤖 Auto-generate with Edge TTS (free, no API key)")
            choice = input("\nSelect option (1 or 2): ").strip()
            
            if choice == "2":
                print("\n🤖 Using Edge TTS...")
                audio_ready = generate_voiceover_edge_tts(full_text, full_audio_path)
                
            if not audio_ready:
                print("\n[!] Please generate manual audio for the full script.")
                print(f"\n   ── TEXT START ──\n   {full_text}\n   ── TEXT END ──\n")
                
                if clipboard_works:
                    clipboard_copy(full_text)
                    print("📋 Full script copied to clipboard!")
                    
                input(f"   → Generate TTS manually, save it precisely as '{full_audio_path}' in the current folder, and press Enter...")
            
        aligned_words = transcribe_and_align(full_audio_path, full_text)
        
        import string
        print("\n=== Splitting Full Audio into Segments ===")
        word_ptr = 0
        for seg in segments:
            seg_id = seg["segment_id"]
            seg_text = seg["voiceover_text"]
            
            seg_word_count = len([w for w in seg_text.split() if w.strip(string.punctuation).lower()])
            seg_words = aligned_words[word_ptr : word_ptr + seg_word_count]
            word_ptr += seg_word_count
            
            if seg_words:
                start_time = seg_words[0]["start"]
                end_time = seg_words[-1]["end"]
            else:
                start_time, end_time = 0.0, 1.0
                
            end_time += 0.3
            
            seg_audio = f"{script_hash}_{seg_id}_audio.mp3"
            if not os.path.exists(seg_audio):
                print(f"   Extracting audio for {seg_id} ({start_time:.2f}s - {end_time:.2f}s)...")
                cmd = ["ffmpeg", "-y", "-i", full_audio_path, "-ss", str(start_time), "-to", str(end_time), "-c", "copy", seg_audio]
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
            seg["word_timestamps"] = []
            for w in seg_words:
                seg["word_timestamps"].append({
                    "word": w["word"],
                    "start": max(0.0, w["start"] - start_time),
                    "end": max(0.0, w["end"] - start_time)
                })
    else:
        print("Using legacy segment-by-segment generation (whisper unavailable).")
        fallback_needed = not elevenlabs_available
        missing_audio_segments = []

        if fallback_needed:
            for seg in segments:
                seg_id = seg["segment_id"]
                if not os.path.exists(f"{script_hash}_{seg_id}_audio.mp3"):
                    missing_audio_segments.append(seg)
        else:
            for seg in segments:
                seg_id = seg["segment_id"]
                audio_path = f"{script_hash}_{seg_id}_audio.mp3"
                if not generate_voiceover_api(seg["voiceover_text"], audio_path):
                    print("\n[!] ElevenLabs API call failed. Switching to fallback.")
                    fallback_needed = True
                    if not os.path.exists(audio_path):
                        missing_audio_segments.append(seg)
                    for r_seg in segments[segments.index(seg)+1:]:
                        if not os.path.exists(f"{script_hash}_{r_seg['segment_id']}_audio.mp3"):
                            missing_audio_segments.append(r_seg)
                    break
                    
        if fallback_needed and missing_audio_segments:
            print("\n" + "="*60)
            print(" FALLBACK — CHOOSE TTS METHOD")
            print("="*60)
            print("  [1] 📋 Manual Voiceover (Segment by Segment)")
            print("  [2] 🤖 Auto-generate with Edge TTS (free, no API key)")
            choice = input("\nSelect option (1 or 2): ").strip()
            
            if choice == "2":
                print("\n🤖 Using Edge TTS...")
                for seg in missing_audio_segments:
                    generate_voiceover_edge_tts(seg["voiceover_text"], f"{script_hash}_{seg['segment_id']}_audio.mp3")
            else:
                manual_dir = os.path.abspath("manual_audio")
                os.makedirs(manual_dir, exist_ok=True)
                for idx, seg in enumerate(missing_audio_segments, 1):
                    text = seg["voiceover_text"]
                    clipboard_copy(text)
                    print(f"\n📋 [{idx}/{len(missing_audio_segments)}] Segment '{seg['segment_id']}' copied to clipboard:")
                    print(f"   ── TEXT START ──\n   {text}\n   ── TEXT END ──")
                    input("   → Press Enter when you've generated & downloaded this segment's audio... ")
                    
                import shutil
                dl_mp3s = sorted([f for f in Path.home().joinpath("Downloads").iterdir() if f.suffix.lower() == '.mp3' and f.stat().st_mtime >= time.time() - 1800], key=lambda f: f.stat().st_mtime)
                if dl_mp3s:
                    if input(f"\nMove {len(dl_mp3s)} recent MP3(s) from ~/Downloads to 'manual_audio/'? (Y/n): ").strip().lower() != 'n':
                        for f in dl_mp3s: shutil.move(str(f), str(Path(manual_dir) / f.name))
                        
                mp3_files = sorted([f for f in os.listdir(manual_dir) if f.lower().endswith('.mp3')], key=lambda x: os.path.getmtime(os.path.join(manual_dir, x)))
                for i, file_name in enumerate(mp3_files):
                    if i < len(missing_audio_segments):
                        os.rename(os.path.join(manual_dir, file_name), f"{script_hash}_{missing_audio_segments[i]['segment_id']}_audio.mp3")
                print("✅ Manual audio mapping complete!")

    # Verify all audio is present before proceeding
    segments = [s for s in segments if os.path.exists(f"{script_hash}_{s['segment_id']}_audio.mp3")]

    if not segments:
        print("No segments have audio. Exiting.")
        return

    # ---------------------------------------------------------
    # PHASE 3: PROCESS & STITCH
    # ---------------------------------------------------------
    print("\n=== PHASE 3: VIDEO PROCESSING ===")
    processed_videos = []
    segment_durations = []
    
    for seg in segments:
        seg_id = seg["segment_id"]
        vid_path = video_paths[seg_id]
        aud_path = f"{script_hash}_{seg_id}_audio.mp3"
        
        # Add 0.4 seconds of padding for a natural pacing pause
        duration = get_audio_duration(aud_path) + 0.4
        segment_durations.append((seg, duration))
        
        if seg_id in subtheme_matches and subtheme_video_map:
            # Build interleaved segment with sub-theme clips
            clips = compute_interleave_plan(
                seg, subtheme_matches[seg_id], duration, subtheme_video_map,
            )
            final_seg_path = build_interleaved_segment(
                clips, vid_path, aud_path, duration, seg_id, script_hash
            )
            if final_seg_path is None:
                # Fallback to normal processing if interleaving failed
                final_seg_path = process_segment_video(seg, vid_path, aud_path, duration, script_hash)
        else:
            final_seg_path = process_segment_video(seg, vid_path, aud_path, duration, script_hash)
        
        processed_videos.append(final_seg_path)
        
    merged_video = stitch_videos(processed_videos, script_hash)
    
    # Generate subtitles file based on timings
    theme_start, theme_end, ass_path = create_ass(segment_durations, theme_word, theme_hex_color, script_hash)
    generate_and_burn_subtitles(merged_video, theme_word, theme_hex_color, theme_start, theme_end, script_hash, ass_path)
    
    # Cleanup temp files
    print("\nCleaning up temporary files...")
    for vp in processed_videos:
        if os.path.exists(vp):
            os.remove(vp)
    if os.path.exists(f"concat_{script_hash}.txt"):
        os.remove(f"concat_{script_hash}.txt")
    if os.path.exists(f"merged_output_{script_hash}.mp4"):
        os.remove(f"merged_output_{script_hash}.mp4")
    if os.path.exists(f"full_audio_{script_hash}.wav"):
        os.remove(f"full_audio_{script_hash}.wav")
    # Cleanup ASS subtitles file
    if os.path.exists(f"subtitles_{script_hash}.ass"):
        os.remove(f"subtitles_{script_hash}.ass")
        print(f"Cleaned up subtitles: subtitles_{script_hash}.ass")
        
    print(f"Workflow complete! Saved as final_video_{script_hash}.mp4")

if __name__ == "__main__":
    main()
