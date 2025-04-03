import os
import asyncio
import aiohttp
import re
import urllib.parse
import uvicorn
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI()

# Allow CORS from your front-end (adjust as needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Use specific domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files (if you have any CSS, JS, etc.)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Set up Jinja2 templates to load HTML files from the "templates" directory
templates = Jinja2Templates(directory="templates")

# ------------------- Root Endpoint to Serve Your HTML ------------------- #

@app.get("/")
async def read_index(request: Request):
    return templates.TemplateResponse("ok.html", {"request": request})


# ------------------- Helper Functions ------------------- #

def parse_links_and_titles(page_content, pattern, title_class):
    """
    Parses the page content to extract links that match a given regex pattern
    and extracts the text of <span> elements with the specified title_class.
    """
    soup = BeautifulSoup(page_content, 'html.parser')
    # Extract links that match the given pattern.
    links = [a['href'] for a in soup.find_all('a', href=True) if re.match(pattern, a['href'])]
    # Extract titles from spans with the provided CSS class.
    titles = [span.get_text() for span in soup.find_all('span', class_=title_class)]
    return links, titles

async def get_webpage_content(url, session):
    async with session.get(url, allow_redirects=True) as response:
        text = await response.text()
        return text, str(response.url), response.status

# ------------------- Erome Functions (unchanged) ------------------- #

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

# ------------------- Updated Bunkr Functions ------------------- #

async def get_album_links_from_search(username, page=1):
    """
    For a given username and page number, fetches the search result page
    from bunkr-albums.io and extracts both album links and their titles.
    """
    search_url = f"https://bunkr-albums.io/?search={urllib.parse.quote(username)}&page={page}"
    async with aiohttp.ClientSession() as session:
        async with session.get(search_url) as response:
            if response.status != 200:
                return []
            text = await response.text()
    # Define the regex pattern for bunkr album URLs and the CSS class for titles.
    pattern = r"^https://bunkr\.cr/a/.*"
    title_class = "album-title"  # Adjust this class name as needed
    links, titles = parse_links_and_titles(text, pattern, title_class)
    # Create a list of dictionaries combining link and title.
    albums = []
    for url, title in zip(links, titles):
        albums.append({"url": url, "title": title})
    return albums

async def get_all_album_links_from_search(username):
    """
    Loops through pages until no further albums are found and returns
    a list of album dictionaries (each with 'url' and 'title').
    """
    all_albums = []
    page = 1
    while True:
        albums = await get_album_links_from_search(username, page)
        if not albums:
            break
        all_albums.extend(albums)
        # Check for the existence of a next page link in the HTML
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
        image_url = img_tag.get('src')
        return image_url
    return None

async def fetch_bunkr_gallery_images(username):
    async with aiohttp.ClientSession() as session:
        albums = await get_all_album_links_from_search(username)
        tasks = []
        for album in albums:
            img_page_links = await get_image_links_from_album(album["url"], session)
            for link in img_page_links:
                tasks.append(get_image_url_from_link(link, session))
        results = await asyncio.gather(*tasks)
        image_urls = [url for url in results if url is not None]
        return list({url for url in image_urls if "/thumb/" not in url})

# ------------------- Fapello and JPG5 Functions (unchanged) ------------------- #

async def fetch_fapello_page_media(page_url, session, username):
    async with session.get(page_url) as response:
        if response.status != 200:
            return {"images": [], "videos": []}
        text = await response.text()
    soup = BeautifulSoup(text, 'html.parser')
    page_images = [
        img['src'] for img in soup.find_all('img', src=True)
        if img['src'].startswith("https://fapello.com/content/") and f"/{username}/" in img['src']
    ]
    page_videos = [
        vid['src'] for vid in soup.find_all('source', type="video/mp4", src=True)
        if vid['src'].startswith("https://") and f"/{username}/" in vid['src']
    ]
    return {"images": page_images, "videos": page_videos}

async def fetch_fapello_album_media(album_url):
    media = {"images": [], "videos": []}
    url_parts = album_url.split("/")
    username = url_parts[0] if url_parts else ""
    if not username:
        return media
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
        text, base_url, status = await get_webpage_content(album_url, session)
        if status != 200:
            return media
        soup = BeautifulSoup(text, 'html.parser')
        links = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            if href.startswith(album_url) and re.search(r'/\d+/?$', href):
                links.append(href)
        links = list(set(links))
        if not links:
            links = [album_url]
        tasks = [fetch_fapello_page_media(link, session, username) for link in links]
        results = await asyncio.gather(*tasks)
        for result in results:
            if isinstance(result, dict):
                media["images"].extend(result.get("images", []))
                media["videos"].extend(result.get("videos", []))
        media["images"] = list(set(media["images"]))
        media["videos"] = list(set(media["videos"]))
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

@app.get("/api/erome-albums")
async def get_erome_albums(username: str):
    try:
        album_links = await fetch_all_album_pages(username)
        return {"albums": album_links}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/erome-gallery")
async def get_erome_gallery(username: str):
    try:
        album_links = await fetch_all_album_pages(username)
        image_urls = await fetch_all_erome_image_urls(album_links)
        return {"images": image_urls}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/bunkr-albums")
async def get_bunkr_albums(username: str):
    try:
        albums = await get_all_album_links_from_search(username)
        return {"albums": albums}  # Each album is a dict with 'url' and 'title'
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/bunkr-gallery")
async def get_bunkr_gallery(username: str):
    try:
        images = await fetch_bunkr_gallery_images(username)
        return {"images": images}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/fapello-gallery")
async def get_fapello_gallery(album_url: str):
    try:
        media = await fetch_fapello_album_media(album_url)
        return {"images": media.get("images", []), "videos": media.get("videos", [])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/jpg5-gallery")
async def get_jpg5_gallery(album_url: str):
    try:
        images = await extract_jpg5_album_media_urls(album_url)
        return {"images": images}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def start():
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

if __name__ == "__main__":
    start()
