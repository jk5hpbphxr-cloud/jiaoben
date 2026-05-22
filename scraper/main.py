import requests
from bs4 import BeautifulSoup

def fetch_page(url: str, timeout: int = 10) -> str:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text

def parse_html(html: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    h1 = soup.find('h1')
    return h1.get_text(strip=True) if h1 else ''

def extract_links(html: str) -> list:
    soup = BeautifulSoup(html, 'html.parser')
    return [a['href'] for a in soup.find_all('a', href=True)]
