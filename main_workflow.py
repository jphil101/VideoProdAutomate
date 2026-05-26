import os
import sys
import json
import time
import hashlib
import zipfile
import subprocess
import requests
import pyperclip
import static_ffmpeg
import re
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize static FFmpeg binaries (adds ffmpeg and ffprobe to PATH)
static_ffmpeg.add_paths()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
NVIDIA_NIM_API_KEY = os.getenv("NVIDIA_NIM_API_KEY")

from openai import OpenAI
from EnvatoDownloader import EnvatoElementsDownloader, BASE_DOWNLOAD_DIR, human_delay

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

def generate_voiceover_api(segment, script_hash):
    text = segment["voiceover_text"]
    seg_id = segment["segment_id"]
    audio_path = f"{script_hash}_{seg_id}_audio.mp3"
    
    # Check if audio already exists
    if os.path.exists(audio_path):
        print(f"   Audio already exists for {seg_id} -> {audio_path}")
        return True
        
    print(f"   Generating voiceover for {seg_id} via ElevenLabs...")
    
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
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-t", str(duration),
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,format=yuv420p",
        "-c:v", "libx264",
        "-af", af_filter,
        "-c:a", "aac",
        output_path
    ]
    
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_path

def stitch_videos(video_paths, script_hash):
    print("Step 5: Stitching segments together...")
    output_path = f"merged_output_{script_hash}.mp4"
    concat_txt = f"concat_{script_hash}.txt"
    
    with open(concat_txt, "w") as f:
        for vp in video_paths:
            f.write(f"file '{vp}'\n")
            
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
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
        
    def write_chunks(text, start, dur):
        nonlocal ass_content
        words = text.split()
        if not words: return
        CHUNK_SIZE = 4
        chunks = [" ".join(words[i:i + CHUNK_SIZE]) for i in range(0, len(words), CHUNK_SIZE)]
        c_dur = dur / len(chunks)
        c_start = start
        for c in chunks:
            c_end = c_start + c_dur
            ass_content += f"Dialogue: 0,{format_time_ass(c_start)},{format_time_ass(c_end)},Default,,0,0,0,,{c}\n"
            c_start = c_end

    for seg, duration in segment_durations:
        start_time = current_time
        end_time = current_time + duration
        current_time = end_time
        
        text = seg['voiceover_text'].strip()
        
        match = None
        if theme_word:
            pattern = re.compile(re.escape(theme_word), re.IGNORECASE)
            match = pattern.search(text)
            
        if match:
            before_text = text[:match.start()].strip()
            theme_text = text[match.start():match.end()].strip()
            
            if theme_text:
                theme_text = theme_text.upper()
                
            after_text = text[match.end():].strip()
            
            # Character-level interpolation for perfect timing!
            # We give commas and periods a heavy weight to simulate the AI speaker pausing.
            def char_weight(s):
                if not s: return 0
                weight = len(s)
                weight += s.count(',') * 8  # A comma acts like 8 characters of time
                weight += s.count('.') * 12 # A period acts like 12 characters of time
                weight += s.count(' ') * 2  # Spaces take a tiny bit of time
                return weight
                
            w_before = char_weight(before_text)
            w_theme = max(1, char_weight(theme_text))
            w_after = char_weight(after_text)
            total_w = max(1, w_before + w_theme + w_after)
            
            time_per_weight = duration / total_w
            b_dur = w_before * time_per_weight
            t_dur = w_theme * time_per_weight
            a_dur = duration - b_dur - t_dur
            
            write_chunks(before_text, start_time, b_dur)
            
            t_start = start_time + b_dur
            chars = list(theme_text)
            
            # Slower, more dramatic typing
            type_speed = min(0.12, t_dur / len(chars))
            
            # Calculate how long the NEXT 6 words take, capped at 3 seconds max
            a_words = after_text.split()
            extended_dur = 0
            if a_words:
                words_to_keep = min(6, len(a_words))
                extended_dur = (words_to_keep / len(a_words)) * a_dur
                extended_dur = min(3.0, extended_dur)
                
            final_end_time = t_start + t_dur + extended_dur
            
            c_start = t_start
            for i in range(1, len(chars) + 1):
                partial = "".join(chars[:i])
                is_last = (i == len(chars))
                
                c_end = final_end_time if is_last else c_start + type_speed
                
                # Add a subtle POP animation to the final fully typed word
                if is_last:
                    stylized_text = f"{{\\fscx115\\fscy115\\t(0,250,\\fscx100\\fscy100)\\blur2}}{partial}"
                else:
                    stylized_text = partial
                    
                ass_content += f"Dialogue: 0,{format_time_ass(c_start)},{format_time_ass(c_end)},Highlight,,0,0,0,,{stylized_text}\n"
                c_start = c_end
                
            write_chunks(after_text, t_start + t_dur, a_dur)
            
            theme_start = t_start
            theme_end = final_end_time
            theme_word = None
        else:
            write_chunks(text, start_time, duration)
        
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
    if not ELEVENLABS_API_KEY or not NVIDIA_NIM_API_KEY:
        print("Please set ELEVENLABS_API_KEY and NVIDIA_NIM_API_KEY in the .env file.")
        return
        
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
    manual_audio_mode = False
    missing_audio_segments = []
    
    for seg in segments:
        if not manual_audio_mode:
            success = generate_voiceover_api(seg, script_hash)
            if not success:
                print("\n[!] Switching to Manual Audio Mode.")
                manual_audio_mode = True
                
        if manual_audio_mode:
            seg_id = seg["segment_id"]
            if not os.path.exists(f"{script_hash}_{seg_id}_audio.mp3"):
                missing_audio_segments.append(seg)

    if manual_audio_mode and missing_audio_segments:
        print("\n" + "="*60)
        print(" MANUAL AUDIO BATCHING MODE")
        print("="*60)
        print(f"There are {len(missing_audio_segments)} segments that need manual voiceover.\n")
        
        manual_dir = os.path.abspath("manual_audio")
        if not os.path.exists(manual_dir):
            os.makedirs(manual_dir)
            
        existing_manual = [f for f in os.listdir(manual_dir) if f.endswith('.mp3')]
        skip_prompts = False
        
        if existing_manual:
            resp = input(f"\n[!] Found {len(existing_manual)} files already in 'manual_audio/'. Skip text copying and map these files directly? (Y/n): ")
            if resp.strip().lower() != 'n':
                skip_prompts = True
                
        if not skip_prompts:
            for seg in missing_audio_segments:
                text = seg["voiceover_text"]
                pyperclip.copy(text)
                print(f"[{seg['segment_id']}] Text copied to clipboard:")
                print(f"\"{text}\"")
                input("-> Press Enter once you've generated this (or to copy the next one)... ")
                print("-" * 40)
                
            print(f"\nAll texts provided! Please place the downloaded MP3 files into this folder:")
            print(f"{manual_dir}")
            print("\nOpening folder for you...")
            
            if sys.platform == "darwin":
                subprocess.run(["open", manual_dir])
            elif sys.platform == "win32":
                subprocess.run(["start", manual_dir], shell=True)
            
        while True:
            resp = input("\nHave you placed all files in the manual_audio folder? Type 'Y' to continue: ")
            if resp.strip().upper() == 'Y':
                mp3_files = [f for f in os.listdir(manual_dir) if f.endswith('.mp3')]
                if len(mp3_files) < len(missing_audio_segments):
                    print(f"Warning: Found {len(mp3_files)} files, but expected {len(missing_audio_segments)}.")
                    retry = input("Continue anyway? (y/n): ")
                    if retry.lower() != 'y':
                        continue
                break
                
        # Sort files by creation/modification time (ascending) to guarantee chronological mapping
        mp3_files.sort(key=lambda x: os.path.getmtime(os.path.join(manual_dir, x)))
        
        for i, file_name in enumerate(mp3_files):
            if i < len(missing_audio_segments):
                seg_id = missing_audio_segments[i]["segment_id"]
                src = os.path.join(manual_dir, file_name)
                dst = f"{script_hash}_{seg_id}_audio.mp3"
                os.rename(src, dst)
                print(f"Mapped {file_name} -> {dst}")
                
        print("Manual audio mapping complete!")

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
