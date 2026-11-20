"""Fetch candidates from RSS feeds, Google News, web pages and Wikipedia."""
from __future__ import annotations

import base64
import html
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0 Safari/537.36")
TIMEOUT = 20

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"})


class FetchError(Exception):
    pass


def get(url: str, **kw) -> requests.Response:
    try:
        r = session.get(url, timeout=TIMEOUT, allow_redirects=True, **kw)
    except requests.RequestException as e:
        raise FetchError(f"{type(e).__name__}: {e}") from e
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code}")
    return r


def clean(s: str | None) -> str:
    if not s:
        return ""
    s = BeautifulSoup(html.unescape(s), "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", s).strip()


def to_iso(value) -> str | None:
    if not value:
        return None
    if isinstance(value, time.struct_time):
        return datetime(*value[:6], tzinfo=timezone.utc).isoformat()
    s = str(value).strip()
    for parser in (lambda x: parsedate_to_datetime(x), lambda x: datetime.fromisoformat(x.replace("Z", "+00:00"))):
        try:
            d = parser(s)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, IndexError):
            continue
    m = re.search(r"([A-Z][a-z]+) (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})", s)
    if m:
        try:
            d = datetime.strptime(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}", "%b %d %Y")
            return d.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            return None
    return None


def candidate(**kw) -> dict:
    base = {"title": "", "publisher": "", "url": "", "published_at": None, "summary": "", "text": "",
            "via": "", "direct_link": True}
    base.update(kw)
    return base


# ---------------- RSS ----------------

def fetch_rss(url: str, publisher: str, limit: int = 40) -> list[dict]:
    r = get(url)
    feed = feedparser.parse(r.content)
    if feed.bozo and not feed.entries:
        raise FetchError(f"Not a valid feed: {feed.bozo_exception}")
    out = []
    for e in feed.entries[:limit]:
        content = ""
        if e.get("content"):
            content = clean(e.content[0].get("value", ""))
        out.append(candidate(
            title=clean(e.get("title")),
            publisher=publisher,
            url=e.get("link", ""),
            published_at=to_iso(e.get("published_parsed") or e.get("updated_parsed") or e.get("published")),
            summary=clean(e.get("summary", ""))[:1200],
            text=content[:12000],
            via="rss",
        ))
    return out


# ---------------- Google News ----------------

def fetch_gnews(url: str, publisher_hint: str = "", limit: int = 30, decode: bool = True) -> list[dict]:
    r = get(url)
    feed = feedparser.parse(r.content)
    out = []
    for e in feed.entries[:limit]:
        title = clean(e.get("title"))
        src = e.get("source", {}) or {}
        publisher = src.get("title") or publisher_hint
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3]
        out.append(candidate(
            title=title,
            publisher=publisher or "Google News",
            url=e.get("link", ""),
            published_at=to_iso(e.get("published_parsed") or e.get("published")),
            summary="",
            via="google-news",
            direct_link=False,
        ))
    if decode:
        for c in out[:limit]:
            real = decode_google_news_url(c["url"])
            if real:
                c["url"] = real
                c["direct_link"] = True
    return out


