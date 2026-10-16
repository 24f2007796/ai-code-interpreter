
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urldefrag
from collections import deque
from pathlib import PurePosixPath
import time

start_url = "https://sanand0.github.io/tdsdata/crawl_html/"
prefix = start_url

# Pages waiting to be visited
queue = deque([start_url])

# Pages we have already visited
visited = set()

# HTML files discovered on the website
html_files = set()

session = requests.Session()
session.headers.update({"User-Agent": "TDS-Educational-Crawler/1.0"})

while queue:
    current_url = queue.popleft()

    # Remove fragments such as #section
    current_url = urldefrag(current_url)[0]

    if current_url in visited:
        continue

    visited.add(current_url)

    try:
        response = session.get(current_url, timeout=20)
        response.raise_for_status()

        # Only inspect HTML pages
        content_type = response.headers.get("Content-Type", "")
        if "text/html" not in content_type.lower():
            continue

        soup = BeautifulSoup(response.text, "html.parser")

        for link in soup.find_all("a", href=True):
            next_url = urldefrag(urljoin(current_url, link["href"]))[0]
            parsed = urlparse(next_url)

            # Stay inside the assignment website's folder
            if not next_url.startswith(prefix):
                continue

            if parsed.scheme not in ("http", "https"):
                continue

            filename = PurePosixPath(parsed.path).name

            if filename.lower().endswith((".html", ".htm")):
                html_files.add(next_url)

            if next_url not in visited:
                queue.append(next_url)

        if len(visited) % 25 == 0:
            print("Pages visited:", len(visited))

        time.sleep(0.1)

    except requests.RequestException as error:
        print("Could not open:", current_url, "-", error)

# Count files whose names begin with J through W
matching_files = sorted(
    url for url in html_files
    if "j" <= PurePosixPath(urlparse(url).path).name[0].lower() <= "w"
)

print("\nCrawling finished!")
print("Pages visited:", len(visited))
print("HTML files discovered:", len(html_files))
print("HTML files beginning with J through W:", len(matching_files))

print("\nMatching filenames:")
for url in matching_files:
    print(PurePosixPath(urlparse(url).path).name)
