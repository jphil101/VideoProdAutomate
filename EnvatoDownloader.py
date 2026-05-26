"""
Envato Elements Browser Automation Downloader

Automates searching and downloading full-quality, licensed videos
from Envato Elements (app.envato.com) using CloakBrowser.

First run: Will open a visible browser and wait for you to log in.
Subsequent runs: Will use the saved session to run automatically.
"""

import os
import re
import sys
import time
import random
import argparse
from pathlib import Path

try:
    from cloakbrowser import launch_persistent_context
    from playwright.sync_api import TimeoutError
except ImportError:
    print("ERROR: Missing dependencies. Please run:")
    print("       pip install cloakbrowser playwright")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TOP_N = 5
DOWNLOAD_TIMEOUT_MS = 300000  # 5 minutes

SCRIPT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = SCRIPT_DIR / ".envato_session"
BASE_DOWNLOAD_DIR = SCRIPT_DIR / "downloads"

# Ensure base download directory exists
BASE_DOWNLOAD_DIR.mkdir(exist_ok=True)


def sanitize_filename(name: str) -> str:
    """Remove characters that are unsafe for filenames."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:80]


def human_delay(min_sec=1.0, max_sec=3.0):
    """Add a random delay to seem more human."""
    time.sleep(random.uniform(min_sec, max_sec))


class EnvatoElementsDownloader:
    def __init__(self, download_dir: Path, headless=False, media_type="video"):
        self.headless = headless
        self.context = None
        self.page = None
        self.download_dir = download_dir
        self.media_type = media_type

    def start(self):
        """Launch the stealth browser with persistent session."""
        print(f"🚀 Launching CloakBrowser (Profile: {PROFILE_DIR.name})...")
        
        # We need a persistent context so we don't have to login every time.
        # humanize=True adds realistic mouse movements and typing delays.
        # We handle downloads explicitly by accepting downloads.
        self.context = launch_persistent_context(
            PROFILE_DIR,
            headless=self.headless,
            humanize=True,
            accept_downloads=True,
            args=["--window-size=1920,1080"],
            viewport={"width": 1920, "height": 1080}
        )
        self.page = self.context.new_page()

    def stop(self):
        """Close the browser."""
        if self.context:
            self.context.close()

    def ensure_logged_in(self):
        """Check if logged in, otherwise pause for manual login."""
        self.page.goto("https://app.envato.com/", wait_until="domcontentloaded")
        
        print("\n" + "!" * 60)
        print("  ACTION REQUIRED:")
        print("  Please look at the automated browser window.")
        print("  If you are NOT logged in, please log into Envato Elements now.")
        print("  Once you are fully logged in (or if you already are),")
        print("  PRESS ENTER HERE IN THE TERMINAL TO CONTINUE.")
        print("!" * 60 + "\n")
        
        input("Press Enter to continue searching and downloading... ")
        print("✅ Proceeding with saved session...")
        human_delay(1, 2)

    def search_and_get_links(self, phrase: str, count: int) -> list[str]:
        """Search for items and return URLs."""
        print(f"\n🔍 Searching for {self.media_type}s: '{phrase}'")
        encoded_phrase = phrase.replace(" ", "+")
        search_url = f"https://app.envato.com/search/all?term={encoded_phrase}"
        self.page.goto(search_url, wait_until="domcontentloaded")
        human_delay(3, 5)

        # Scroll down a bit to load results
        self.page.mouse.wheel(0, 500)
        human_delay(1, 2)
        self.page.mouse.wheel(0, 500)
        human_delay(1, 2)

        links = []
        try:
            # Envato Elements links typically use /stock-video/ or /video/ for videos,
            # and /photo/ or -photo/ for photos.
            search_str1 = f"-{self.media_type}/"
            search_str2 = f"/{self.media_type}/"
            
            self.page.wait_for_selector(f"a[href*='{search_str1}']", timeout=10000)
            
            elements = self.page.locator(f"a[href*='{search_str1}']").all()
            if not elements:
                elements = self.page.locator(f"a[href*='{search_str2}']").all()
                
            for el in elements:
                href = el.get_attribute("href")
                if href and (search_str1 in href or search_str2 in href) and href not in links:
                    links.append(href)
                    if len(links) >= count:
                        break
        except TimeoutError:
            print(f"⚠️  No {self.media_type} results found or page structure changed.")

        # Ensure absolute URLs and bypass the modal overlay
        absolute_links = []
        for link in links:
            # If the link opens the modal overlay (e.g. /search/all/stock-video/...)
            # we strip out the search part to get the standalone item page URL
            clean_link = link.replace("/search/all/", "/")
            clean_link = clean_link.split("?")[0] # remove search tracking parameters
            
            if clean_link.startswith("http"):
                absolute_links.append(clean_link)
            else:
                absolute_links.append(f"https://app.envato.com{clean_link}")
                
        print(f"✅ Found {len(absolute_links)} {self.media_type} links.")
        return absolute_links

    def download_item(self, index: int, item_url: str):
        """Navigate to an item page, trigger download, and save the file."""
        print(f"\n── Result {index}: {item_url}")
        self.page.goto(item_url, wait_until="domcontentloaded")
        human_delay(2, 4)

        # Get item title for filename
        title = f"{self.media_type}_item"
        try:
            title_el = self.page.locator("h1").first
            if title_el.is_visible():
                title = title_el.inner_text()
        except:
            pass

        # We define a base filename without extension first
        base_filename = f"{index}_{sanitize_filename(title)}"
        
        # Click the download button
        download_btn = self.page.locator("button:has-text('Download')").first
        try:
            download_btn.wait_for(state="visible", timeout=30000)
        except TimeoutError:
            print("   ❌ 'Download' button not found after waiting. You may need a subscription or the page structure changed.")
            return False

        # The click must be wrapped INSIDE expect_download, otherwise Playwright misses the event 
        # and the browser downloads it natively into the default Downloads folder.
        try:
            print("   ⏳ Triggering download... (waiting up to 60s for server)")
            with self.page.expect_download(timeout=60000) as download_info:
                print("   🖱️  Clicking Download button...")
                download_btn.click()
                
                # Check if we need to assign a project (license dialog)
                project_dialog = self.page.locator("text='Add & Download'").first
                try:
                    project_dialog.wait_for(state="visible", timeout=3000)
                    print("   📝 License dialog detected. Clicking 'Add & Download'...")
                    project_dialog.click()
                except TimeoutError:
                    pass
                    
            download = download_info.value
            
            # Use the actual file extension from Envato's server
            original_filename = download.suggested_filename
            ext = os.path.splitext(original_filename)[1]
            if not ext:
                ext = ".mp4" if self.media_type == "video" else ".jpg"  # fallback if no extension provided
                
            final_filename = f"{base_filename}{ext}"
            filepath = self.download_dir / final_filename
            
            print(f"   ⬇️  Downloading file... (saving to {final_filename})")
            
            # Save the download to our folder instead of the default native downloads folder
            download.save_as(filepath)
            
            size_mb = filepath.stat().st_size / (1024 * 1024)
            print(f"   ✅ Saved {size_mb:.1f} MB to {filepath}")
            return True
            
        except TimeoutError:
            print("   ❌ Download did not start within the timeout.")
            return False
        except Exception as e:
            print(f"   ❌ Error during download: {e}")
            return False


def main():
    parser = argparse.ArgumentParser(description="Envato Elements CloakBrowser Downloader")
    parser.add_argument("--term", type=str, required=True, help="The exact search phrase (e.g., 'modular building construction')")
    parser.add_argument("--segment", type=str, required=True, help="Unique segment number/id for the subfolder")
    parser.add_argument("--count", type=int, default=TOP_N, help="Number of items to download (default 5)")
    parser.add_argument("--type", type=str, choices=["video", "photo"], default="video", help="Media type to search for")
    args = parser.parse_args()

    search_phrase = args.term
    segment_number = args.segment
    download_count = args.count
    media_type = args.type

    print("=" * 60)
    print("  Envato Elements CloakBrowser Downloader")
    print(f"  Search Term: '{search_phrase}'")
    print(f"  Segment ID:  {segment_number}")
    print("=" * 60)

    # Create segment-specific download directory
    segment_dir = BASE_DOWNLOAD_DIR / segment_number
    segment_dir.mkdir(parents=True, exist_ok=True)

    # Save the raw search term to a text file inside the segment directory
    term_file = segment_dir / "search_term.txt"
    term_file.write_text(search_phrase)
    
    # We use headless=False by default so you can see it working
    downloader = EnvatoElementsDownloader(download_dir=segment_dir, headless=False, media_type=media_type)
    
    try:
        downloader.start()
        downloader.ensure_logged_in()
        
        links = downloader.search_and_get_links(search_phrase, download_count)
        
        if not links:
            print("No items to download.")
            return

        success_count = 0
        failed_links = []
        
        # Initial pass
        for i, link in enumerate(links, 1):
            if downloader.download_item(i, link):
                success_count += 1
            else:
                failed_links.append((i, link))
            human_delay(3, 6) # Delay between items
            
        # Auto-retry logic for failed downloads (up to 3 retries)
        max_retries = 3
        current_retry = 0
        
        while failed_links and current_retry < max_retries:
            current_retry += 1
            print(f"\n🔄 Auto-retry pass {current_retry}/{max_retries} for {len(failed_links)} failed downloads...")
            
            # Copy current failures to process, and clear the main list to collect new failures
            retry_queue = failed_links.copy()
            failed_links.clear()
            
            for i, link in retry_queue:
                if downloader.download_item(i, link):
                    success_count += 1
                else:
                    failed_links.append((i, link))
                human_delay(3, 6)
            
        print("\n" + "=" * 60)
        print(f"  Downloaded: {success_count} / {len(links)}")
        if failed_links:
            print(f"  Permanently failed: {len(failed_links)}")
        print(f"  Saved to:   {segment_dir}")
        print("=" * 60)
            
    finally:
        downloader.stop()


if __name__ == "__main__":
    main()
