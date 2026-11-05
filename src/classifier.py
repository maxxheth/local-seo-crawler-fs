"""Post-crawl page classification (page type, clinic detection, location status).

Runs after the crawl because it needs the whole page set and link graph:
directory hubs are recognised from body-placement links, clinic pages from the
hubs' targets plus DOM signals, and location status from (in priority order) an
authoritative locations file, directory labels, and on-page notices limited to
the page's opening section.

Nothing here is site-specific: all thresholds, token lists and status phrases are
configurable through ClassifierConfig.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import dataclass, field
from datetime import date
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

from src.adapters.competitor import ADDRESS_REGEX, US_STATE_NAMES

PAGE_TYPES = (
    "clinic_location",
    "clinic_service",
    "directory_hub",
    "utility_page",
    "service_page",
    "blog_post",
    "blog_archive",
    "other",
)

LOCATION_STATUSES = (
    "open",
    "coming_soon",
    "temporarily_closed",
    "moved",
    "ownership_transferred",
    "closed",
    "unknown",
)

DEFAULT_UTILITY_TOKENS = (
    "thank-you",
    "thankyou",
    "confirmation",
    "submitted",
    "cart",
    "checkout",
    "order-received",
    "success",
    "form-received",
    "quote-received",
)

# Utility tokens that indicate a conversion confirmation page (subject to the
# "Indexable Thank-You / Confirmation Page" rule). Cart/checkout are utility pages
# but not confirmations.
CONVERSION_UTILITY_TOKENS = (
    "thank-you",
    "thankyou",
    "confirmation",
    "submitted",
    "order-received",
    "success",
    "form-received",
    "quote-received",
)

# Prototype/staging slug words. Tokens match whole hyphen-delimited words of a path
# segment; "^tok" must start the segment, "tok$" must end it (ignoring a trailing
# number, e.g. "home-copy-2"), "^tok$" must be the whole segment. Common words that
# also start real slugs ("new-braunfels", "new-patients", "old-town-spa", "covid-test")
# are therefore only matched as suffixes ("careers-old", "memberships-new").
DEFAULT_PROTOTYPE_TOKENS = (
    "demo",
    "staging",
    "prototype",
    "sandbox",
    "draft",
    "backup",
    "temp",
    "test-page",
    "test-site",
    "^test$",
    "old$",
    "new$",
    "copy$",
)

DEFAULT_STATUS_MARKERS: dict[str, tuple[str, ...]] = {
    "temporarily_closed": (
        "temporarily closed",
        "temporary closure",
        "closed temporarily",
    ),
    "closed": (
        "permanently closed",
        "closed permanently",
        "this location is closed",
        "this location has closed",
    ),
    "coming_soon": (
        "coming soon",
        "grand opening",
        "opening soon",
    ),
    "moved": (
        "has moved",
        "have moved",
        "we've moved",
        "we’ve moved",
        "we have moved",
        "relocated",
    ),
    "ownership_transferred": (
        "under new ownership",
        "now owned by",
        "new ownership",
    ),
}

LOCAL_SCHEMA_TYPES = (
    "LocalBusiness",
    "MedicalBusiness",
    "HealthAndBeautyBusiness",
    "DaySpa",
    "BeautySalon",
    "MedicalClinic",
    "MedicalSpa",
    "Physician",
    "Store",
    "ProfessionalService",
)

# Types assigned before hub detection; such pages are never directory entries or clinics.
_NON_CANDIDATE_TYPES = ("utility_page", "blog_post", "blog_archive", "service_page")

_BLOG_ARCHIVE_SEGMENTS = {"category", "tag", "author", "page", "archive", "archives"}
_BLOG_SCHEMA_TYPES = {"BlogPosting", "NewsArticle"}
_CITY_STATE_PATTERN = re.compile(r"\bin\s+[A-Z][A-Za-z .'-]+,\s*(?:[A-Z]{2}\b|[A-Z][a-z]+)")
_BOOKING_PATTERN = re.compile(r"\b(book|appointment|schedule|reserve)\b", re.IGNORECASE)

_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for m in names
}
_MONTH_ALT = "|".join(sorted((m.capitalize() for m in _MONTHS), key=len, reverse=True))
# Explicit date: "October 15, 2026", "October 2026", "Oct. 15"
_DATE_EXPLICIT = re.compile(
    rf"\b(?P<month>{_MONTH_ALT})\.?\s+(?:(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<year1>\d{{4}}))?|(?P<year2>\d{{4}}))\b"
)
# Month-only phrase introduced by a time preposition: "reopening in October"
_DATE_MONTH_ONLY = re.compile(
    rf"\b(?:in|by|until|through|on|after|before|from|since|starting|beginning|early|mid|late|end of)\s+(?P<month>{_MONTH_ALT})\b(?!\.?\s+\d)"
)
_DATE_NUMERIC = re.compile(r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{2}|\d{4})\b")

_ADDR_SUFFIX_MAP = {
    "street": "st", "avenue": "ave", "road": "rd", "boulevard": "blvd", "drive": "dr",
    "lane": "ln", "trail": "trl", "court": "ct", "circle": "cir", "parkway": "pkwy",
    "highway": "hwy", "place": "pl", "plaza": "plz", "square": "sq", "terrace": "ter",
    "center": "ctr", "crossing": "xing", "freeway": "fwy", "expressway": "expy",
}


@dataclass
class ClassifierConfig:
    utility_tokens: tuple[str, ...] = DEFAULT_UTILITY_TOKENS
    prototype_tokens: tuple[str, ...] = DEFAULT_PROTOTYPE_TOKENS
    prototype_allowlist: tuple[str, ...] = ()
    hub_min_links: int = 10
    directory_urls: tuple[str, ...] = ()
    locations_file: str = ""
    corporate_share: float = 0.40
    opening_chars: int = 600
    clinic_min_confidence: float = 0.5
    status_markers: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_STATUS_MARKERS))
    blog_prefixes: tuple[str, ...] = ("blog", "news", "articles", "posts")
    service_prefixes: tuple[str, ...] = ("services", "service", "treatments", "treatment")
    crawl_date: date | None = None


# ── URL helpers ──────────────────────────────────────────────────────────────

def url_key(url: str) -> str:
    """Trailing-slash-insensitive key used to join pages and link targets."""
    return (url or "").rstrip("/")


def path_segments(url: str) -> list[str]:
    return [s for s in urlparse(url).path.lower().split("/") if s]


def _parse_token(tok: str) -> tuple[list[str], bool, bool]:
    t = tok.strip().lower()
    start = t.startswith("^")
    end = t.endswith("$")
    t = t.strip("^$")
    return t.split("-"), start, end


def token_in_segments(url: str, tokens: tuple[str, ...]) -> tuple[str, str] | None:
    """Match tokens against whole hyphen-delimited words of each path segment.

    "test-page" matches /map-test-page/ but not /reviews-and-testimonials/; multi-word
    tokens match contiguous word sequences. Anchors: "^tok" must start the segment,
    "tok$" must end it (a trailing all-digit word is ignored), "^tok$" is the whole segment.
    Returns (token, segment) for the first match, or None.
    """
    parsed = [(tok, *_parse_token(tok)) for tok in tokens if tok.strip("^$ ")]
    for seg in path_segments(url):
        words = seg.split("-")
        trimmed = list(words)
        while len(trimmed) > 1 and trimmed[-1].isdigit():
            trimmed.pop()
        for tok, tok_words, start, end in parsed:
            n = len(tok_words)
            if start and end:
                if trimmed == tok_words:
                    return tok, seg
            elif start:
                if words[:n] == tok_words:
                    return tok, seg
            elif end:
                if len(trimmed) >= n and trimmed[-n:] == tok_words:
                    return tok, seg
            else:
                for i in range(len(words) - n + 1):
                    if words[i : i + n] == tok_words:
                        return tok, seg
    return None


def is_prototype_url(url: str, tokens: tuple[str, ...], allowlist: tuple[str, ...] = ()) -> tuple[str, str] | None:
    """Prototype/staging token hit for url, unless the URL is allowlisted."""
    if allowlist and url_key(url) in {url_key(a) for a in allowlist}:
        return None
    return token_in_segments(url, tokens)


def is_noindex(page: dict[str, Any]) -> bool:
    return "noindex" in (page.get("meta_robots") or "").lower() or "noindex" in (
        page.get("x_robots_tag") or ""
    ).lower()


def is_indexable(page: dict[str, Any]) -> bool:
    return int(page.get("status_code") or 0) == 200 and not is_noindex(page)


# ── Address helpers ──────────────────────────────────────────────────────────

def normalize_address(addr: str) -> str:
    text = (addr or "").lower()
    text = re.sub(r"\bunited states( of america)?\b|\busa\b", " ", text)
    for full, abbr in sorted(US_STATE_NAMES.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(rf"\b{full}\b", abbr.lower(), text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [_ADDR_SUFFIX_MAP.get(w, w) for w in text.split()]
    words = [w for w in words if w not in ("suite", "ste", "unit", "apt")]
    return " ".join(words)


def address_key(addr: str) -> str:
    """Stable comparison key: house number + first street word + ZIP5.

    "3989 Boston Street Baltimore, MD 21224" and "3989 Boston Street, Baltimore, MD, 21224,
    United States" both map to "3989|boston|21224".
    """
    norm = normalize_address(addr)
    m = re.match(r"^(\d+[a-z]?)\s+(.*)$", norm)
    zip_m = re.findall(r"\b(\d{5})(?:\s+\d{4})?\b", norm)
    if not m or not zip_m:
        return ""
    rest = [w for w in m.group(2).split() if w not in ("n", "s", "e", "w")]
    first_word = rest[0] if rest else ""
    return f"{m.group(1)}|{first_word}|{zip_m[-1]}"


def page_addresses(page: dict[str, Any]) -> list[str]:
    """Addresses displayed on the page (NAP extraction plus the opening section)."""
    found = list((page.get("nap_found") or {}).get("addresses") or [])
    for m in ADDRESS_REGEX.finditer(page.get("opening_text") or ""):
        found.append(" ".join(m.group(0).split()))
    return found


def page_address_keys(page: dict[str, Any], corporate_keys: set[str] | None = None) -> set[str]:
    keys = {address_key(a) for a in page_addresses(page)}
    keys.discard("")
    if corporate_keys:
        keys -= corporate_keys
    return keys


def corporate_values(pages: list[dict[str, Any]], share: float) -> tuple[set[str], set[str]]:
    """Phones / address keys that appear on at least `share` of 200 pages (site-wide chrome)."""
    ok_pages = [p for p in pages if int(p.get("status_code") or 0) == 200]
    total = max(1, len(ok_pages))
    phone_counts: Counter[str] = Counter()
    addr_counts: Counter[str] = Counter()
    for p in ok_pages:
        nap = p.get("nap_found") or {}
        phone_counts.update(set(nap.get("phones") or []))
        addr_counts.update({address_key(a) for a in (nap.get("addresses") or [])} - {""})
    corp_phones = {ph for ph, c in phone_counts.items() if c / total >= share and c > 1}
    corp_addrs = {a for a, c in addr_counts.items() if c / total >= share and c > 1}
    return corp_phones, corp_addrs


# ── Status helpers ───────────────────────────────────────────────────────────

def find_status_markers(text: str, markers: dict[str, tuple[str, ...]]) -> list[tuple[int, str, str]]:
    """All (position, status, phrase) marker hits in text, sorted by position."""
    low = (text or "").lower().replace("’", "'")
    hits: list[tuple[int, str, str]] = []
    for status, phrases in markers.items():
        for phrase in phrases:
            ph = phrase.lower().replace("’", "'")
            for m in re.finditer(rf"\b{re.escape(ph)}\b", low):
                hits.append((m.start(), status, phrase))
    hits.sort(key=lambda h: (h[0], h[1]))
    return hits


def first_status(text: str, markers: dict[str, tuple[str, ...]]) -> tuple[str, str, int] | None:
    hits = find_status_markers(text, markers)
    if not hits:
        return None
    pos, status, phrase = hits[0]
    return status, phrase, pos


def label_status(label: str, markers: dict[str, tuple[str, ...]]) -> tuple[str, str] | None:
    """Status from a directory label ("Clinic X - Temporarily Closed", "Clinic Y - Closed")."""
    hit = first_status(label, markers)
    if hit:
        return hit[0], hit[1]
    if re.search(r"(?<!temporarily )\bclosed\b", (label or "").lower()):
        return "closed", "closed"
    return None


def find_stale_dates(text: str, crawl_date: date) -> list[str]:
    """Date phrases in a status notice that are already in the past at crawl time.

    Month-only phrases ("reopening in October") are read as the most recent such month
    on or before the crawl date: a month 1–6 months back is stale, the current month is
    not, and anything further back is assumed to refer to the coming year.
    """
    stale: list[str] = []
    for m in _DATE_EXPLICIT.finditer(text or ""):
        month = _MONTHS[m.group("month").lower()]
        year_s = m.group("year1") or m.group("year2")
        day = int(m.group("day")) if m.group("day") else None
        if year_s:
            year = int(year_s)
            if day:
                try:
                    d = date(year, month, day)
                except ValueError:
                    continue
                if d < crawl_date:
                    stale.append(m.group(0))
            elif (year, month) < (crawl_date.year, crawl_date.month):
                stale.append(m.group(0))
        elif day:
            delta = (crawl_date.month - month) % 12
            if 1 <= delta <= 6 or (delta == 0 and day < crawl_date.day):
                stale.append(m.group(0))
    for m in _DATE_MONTH_ONLY.finditer(text or ""):
        month = _MONTHS[m.group("month").lower()]
        delta = (crawl_date.month - month) % 12
        if 1 <= delta <= 6:
            stale.append(m.group(0))
    for m in _DATE_NUMERIC.finditer(text or ""):
        y = int(m.group("y"))
        y = y + 2000 if y < 100 else y
        try:
            d = date(y, int(m.group("m")), int(m.group("d")))
        except ValueError:
            continue
        if d < crawl_date:
            stale.append(m.group(0))
    return stale


# ── Locations file ───────────────────────────────────────────────────────────

def load_locations_file(path: str) -> list[dict[str, str]]:
    """Load an authoritative store list (CSV or JSON) with url, name, address, phone, status."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Locations file not found: {path}")
    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("locations") or data.get("stores") or []
        rows = [dict(r) for r in data if isinstance(r, dict)]
    else:
        with open(p, newline="", encoding="utf-8") as f:
            rows = [dict(r) for r in csv.DictReader(f)]
    out = []
    for r in rows:
        norm = {str(k).strip().lower(): (str(v).strip() if v is not None else "") for k, v in r.items()}
        if norm.get("url"):
            out.append(norm)
    if not out:
        raise ValueError(f"Locations file has no rows with a 'url' column: {path}")
    return out