def decode_google_news_url(url: str) -> str | None:
    """Resolve a news.google.com/rss/articles/... link to the publisher URL. Best effort."""
    m = re.search(r"/articles/([^?/]+)", url)
    if not m:
        return None
    gid = m.group(1)
    # Older ids embed the URL directly in base64.
    try:
        raw = base64.urlsafe_b64decode(gid + "=" * (-len(gid) % 4))
        hit = re.search(rb"https?://[^\x00-\x20\x7f-\xff]+", raw)
        if hit and b"news.google" not in hit.group(0):
            return hit.group(0).decode("ascii", "ignore")
    except (ValueError, TypeError):
        pass
    # Newer ids need a signed batchexecute call.
    try:
        page = get(f"https://news.google.com/rss/articles/{gid}")
        soup = BeautifulSoup(page.text, "html.parser")
        node = soup.select_one("c-wiz > div[jscontroller]") or soup.find(attrs={"data-n-a-sg": True})
        if not node:
            return None
        sig, ts = node.get("data-n-a-sg"), node.get("data-n-a-ts")
        if not sig or not ts:
            return None
        inner = ["garturlreq",
                 [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
                  "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                 gid, int(ts), sig]
        payload = [[["Fbv4je", json.dumps(inner), None, "generic"]]]
        resp = session.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                            data={"f.req": json.dumps(payload)},
                            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                            timeout=TIMEOUT)
        if resp.status_code >= 400:
            return None
        chunk = resp.text.split("\n\n", 1)[1]
        data = json.loads(chunk)
        decoded = json.loads(data[0][2])[1]
        return decoded if decoded.startswith("http") else None
    except (FetchError, requests.RequestException, ValueError, IndexError, TypeError, KeyError):
        return None


# ---------------- Page extraction ----------------

def fetch_page_links(url: str, publisher: str, link_pattern: str = "", limit: int = 30) -> list[dict]:
    r = get(url)
    soup = BeautifulSoup(r.text, "html.parser")
    pat = re.compile(link_pattern) if link_pattern else None
    host = urlparse(r.url).netloc
    seen: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(r.url, a["href"]).split("#")[0].split("?")[0].rstrip("/")
        if pat and not pat.match(href):
            continue
        if not pat and urlparse(href).netloc != host:
            continue
        text = clean(a.get_text(" "))
        if href in seen:
            if len(text) > len(seen[href]["title"]) and len(text) < 200:
                seen[href]["title"] = text
            continue
        if len(text) < 12:
            heading = a.find(["h1", "h2", "h3", "h4"])
            text = clean(heading.get_text(" ")) if heading else text
        container = a.find_parent(["article", "li", "section", "div"])
        blurb = clean(container.get_text(" "))[:600] if container else ""
        seen[href] = candidate(title=text[:200], publisher=publisher, url=href, summary=blurb, via="page")
        if len(seen) >= limit * 2:
            break
    out = [c for c in seen.values() if len(c["title"]) >= 12][:limit]
    return out


def fetch_article(url: str) -> dict:
    """Fetch an article page and pull its title, publication date and body text."""
    r = get(url)
    soup = BeautifulSoup(r.text, "html.parser")

    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n}) \
                or soup.find("meta", attrs={"itemprop": n})
            if tag and tag.get("content"):
                return tag["content"]
        return None

    published = meta("article:published_time", "datePublished", "pubdate", "date", "publish-date")
    if not published:
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(s.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            items = data if isinstance(data, list) else [data]
            for it in items:
                if isinstance(it, dict) and it.get("datePublished"):
                    published = it["datePublished"]
                    break
            if published:
                break
    if not published:
        t = soup.find("time")
        if t:
            published = t.get("datetime") or t.get_text(" ")
    if not published:
        head = soup.find(["h1"])
        nearby = head.find_next(string=re.compile(r"[A-Z][a-z]+ \d{1,2}(st|nd|rd|th)?, \d{4}")) if head else None
        published = str(nearby) if nearby else None
    for bad in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        bad.decompose()
    body = soup.find("article") or soup.find("main") or soup.body or soup
    paras = [clean(p.get_text(" ")) for p in body.find_all(["p", "h2", "li"])]
    text = "\n".join(p for p in paras if len(p) > 40)
    return {
        "url": r.url,
        "title": clean(meta("og:title") or (soup.title.string if soup.title else "")),
        "published_at": to_iso(published),
        "description": clean(meta("og:description", "description")),
        "image": meta("og:image"),
        "text": text[:20000],
    }


# ---------------- Wikipedia ----------------

def fetch_wikipedia(query: str, limit: int = 2) -> list[dict]:
    r = get("https://en.wikipedia.org/w/api.php", params={
        "action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": limit})
    hits = r.json().get("query", {}).get("search", [])
    out = []
    for h in hits:
        title = h["title"]
        s = get("https://en.wikipedia.org/api/rest_v1/page/summary/" + quote_plus(title.replace(" ", "_")))
        d = s.json()
        out.append(candidate(title=title, publisher="Wikipedia",
                             url=d.get("content_urls", {}).get("desktop", {}).get("page", ""),
                             summary=d.get("extract", "")[:1500], text=d.get("extract", ""), via="wikipedia"))
    return out


def check_link(url: str) -> tuple[str, str]:
    """Return (status, detail): ok | blocked (bot wall, assumed fine) | broken."""
    try:
        r = session.head(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code in (405, 404, 403) or r.status_code >= 500:
            r = session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
            r.close()
    except requests.RequestException as e:
        return "broken", type(e).__name__
    if r.status_code < 400:
        return "ok", str(r.status_code)
    if r.status_code in (401, 403, 429):
        return "blocked", f"HTTP {r.status_code} (publisher blocks automated checks)"
    return "broken", f"HTTP {r.status_code}"
