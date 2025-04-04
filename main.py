import os
import asyncio
import aiohttp
import re
import json
import urllib.parse
import uvicorn
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

app = FastAPI()

# Global stats variable for total visits
total_visits = 0

# For tracking active WebSocket connections (used to determine online users)
active_connections = set()

# ------------------- Stats Middleware ------------------- #
class StatsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        global total_visits
        total_visits += 1
        response = await call_next(request)
        return response

app.add_middleware(StatsMiddleware)

# ------------------- CORS and Static Files ------------------- #
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, use a specific domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ------------------- Root Endpoint ------------------- #
@app.get("/")
async def read_index(request: Request):
    return templates.TemplateResponse("ok.html", {"request": request})

# ------------------- Helper Functions ------------------- #
def is_url(identifier: str) -> bool:
    return identifier.startswith("http")

def parse_links_and_titles(page_content, pattern, title_class):
    soup = BeautifulSoup(page_content, 'html.parser')
    links = [a['href'] for a in soup.find_all('a', href=True) if re.match(pattern, a['href'])]
    titles = [span.get_text() for span in soup.find_all('span', class_=title_class)]
    return links, titles

async def get_webpage_content(url, session):
    async with session.get(url, allow_redirects=True) as response:
        text = await response.text()
        return text, str(response.url), response.status

# ------------------- Erome Functions ------------------- #
def extract_album_links(page_content):
    soup = BeautifulSoup(page_content, 'html.parser')
    links = set()
    for a_tag in soup.find_all('a', class_='album-link'):
        href = a_tag.get('href')
        if href and href.startswith("https://www.erome.com/a/"):
            links.add(href)
    return list(links)

async def fetch_all_album_pages_by_username(username, max_pages=10):
    all_links = set()
    async with aiohttp.ClientSession() as session:
        for page in range(1, max_pages + 1):
            search_url = f"https://www.erome.com/search?q={urllib.parse.quote(username)}&page={page}"
            text, _, status = await get_webpage_content(search_url, session)
            if status != 200 or not text:
                break
            links = extract_album_links(text)
            for link in links:
                all_links.add(link)
    return list(all_links)

async def fetch_album_page_from_url(url: str):
    async with aiohttp.ClientSession() as session:
        text, _, status = await get_webpage_content(url, session)
        if status != 200:
            return []
        return extract_album_links(text)

async def fetch_image_urls(album_url, session):
    page_content, base_url, _ = await get_webpage_content(album_url, session)
    soup = BeautifulSoup(page_content, 'html.parser')
    images = [
        urljoin(base_url, img['data-src'])
        for img in soup.find_all('div', class_='img')
        if img.get('data-src')
    ]
    return images

async def fetch_all_erome_image_urls(album_urls):
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
        tasks = [fetch_image_urls(url, session) for url in album_urls]
        all_images = await asyncio.gather(*tasks, return_exceptions=True)
        all_image_urls = [img_url for images in all_images if isinstance(images, list) for img_url in images]
        return list({url for url in all_image_urls if "/thumb/" not in url})

# ------------------- Bunkr Functions ------------------- #
async def get_album_links_from_search(username, page=1):
    search_url = f"https://bunkr-albums.io/?search={urllib.parse.quote(username)}&page={page}"
    async with aiohttp.ClientSession() as session:
        async with session.get(search_url) as response:
            if response.status != 200:
                return []
            text = await response.text()
    pattern = r"^https://bunkr\.cr/a/.*"
    title_class = "album-title"  # Adjust as needed
    links, titles = parse_links_and_titles(text, pattern, title_class)
    albums = []
    for url, title in zip(links, titles):
        albums.append({"url": url, "title": title})
    return albums

async def get_all_album_links_from_search(username):
    all_albums = []
    page = 1
    while True:
        albums = await get_album_links_from_search(username, page)
        if not albums:
            break
        all_albums.extend(albums)
        async with aiohttp.ClientSession() as session:
            search_url = f"https://bunkr-albums.io/?search={urllib.parse.quote(username)}&page={page}"
            text, _, _ = await get_webpage_content(search_url, session)
        soup = BeautifulSoup(text, 'html.parser')
        next_page = soup.find('a', href=re.compile(rf"\?search={re.escape(username)}&page={page+1}"), class_="btn btn-sm btn-main")
        if not next_page:
            break
        page += 1
    return all_albums

async def get_album_info_from_url(url: str):
    # Directly fetch the page and parse album info
    async with aiohttp.ClientSession() as session:
        text, _, status = await get_webpage_content(url, session)
        if status != 200:
            return None
        # Using the same parsing as search
        pattern = r"^https://bunkr\.cr/a/.*"
        title_class = "album-title"  # Adjust as needed
        links, titles = parse_links_and_titles(text, pattern, title_class)
        if links and titles:
            return {"url": url, "title": titles[0]}
        else:
            return {"url": url, "title": "Unknown Title"}