def _normalize_file_status(raw: str) -> str:
    s = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "temporarily_closed": "temporarily_closed",
        "temp_closed": "temporarily_closed",
        "closed": "closed",
        "permanently_closed": "closed",
        "coming_soon": "coming_soon",
        "moved": "moved",
        "relocated": "moved",
        "ownership_transferred": "ownership_transferred",
        "open": "open",
        "active": "open",
    }
    return aliases.get(s, "")


# ── Classification ───────────────────────────────────────────────────────────

def _result(page_type: str, confidence: float, reasons: list[str]) -> dict[str, Any]:
    return {
        "page_type": page_type,
        "page_type_confidence": round(confidence, 2),
        "page_type_reasons": reasons,
        "location_status": "",
        "moved_to": "",
        "relocated": False,
        "status_evidence": {},
    }


def _is_descendant(child: str, parent: str) -> bool:
    c, p = path_segments(child), path_segments(parent)
    return len(c) > len(p) and c[: len(p)] == p


def _shape(url: str) -> tuple[int, str]:
    segs = path_segments(url)
    return (len(segs), segs[0] if len(segs) > 1 else "*")


def _anchor_prefix(anchor: str, n: int = 3) -> str:
    words = re.findall(r"[a-z0-9']+", (anchor or "").lower())
    return " ".join(words[:n])


