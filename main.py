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

async def fetch_all_album_pages(username, max_pages=10):
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

async def fetch_bunkr_gallery_images(username):
    async with aiohttp.ClientSession() as session:
        albums = await get_all_album_links_from_search(username)
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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.93 Safari/537.36",
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

# Erome Albums Endpoint (username only)
@app.get("/api/erome-albums")
async def get_erome_albums(username: str):
    try:
        album_links = await fetch_all_album_pages(username)
        return {"albums": album_links}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Erome Gallery Endpoint (supports URL or username)
@app.get("/api/erome-gallery")
async def get_erome_gallery(query: str):
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
            # If query looks like a URL, treat it as an album URL directly.
            if query.startswith("http"):
                images = await fetch_image_urls(query, session)
            else:
                album_links = await fetch_all_album_pages(query)
                images = await fetch_all_erome_image_urls(album_links)
        return {"images": images}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Bunkr Albums Endpoint (username only)
@app.get("/api/bunkr-albums")
async def get_bunkr_albums(username: str):
    try:
        albums = await get_all_album_links_from_search(username)
        return {"albums": albums}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Bunkr Gallery Endpoint (supports URL or username)
@app.get("/api/bunkr-gallery")
async def get_bunkr_gallery(query: str):
    try:
        async with aiohttp.ClientSession() as session:
            # If query looks like a URL, treat it as an album URL directly.
            if query.startswith("http"):
                image_page_links = await get_image_links_from_album(query, session)
                tasks = [get_image_url_from_link(link, session) for link in image_page_links]
                results = await asyncio.gather(*tasks)
                images = [url for url in results if url is not None and not thumb_pattern.search(url)]
                # Validate the URLs
                validation_tasks = [validate_url(url, session) for url in images]
                validated_results = await asyncio.gather(*validation_tasks)
                images = [url for url in validated_results if url is not None and not thumb_pattern.search(url)]
            else:
                images = await fetch_bunkr_gallery_images(query)
        return {"images": images}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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

# ------------------- Stats Endpoint ------------------- #
@app.get("/api/stats")
async def get_stats():
    online_users = len(active_connections)
    return {"totalVisits": total_visits, "onlineUsers": online_users}

# ------------------- Real WebSocket Logic for Live Stats ------------------- #
async def broadcast_stats():
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