async def get_image_links_from_album(album_url, session):
    async with session.get(album_url) as response:
        if response.status != 200:
            return []
        text = await response.text()
    soup = BeautifulSoup(text, 'html.parser')
    links = []
    for link in soup.find_all('a', attrs={'aria-label': 'download'}, href=True):
        href = link.get('href')
        if href.startswith("/f/"):
            full_link = "https://bunkr.cr" + href
            links.append(full_link)
        elif href.startswith("https://bunkr.cr/f/"):
            links.append(href)
    return links

async def get_image_url_from_link(link, session):
    async with session.get(link) as response:
        if response.status != 200:
            return None
        text = await response.text()
    soup = BeautifulSoup(text, 'html.parser')
    img_tag = soup.find('img', class_=lambda x: x and "object-cover" in x)
    if img_tag:
        return img_tag.get('src')
    return None

async def validate_url(url, session):
    try:
        headers = {"Range": "bytes=0-0"}
        async with session.get(url, headers=headers, allow_redirects=True) as response:
            if response.status == 200:
                return url
    except Exception:
        pass
    return None

thumb_pattern = re.compile(r"/thumb/")

async def fetch_bunkr_gallery_images_from_albums(albums):
    async with aiohttp.ClientSession() as session:
        tasks = []
        for album in albums:
            img_page_links = await get_image_links_from_album(album["url"], session)
            for link in img_page_links:
                tasks.append(get_image_url_from_link(link, session))
        results = await asyncio.gather(*tasks)
        image_urls = [url for url in results if url is not None and not thumb_pattern.search(url)]
        validation_tasks = [validate_url(url, session) for url in image_urls]
        validated_results = await asyncio.gather(*validation_tasks)
        validated_image_urls = [url for url in validated_results if url is not None and not thumb_pattern.search(url)]
        return list({url for url in validated_image_urls})

# ------------------- Fapello and JPG5 Functions ------------------- #
async def fetch_fapello_page_media(page_url: str, session: aiohttp.ClientSession, username: str) -> dict:
    try:
        content, base_url, status = await get_webpage_content(page_url, session)
        if status != 200:
            print(f"[DEBUG] Failed to fetch {page_url} with status {status}")
            return {"images": [], "videos": []}
        soup = BeautifulSoup(content, 'html.parser')
        image_tags = soup.find_all('img')
        page_images = []
        for img in image_tags:
            img_url = img.get('src') or img.get('data-src')
            if img_url and img_url.startswith("https://fapello.com/content/") and f"/{username}/" in img_url:
                page_images.append(img_url)
        video_tags = soup.find_all('source', type="video/mp4", src=True)
        page_videos = [
            vid['src']
            for vid in video_tags
            if vid['src'].startswith("https://") and f"/{username}/" in vid['src']
        ]
        print(f"[DEBUG] {page_url}: Found {len(page_images)} images and {len(page_videos)} videos for user {username}")
        return {"images": page_images, "videos": page_videos}
    except Exception as e:
        print(f"[DEBUG] Exception in fetching {page_url}: {e}")
        return {"images": [], "videos": []}

async def fetch_fapello_album_media(album_url: str) -> dict:
    media = {"images": [], "videos": []}
    parsed = urllib.parse.urlparse(album_url)
    path_parts = parsed.path.strip("/").split("/")
    if not path_parts or not path_parts[0]:
        print("[DEBUG] Could not extract username from album URL.")
        return media
    username = path_parts[0]
    async with aiohttp.ClientSession(
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": album_url
        }
    ) as session:
        content, base_url, status = await get_webpage_content(album_url, session)
        if status != 200:
            print(f"[DEBUG] Failed to fetch main album page: {album_url} (status {status})")
            return media
        soup = BeautifulSoup(content, 'html.parser')
        links = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            full_href = urllib.parse.urljoin(base_url, href)
            if full_href.startswith(album_url) and re.search(r'/\d+/?$', full_href):
                links.append(full_href)
        links = list(set(links))
        print(f"[DEBUG] Found {len(links)} album pages from main page {album_url}")
        if not links:
            links = [album_url]
        tasks = [fetch_fapello_page_media(link, session, username) for link in links]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, dict):
                media["images"].extend(result.get("images", []))
                media["videos"].extend(result.get("videos", []))
        media["images"] = list(set(media["images"]))
        media["videos"] = list(set(media["videos"]))
        print(f"[DEBUG] Total media collected for {username}: {len(media['images'])} images and {len(media['videos'])} videos")
        return media