def _slug_name(url: str) -> str:
    segs = path_segments(url)
    return " ".join(segs[-1].split("-")) if segs else ""


FALLBACK_CLINIC_REASON = "no directory hub detected; inferred from on-page address and map/phone signals"


def _dom_signals(
    p: dict[str, Any],
    corp_phones: set[str],
    corp_addrs: set[str],
    body_booking: set[str],
    child_count: Counter[str],
) -> tuple[float, list[str]]:
    """Clinic DOM signals -> (score contribution, reasons). Signals add confidence; none is required."""
    nap = p.get("nap_found") or {}
    score = 0.0
    reasons: list[str] = []
    local_phones = [ph for ph in (nap.get("phones") or []) if ph not in corp_phones]
    if local_phones:
        score += 0.15
        reasons.append(f"local phone {local_phones[0]}")
    if page_address_keys(p, corp_addrs):
        score += 0.15
        reasons.append("visible street address")
    if nap.get("map_links") or nap.get("map_embed_coords"):
        score += 0.1
        reasons.append("map embed/link")
    heading_text = " ".join([p.get("title") or ""] + list(p.get("h1") or []))
    if _CITY_STATE_PATTERN.search(heading_text):
        score += 0.1
        reasons.append("title/H1 names a city and state")
    if url_key(p.get("url", "")) in body_booking:
        score += 0.05
        reasons.append("booking call-to-action")
    segs = path_segments(p.get("url", ""))
    if len(segs) == 1 and child_count.get("/" + segs[0], 0) > 0:
        score += 0.1
        reasons.append("has child service pages")
    return score, reasons


def clinic_detection_method(pages: list[dict[str, Any]]) -> str:
    """How clinic pages were found: locations_file, directory, on_page_signals, or none."""
    reasons = [
        r
        for p in pages
        if p.get("page_type") == "clinic_location"
        for r in (p.get("page_type_reasons") or [])
    ]
    if any(r == "listed in --locations-file" for r in reasons):
        return "locations_file"
    if any(r.startswith("linked from directory hub") for r in reasons):
        return "directory"
    if any(r == FALLBACK_CLINIC_REASON for r in reasons):
        return "on_page_signals"
    return "none"


def classify_pages(
    pages: list[dict[str, Any]],
    links: list[dict[str, Any]],
    cfg: ClassifierConfig | None = None,
) -> dict[str, dict[str, Any]]:
    """Classify every crawled page. Returns {url: classification fields}."""
    cfg = cfg or ClassifierConfig()
    crawl_date = cfg.crawl_date or date.today()
    results: dict[str, dict[str, Any]] = {}
    by_key: dict[str, dict[str, Any]] = {}
    for p in pages:
        by_key.setdefault(url_key(p.get("url", "")), p)

    def page_for(target: str) -> dict[str, Any] | None:
        return by_key.get(url_key(target))

    ok = {url_key(p["url"]) for p in pages if int(p.get("status_code") or 0) == 200}
    home_keys = {url_key(p["url"]) for p in pages if not path_segments(p.get("url", ""))}

    # Body-placement edges only: site-wide menus and footers list every page and
    # cannot distinguish directory hubs.
    body_edges: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in links:
        if (e.get("placement") or "body") != "body":
            continue
        s_key, t_key = url_key(e.get("source_url", "")), url_key(e.get("target_url", ""))
        if s_key == t_key:
            continue
        body_edges[s_key].append(e)

    # 1. Utility pages, blog posts/archives, non-200 pages
    for p in pages:
        u = p.get("url", "")
        if int(p.get("status_code") or 0) != 200:
            results[u] = _result("other", 0.5, [f"non-200 status ({p.get('status_code')})"])
            continue
        hit = token_in_segments(u, cfg.utility_tokens)
        if hit:
            results[u] = _result("utility_page", 0.95, [f"utility token '{hit[0]}' in slug segment '{hit[1]}'"])
            continue
        segs = path_segments(u)
        schema = set(p.get("schema_types") or [])
        og_type = ((p.get("og_tags") or {}).get("type") or "").lower()
        if segs and segs[0] in cfg.blog_prefixes:
            if len(segs) == 1 or _BLOG_ARCHIVE_SEGMENTS & set(segs[1:]):
                results[u] = _result("blog_archive", 0.9, [f"blog archive path under /{segs[0]}/"])
            else:
                results[u] = _result("blog_post", 0.9, [f"blog post path under /{segs[0]}/"])
            continue
        if schema & _BLOG_SCHEMA_TYPES or ("Article" in schema and og_type == "article"):
            results[u] = _result("blog_post", 0.8, ["article schema / og:type=article"])
            continue
        if len(segs) >= 2 and segs[0] in cfg.service_prefixes:
            # Classified before hub detection so service listings never look like directories
            results[u] = _result("service_page", 0.8, [f"path under /{segs[0]}/"])
            continue

    # 2. Directory hubs (auto-detected or forced via --directory-url)
    forced_hubs = {url_key(u) for u in cfg.directory_urls}
    hub_candidates: dict[str, set[str]] = {}
    hub_reasons: dict[str, str] = {}
    for p in pages:
        u = p.get("url", "")
        k = url_key(u)
        if k not in ok or u in results or k in home_keys and k not in forced_hubs:
            continue
        if k not in forced_hubs and is_prototype_url(u, cfg.prototype_tokens, cfg.prototype_allowlist):
            continue  # prototype/demo copies are not authoritative directories
        targets: dict[str, str] = {}
        for e in body_edges.get(k, []):
            t = e.get("target_url", "")
            tk = url_key(t)
            if tk not in ok or tk in home_keys:
                continue
            tp = page_for(t)
            if tp is None or results.get(tp["url"], {}).get("page_type") in _NON_CANDIDATE_TYPES:
                continue
            if k in forced_hubs or not _is_descendant(t, u):
                targets.setdefault(tk, tp["url"])
        if k in forced_hubs:
            hub_candidates[k] = set(targets)
            hub_reasons[k] = "listed via --directory-url"
            continue
        groups: dict[tuple[int, str], set[str]] = defaultdict(set)
        for tk, t in targets.items():
            groups[_shape(t)].add(tk)
        if not groups:
            continue
        shape, members = max(groups.items(), key=lambda kv: (len(kv[1]), kv[0]))
        if len(members) >= cfg.hub_min_links:
            hub_candidates[k] = members
            shape_desc = "single-segment slugs" if shape[0] == 1 else f"{shape[0]}-segment paths under /{shape[1]}/"
            hub_reasons[k] = f"{len(members)} distinct body links to {shape_desc}"

    # 3. Clinic candidates and scoring
    file_rows: dict[str, dict[str, str]] = {}
    if cfg.locations_file:
        for row in load_locations_file(cfg.locations_file):
            file_rows[url_key(row["url"])] = row

    corp_phones, corp_addrs = corporate_values(pages, cfg.corporate_share)

    candidate_hubs: dict[str, set[str]] = defaultdict(set)
    candidate_labels: dict[str, list[tuple[str, str]]] = defaultdict(list)  # (hub_url, anchor)
    if file_rows:
        for fk in file_rows:
            candidate_hubs.setdefault(fk, set())
    else:
        for hk, members in hub_candidates.items():
            for mk in members:
                if mk in hub_candidates:
                    continue
                candidate_hubs[mk].add(hk)
    # Directory labels (anchor text of hub body links) for every hub
    dominant_prefix: dict[str, str] = {}
    for hk, members in hub_candidates.items():
        anchors = []
        for e in body_edges.get(hk, []):
            tk = url_key(e.get("target_url", ""))
            if tk in members:
                candidate_labels[tk].append((hk, e.get("anchor_text") or ""))
                anchors.append(_anchor_prefix(e.get("anchor_text") or ""))
        counts = Counter(a for a in anchors if a)
        if counts:
            prefix, cnt = counts.most_common(1)[0]
            if cnt >= max(3, 0.5 * len(members)):
                dominant_prefix[hk] = prefix

    child_count: Counter[str] = Counter()
    for p in pages:
        segs = path_segments(p.get("url", ""))
        if len(segs) >= 2 and url_key(p["url"]) in ok:
            child_count["/" + segs[0]] += 1

    body_booking: set[str] = set()
    for sk, edges in body_edges.items():
        if any(_BOOKING_PATTERN.search((e.get("anchor_text") or "") + " " + (e.get("target_url") or "")) for e in edges):
            body_booking.add(sk)

    clinic_keys: set[str] = set()
    clinic_score: dict[str, tuple[float, list[str]]] = {}
    for ck, hubs in candidate_hubs.items():
        p = page_for(ck)
        if p is None or ck not in ok or ck in home_keys:
            continue
        if results.get(p["url"], {}).get("page_type") in _NON_CANDIDATE_TYPES:
            continue
        reasons: list[str] = []
        score = 0.0
        if ck in file_rows:
            score = 1.0
            reasons.append("listed in --locations-file")
        else:
            score += 0.3
            reasons.append(f"linked from directory hub body ({len(hubs)} hub{'s' if len(hubs) != 1 else ''})")
            if len(hubs) >= 2:
                score += 0.1
            labels = [lab for hk, lab in candidate_labels.get(ck, []) if hk in dominant_prefix and _anchor_prefix(lab) == dominant_prefix[hk]]
            if labels:
                score += 0.2
                reasons.append("directory label follows the hub's dominant entry pattern")
        sig_score, sig_reasons = _dom_signals(p, corp_phones, corp_addrs, body_booking, child_count)
        score += sig_score
        reasons += sig_reasons
        score = min(1.0, score)
        clinic_score[ck] = (score, reasons)
        if score >= cfg.clinic_min_confidence:
            clinic_keys.add(ck)

    for ck in sorted(clinic_keys):
        p = page_for(ck)
        score, reasons = clinic_score[ck]
        results[p["url"]] = _result("clinic_location", score, reasons)

    # A hub whose entries are not clinics is not a location directory
    for hk, members in hub_candidates.items():
        p = page_for(hk)
        n_clinics = len(members & clinic_keys)
        if hk in forced_hubs or (n_clinics >= 3 and n_clinics >= 0.3 * len(members)):
            results[p["url"]] = _result(
                "directory_hub", 1.0 if hk in forced_hubs else 0.9,
                [hub_reasons[hk], f"{n_clinics} entries classified as clinic pages"],
            )

    # 3b. No directory hub and no locations file (e.g. a small site whose /locations/ page
    # lists fewer than --hub-min-links entries): infer clinics from on-page signals so the
    # LocalBusiness checks still run. Requires a local address plus a map or local phone.
    if not clinic_keys and not file_rows:
        fallback: dict[str, tuple[float, list[str]]] = {}
        for p in pages:
            u = p.get("url", "")
            k = url_key(u)
            if k not in ok or k in home_keys or u in results:
                continue
            if is_prototype_url(u, cfg.prototype_tokens, cfg.prototype_allowlist):
                continue
            nap = p.get("nap_found") or {}
            has_addr = bool(page_address_keys(p, corp_addrs))
            has_local_phone = any(ph not in corp_phones for ph in (nap.get("phones") or []))
            has_map = bool(nap.get("map_links") or nap.get("map_embed_coords"))
            if not (has_addr and (has_map or has_local_phone)):
                continue
            sig_score, sig_reasons = _dom_signals(p, corp_phones, corp_addrs, body_booking, child_count)
            score = min(1.0, 0.2 + sig_score)
            if score >= cfg.clinic_min_confidence:
                fallback[k] = (score, [FALLBACK_CLINIC_REASON] + sig_reasons)
        # A page under another inferred clinic is that clinic's service page
        for k in sorted(fallback):
            u = page_for(k)["url"]
            if any(_is_descendant(u, page_for(o)["url"]) for o in fallback if o != k):
                continue
            clinic_keys.add(k)
            results[u] = _result("clinic_location", *fallback[k])

    # 4. Secondary hubs: regional pages that link to clinics and name several of them
    clinic_names = {ck: _slug_name(page_for(ck)["url"]) for ck in clinic_keys}
    clinic_urls = [page_for(ck)["url"] for ck in clinic_keys]
    for p in pages:
        u = p.get("url", "")
        k = url_key(u)
        if k not in ok or u in results or k in home_keys:
            continue
        if any(_is_descendant(u, cu) for cu in clinic_urls):
            continue  # clinic service pages
        if is_prototype_url(u, cfg.prototype_tokens, cfg.prototype_allowlist):
            continue
        t_keys = {url_key(e.get("target_url", "")) for e in body_edges.get(k, [])}
        t_keys = {
            t for t in t_keys
            if t in ok and t not in home_keys and not _is_descendant(by_key[t]["url"], u)
        }
        clinic_targets = t_keys & clinic_keys
        if not clinic_targets or len(clinic_targets) < 0.5 * len(t_keys):
            continue
        headings = " ".join(list(p.get("h1") or []) + list(p.get("h2") or []) + list(p.get("h3") or []))
        heading_norm = " ".join(re.findall(r"[a-z0-9]+", headings.lower()))
        # Links alone don't make a directory: the headings must name at least two clinics
        mentioned = {ck for ck, name in clinic_names.items() if name and f" {name} " in f" {heading_norm} "}
        if len(mentioned) >= 2:
            results[u] = _result(
                "directory_hub", 0.7,
                [f"body links to {len(clinic_targets)} clinic page(s)", f"headings name {len(mentioned)} clinics"],
            )

    # 5. Clinic service pages, service pages, everything else
    clinic_paths = {tuple(path_segments(page_for(ck)["url"])): page_for(ck)["url"] for ck in clinic_keys}
    for p in pages:
        u = p.get("url", "")
        if u in results:
            continue
        segs = path_segments(u)
        parent = next(
            (clinic_paths[tuple(segs[:i])] for i in range(len(segs) - 1, 0, -1) if tuple(segs[:i]) in clinic_paths),
            None,
        )
        if parent:
            results[u] = _result("clinic_service", 0.9, [f"child of clinic page {parent}"])
        elif segs and segs[0] in cfg.service_prefixes:
            results[u] = _result("service_page", 0.8, [f"path under /{segs[0]}/"])
        else:
            results[u] = _result("other", 0.5, ["homepage"] if not segs else ["no classification rule matched"])

    # 6. Location status for clinic pages
    _assign_location_status(pages, results, clinic_keys, file_rows, candidate_labels, corp_addrs, cfg, crawl_date)
    return results