async def extract_jpg5_album_media_urls(album_url):
    media_urls = set()
    next_page_url = album_url.rstrip('/')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        while next_page_url:
            async with session.get(next_page_url) as response:
                if response.status != 200:
                    break
                content = await response.text()
            soup = BeautifulSoup(content, 'html.parser')
            imgs = soup.find_all('img', src=True)
            current_page_media = {img['src'] for img in imgs if "jpg5.su" in img['src']}
            if not current_page_media:
                break
            before = len(media_urls)
            media_urls.update(current_page_media)
            if len(media_urls) == before:
                break
            next_link = soup.find("a", {"data-pagination": "next"})
            if next_link and "href" in next_link.attrs:
                next_page_url = next_link["href"]
                if not next_page_url.startswith("http"):
                    next_page_url = f"https://jpg5.su{next_page_url}"
            else:
                break
    return list(media_urls)

# ------------------- API Endpoints ------------------- #

# --- Erome Endpoints ---
@app.get("/api/erome-albums")
async def get_erome_albums(identifier: str):
    """
    Accepts either a username or an album URL.
      - If a URL (and contains "erome.com"), fetches the album links directly from that page.
      - Otherwise, uses the username to perform a search.
    """
    try:
        if is_url(identifier) and "erome.com" in identifier:
            album_links = await fetch_album_page_from_url(identifier)
        else:
            album_links = await fetch_all_album_pages_by_username(identifier)
        return {"albums": album_links}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/erome-gallery")
async def get_erome_gallery(identifier: str):
    """
    Accepts either a username or an album URL.
      - If a URL (and contains "erome.com"), fetches images from that album page.
      - Otherwise, uses the username to search for albums and then fetches all gallery images.
    """
    try:
        if is_url(identifier) and "erome.com" in identifier:
            # Assume direct album URL; wrap into a list
            album_urls = [identifier]
        else:
            album_urls = await fetch_all_album_pages_by_username(identifier)
        image_urls = await fetch_all_erome_image_urls(album_urls)
        return {"images": image_urls}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- Bunkr Endpoints ---
@app.get("/api/bunkr-albums")
async def get_bunkr_albums(identifier: str):
    """
    Accepts either a username or a direct album URL.
      - If a URL (and contains "bunkr"), returns album info based on that URL.
      - Otherwise, performs a search using the username.
    """
    try:
        if is_url(identifier) and "bunkr" in identifier:
            album_info = await get_album_info_from_url(identifier)
            albums = [album_info] if album_info else []
        else:
            albums = await get_all_album_links_from_search(identifier)
        return {"albums": albums}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/bunkr-gallery")
async def get_bunkr_gallery(identifier: str):
    """
    Accepts either a username or a direct album URL.
      - If a URL (and contains "bunkr"), fetches gallery images from that album.
      - Otherwise, searches for albums using the username and aggregates images from all found albums.
    """
    try:
        if is_url(identifier) and "bunkr" in identifier:
            # For a direct URL, build a list with a single album info dictionary
            albums = [{"url": identifier, "title": "Direct Album"}]
        else:
            albums = await get_all_album_links_from_search(identifier)
        images = await fetch_bunkr_gallery_images_from_albums(albums)
        return {"images": images}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- Fapello and JPG5 Endpoints remain unchanged --- #
@app.get("/api/fapello-gallery")
async def get_fapello_gallery(album_url: str):
    if "fapello.com" not in album_url:
        raise HTTPException(status_code=400, detail="Invalid album URL provided.")
    try:
        media = await fetch_fapello_album_media(album_url)
        return {"images": media.get("images", []), "videos": media.get("videos", [])}
    except Exception as e:
        print(f"[DEBUG] Error in fapello-gallery: {e}")
        raise HTTPException(status_code=500, detail="Error fetching Fapello media: " + str(e))

@app.get("/api/jpg5-gallery")
async def get_jpg5_gallery(album_url: str):
    try:
        images = await extract_jpg5_album_media_urls(album_url)
        return {"images": images}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- Stats Endpoint --- #
@app.get("/api/stats")
async def get_stats():
    online_users = len(active_connections)  # Count of active WebSocket connections
    return {"totalVisits": total_visits, "onlineUsers": online_users}

# ------------------- Real WebSocket Logic for Live Stats ------------------- #
async def broadcast_stats():
    """Broadcast the current stats to all connected WebSocket clients."""
    stats = {"totalVisits": total_visits, "onlineUsers": len(active_connections)}
    message = json.dumps(stats)
    for connection in list(active_connections):
        try:
            await connection.send_text(message)
        except Exception:
            active_connections.discard(connection)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)
    await broadcast_stats()
    try:
        while True:
            data = await websocket.receive_text()
            await broadcast_stats()
    except WebSocketDisconnect:
        active_connections.remove(websocket)
        await broadcast_stats()

def start():
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

if __name__ == "__main__":
    start()