def _assign_location_status(
    pages: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    clinic_keys: set[str],
    file_rows: dict[str, dict[str, str]],
    candidate_labels: dict[str, list[tuple[str, str]]],
    corp_addrs: set[str],
    cfg: ClassifierConfig,
    crawl_date: date,
) -> None:
    by_key = {url_key(p["url"]): p for p in pages}
    markers = cfg.status_markers

    clinic_addr: dict[str, set[str]] = {}
    clinic_coords: dict[str, set[str]] = {}
    for ck in clinic_keys:
        p = by_key[ck]
        keys = page_address_keys(p, corp_addrs)
        if ck in file_rows and file_rows[ck].get("address"):
            fk = address_key(file_rows[ck]["address"])
            if fk:
                keys.add(fk)
        clinic_addr[ck] = keys
        coords = set()
        for c in (p.get("nap_found") or {}).get("map_embed_coords") or []:
            try:
                lat, lng = (float(x) for x in c.split(","))
                coords.add(f"{lat:.3f},{lng:.3f}")
            except ValueError:
                continue
        clinic_coords[ck] = coords

    page_claims: dict[str, tuple[str, str, int] | None] = {}
    label_claims: dict[str, tuple[str, str, str] | None] = {}  # (status, phrase, label)
    openings: dict[str, str] = {}
    for ck in clinic_keys:
        p = by_key[ck]
        opening = (p.get("opening_text") or "")[: cfg.opening_chars]
        if not opening:
            # Older crawls have no opening_text: fall back to the hero headings
            opening = " ".join(list(p.get("h1") or [])[:1] + list(p.get("h2") or [])[:3])
        openings[ck] = opening
        page_claims[ck] = first_status(opening, markers)
        label_claims[ck] = None
        for _hub, label in sorted(candidate_labels.get(ck, [])):
            ls = label_status(label, markers)
            if ls:
                label_claims[ck] = (ls[0], ls[1], label)
                break

    # Map coordinates shared by many clinic pages are site chrome (e.g. an HQ map), not evidence
    coord_counts: Counter[str] = Counter(c for cs in clinic_coords.values() for c in cs)
    shared_coords = {
        c for c, k in coord_counts.items() if k > 2 and k >= cfg.corporate_share * max(1, len(clinic_keys))
    }

    def moved_target(ck: str) -> str:
        own = clinic_addr.get(ck, set())
        own_coords = clinic_coords.get(ck, set()) - shared_coords
        matches = []
        for other in sorted(clinic_keys - {ck}):
            addr_match = bool(own & clinic_addr.get(other, set()))
            coord_match = bool(own_coords & (clinic_coords.get(other, set()) - shared_coords))
            if addr_match or coord_match:
                claims_moved = (page_claims.get(other) or ("",))[0] == "moved" or (label_claims.get(other) or ("",))[0] == "moved"
                matches.append((not addr_match, claims_moved, other))
        if not matches:
            return ""
        matches.sort()
        return by_key[matches[0][2]]["url"]

    for ck in sorted(clinic_keys):
        p = by_key[ck]
        res = results[p["url"]]
        opening = openings[ck]
        pc, lc = page_claims[ck], label_claims[ck]
        evidence: dict[str, str] = {}
        if pc:
            evidence["page_notice"] = opening[pc[2] : pc[2] + 300]
        if lc:
            evidence["directory_label"] = lc[2]

        file_status = _normalize_file_status(file_rows.get(ck, {}).get("status", "")) if ck in file_rows else ""
        status, moved_to, relocated = "", "", False
        if file_status:
            status = file_status
            evidence["source"] = "locations_file"
            if status == "moved":
                moved_to = moved_target(ck)
        elif (pc and pc[0] == "moved") or (lc and lc[0] == "moved"):
            target = moved_target(ck)
            if target:
                status, moved_to = "moved", target
                evidence["source"] = "on_page_notice" if pc and pc[0] == "moved" else "directory_label"
                evidence["address_match"] = target
            elif find_status_markers(opening, {"ownership_transferred": markers.get("ownership_transferred", ())}):
                status = "ownership_transferred"
                evidence["source"] = "on_page_notice"
            else:
                status, relocated = "open", True
                evidence["source"] = "on_page_notice" if pc and pc[0] == "moved" else "directory_label"
                evidence["note"] = "move notice names an address no other clinic page uses"
            if lc and lc[0] != "moved" and status == "moved":
                evidence["conflict"] = f"directory label says {lc[0]}; address-verified move takes precedence"
        elif lc:
            status = lc[0]
            evidence["source"] = "directory_label"
            if pc and pc[0] != lc[0]:
                evidence["conflict"] = f"page notice says {pc[0]}"
        elif pc:
            status = pc[0]
            evidence["source"] = "on_page_notice"
        else:
            nap = p.get("nap_found") or {}
            has_signals = bool(nap.get("phones") or clinic_addr.get(ck) or nap.get("map_links"))
            status = "open" if has_signals else "unknown"
            evidence["source"] = "clinic_signals" if has_signals else "none"

        if pc or lc:
            notice_text = evidence.get("page_notice") or evidence.get("directory_label", "")
            stale = find_stale_dates(notice_text, crawl_date)
            if stale:
                evidence["stale_dates"] = "; ".join(stale)

        res["location_status"] = status
        res["moved_to"] = moved_to
        res["relocated"] = relocated
        res["status_evidence"] = evidence
