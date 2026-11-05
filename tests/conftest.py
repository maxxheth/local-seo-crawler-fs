"""Shared synthetic-site fixture modelled on the F2F cases (no real client pages).

The HTML is parsed by the real CompetitorSiteAdapter, stored in SQLiteStorage, and run
through the real post-crawl stage, so classification, address extraction and opening-
section capture are exercised end to end.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from src.adapters.competitor import CompetitorSiteAdapter
from src.post_crawl import run_post_crawl_stage
from src.seo_auditor import SEOAuditConfig, audit_page
from src.storage import SQLiteStorage

SITE = "https://spa.example.com"
CRAWL_DATE = date(2026, 11, 5)

CLINICS = [
    "oak-hill", "pine-grove", "elm-park", "cedar-ridge", "maple-st",
    "birch-bay", "willow", "harbor", "aspen", "spruce",
]
DIRECTORY_LABELS = {c: f"Spa at {c.replace('-', ' ').title()}" for c in CLINICS}
DIRECTORY_LABELS["cedar-ridge"] += " - Temporarily Closed"
DIRECTORY_LABELS["maple-st"] += " - We've Moved!"

FILLER = " ".join(["Our team focuses on personalised skin care and a welcoming experience."] * 12)

LOCAL_SCHEMA = '<script type="application/ld+json">{"@type": "DaySpa", "name": "Pine Grove"}</script>'


def _nav() -> str:
    # Mega-menu listing every clinic on every page: must never create directory hubs
    items = "".join(f'<li><a href="/{c}/">{c}</a></li>' for c in CLINICS)
    return f'<header><nav class="menu"><ul>{items}</ul><a href="/book/">Book Now</a></nav></header>'


def _footer() -> str:
    return '<footer><p>Corporate office 512-555-0000</p><a href="/privacy/">Privacy</a></footer>'


def page_html(title: str, main: str, head: str = "", canonical: str = "") -> str:
    canon = f'<link rel="canonical" href="{canonical}">' if canonical else ""
    return (
        f"<!DOCTYPE html><html lang='en'><head><title>{title}</title>"
        f"<meta name='viewport' content='width=device-width'>{canon}{head}</head>"
        f"<body>{_nav()}<main>{main}</main>{_footer()}</body></html>"
    )


def _clinic(name: str, opening: str, address: str = "", phone: str = "", map_embed: bool = True, tail: str = "") -> str:
    addr = f"<p class='address'>{address}</p>" if address else ""
    ph = f"<a href='tel:{phone}'>{phone}</a>" if phone else ""
    lng = -90 - (sum(map(ord, name)) % 1000) / 100.0  # distinct per clinic name
    mp = f"<iframe src='https://www.google.com/maps/embed?pb=!2d{lng:.4f}!3d30.2000'></iframe>" if map_embed else ""
    return f"{opening}<h1>Med Spa in {name}</h1>{addr}{ph}{mp}<p>{FILLER}</p>{tail}<a href='/book/'>Book an appointment</a>"


def build_pages() -> dict[str, dict[str, Any]]:
    """Return {path: {"html": ..., options}} for the synthetic site."""
    p: dict[str, dict[str, Any]] = {}
    p["/"] = {"html": page_html("Spa Example Home", "<h1>Welcome</h1><a href='/locations/'>Locations</a>"
                                 "<a href='/franchising/'>Franchising</a><a href='/old-page'>Old page</a>")}
    hub_links = "".join(f"<li><a href='/{c}/'>{DIRECTORY_LABELS[c]}</a></li>" for c in CLINICS)
    p["/locations/"] = {"html": page_html("Locations", f"<h1>Our Locations</h1><ul>{hub_links}</ul>"
                                          "<a href='/book/'>Book Now</a><a href='/reviews-and-testimonials/'>reviews</a>")}
    # Prototype copy of the directory: flagged as prototype, never a hub
    p["/full-locations-demo/"] = {"html": page_html("Locations Demo", f"<h1>Demo</h1><ul>{hub_links}</ul>")}
    # Regional page linking one clinic in the body but naming two in headings (secondary hub)
    p["/washington/"] = {"html": page_html(
        "Washington Locations",
        "<h1>Washington Locations</h1><h2>Maple St Location - We've Moved!</h2><h2>Birch Bay Location</h2>"
        "<a href='/birch-bay/'>View Location</a>",
    )}

    p["/oak-hill/"] = {"html": page_html("Med Spa in Austin, TX", _clinic(
        "Austin, TX", "", "100 Oak Hill Rd, Austin, TX 78701", "512-555-0101"))}
    p["/oak-hill/botox/"] = {"html": page_html("Botox at Oak Hill", "<h1>Botox</h1>" + FILLER)}
    p["/pine-grove/"] = {"html": page_html("Med Spa in Dallas, TX", _clinic(
        "Dallas, TX", "", "200 Pine Grove Ave, Dallas, TX 75201", "214-555-0102",
        tail="<h2>Memberships</h2><p>Become a member today. Coming soon!</p>"), head=LOCAL_SCHEMA)}
    p["/elm-park/"] = {"html": page_html("Med Spa in Houston, TX", _clinic(
        "Houston, TX", "", "", "713-555-0103", map_embed=False))}
    p["/cedar-ridge/"] = {"html": page_html("Cedar Ridge Location", _clinic(
        "Little Rock, AR",
        "<h2>Temporary Closure Notice</h2><p>We are temporarily closed due to renovations. "
        "We anticipate reopening in October and look forward to welcoming you back.</p>",
        "", "501-555-0104", map_embed=False))}
    p["/birch-bay/"] = {"html": page_html("Med Spa in Seattle, WA", _clinic(
        "Seattle, WA", "", "500 Birch Bay Blvd Seattle, WA 98101", "206-555-0105"))}
    p["/maple-st/"] = {"html": page_html("Maple St Location", _clinic(
        "Maple St",
        "<h2>We've Moved!</h2><p>Maple St has relocated and is now serving clients at our sister "
        "location: Birch Bay 500 Birch Bay Boulevard, Seattle, Washington 98101.</p>",
        "", "206-555-0106"))}
    p["/willow/"] = {"html": page_html("Med Spa in Orem, UT", _clinic(
        "Orem, UT",
        "<h2>We Have Moved!</h2><p>Willow has relocated to 560 W 800 N, Suite 121 Orem, Utah 84057.</p>",
        "575 E. University Pkwy, N-238 Orem, UT 84097", "801-555-0107"))}
    p["/harbor/"] = {"html": page_html("Harbor Location", _clinic(
        "Baltimore, MD",
        "<h2>Harbor Is Under New Ownership</h2><p>Harbor is now owned by Acme Wellness. "
        "The location remains at 3989 Boston Street in Baltimore.</p>",
        "3989 Boston Street Baltimore, MD 21224", "410-555-0108"))}
    p["/aspen/"] = {"html": page_html("Med Spa in Denver, CO", _clinic(
        "Denver, CO", "", "77 Shared Ln, Denver, CO 80202", "303-555-0109"))}
    p["/spruce/"] = {"html": page_html("Med Spa in Denver, CO", _clinic(
        "Denver, CO", "", "77 Shared Lane Denver, CO 80202", "303-555-0110"))}

    for c in ("oak-hill", "cedar-ridge"):
        p[f"/locations/thank-you-{c}-location/"] = {"html": page_html("Thank You", "<h1>Thank you!</h1>")}
    p["/book/"] = {"html": page_html("Book an appointment", "<h1>Book</h1>" + FILLER)}
    p["/reviews-and-testimonials/"] = {"html": page_html("Reviews and testimonials", "<h1>Reviews</h1>" + FILLER)}
    p["/careers-old/"] = {"html": page_html("Careers (old)", "<h1>Careers</h1>"), "in_sitemap": True}
    p["/memberships-new/"] = {"html": page_html("Memberships new", "<h1>Memberships</h1>")}
    p["/map-test-page/"] = {"html": page_html(
        "Map test", "<h1>Map test</h1>", head="<meta name='robots' content='noindex'>")}
    p["/blog/new-location-at-oak-hill/"] = {"html": page_html(
        "New location at Oak Hill", "<h1>New location</h1><a href='/oak-hill/'>Oak Hill</a>"
        "<a href='www.youtube.com/watch?v=abc'>Watch video</a><a href='www.youtube.com/watch?v=ok'>Second video</a>")}
    p["/blog/congratulations-to-our-newest-team/"] = {"html": page_html("Congrats", "<h1>Congrats</h1>")}
    p["/services/facials/"] = {"html": page_html(
        "Facials", "<h1>Facials</h1>", canonical="https://staging-2026.example.com/services/facials/")}
    return p


def _record(adapter: CompetitorSiteAdapter, url: str, html: str, **kw: Any) -> dict[str, Any]:
    rec, _, _, edges = adapter.parse(url=url, html=html, http_status=200, final_url=url, **kw)
    rec.issues = audit_page(rec, SEOAuditConfig())
    return {"record": rec, "edges": edges}


def build_site_db(db_path: Path) -> SQLiteStorage:
    storage = SQLiteStorage(db_path)
    adapter = CompetitorSiteAdapter()
    for path, spec in build_pages().items():
        url = SITE + path
        out = _record(adapter, url, spec["html"], in_sitemap=spec.get("in_sitemap", False))
        storage.save_page(out["record"].model_dump(), domain="spa.example.com")
        storage.save_seo_issues_batch(out["record"].issues)
        storage.save_internal_links_batch(out["edges"])

    # Non-HTML outcomes discovered during the crawl
    def save_plain(url: str, status: int, **fields: Any) -> None:
        from src.models import PageTechnicalRecord

        rec = PageTechnicalRecord(url=url, status_code=status, final_url=fields.pop("final_url", url), **fields)
        rec.issues = audit_page(rec, SEOAuditConfig())
        storage.save_page(rec.model_dump(), domain="spa.example.com")
        storage.save_seo_issues_batch(rec.issues)

    save_plain(SITE + "/old-page", 301, initial_status_code=301, redirect_chain=["301 -> /oak-hill/"],
               redirect_target_status=200, final_url=SITE + "/oak-hill/")
    save_plain(SITE + "/franchising/", 301, initial_status_code=301,
               redirect_chain=["301 -> https://spa-franchising.example.org/"],
               final_url="https://spa-franchising.example.org/",
               transport_error="[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Hostname mismatch, "
                               "certificate is not valid for 'spa-franchising.example.org'.",
               transport_error_type="tls_hostname_mismatch")
    save_plain(SITE + "/blog/new-location-at-oak-hill/www.youtube.com/watch?v=abc", 404)
    save_plain(SITE + "/blog/new-location-at-oak-hill/www.youtube.com/watch?v=ok", 301, initial_status_code=301,
               redirect_chain=["301 -> /blog/new-location-at-oak-hill/"], redirect_target_status=200,
               final_url=SITE + "/blog/new-location-at-oak-hill/")
    save_plain(SITE + "/privacy/", 404)
    storage.set_meta("domain", "spa.example.com")
    run_post_crawl_stage(storage, crawl_date=CRAWL_DATE)
    return storage


@pytest.fixture()
def site_storage(tmp_path: Path) -> SQLiteStorage:
    return build_site_db(tmp_path / "crawl_results.db")


def pages_by_path(storage: SQLiteStorage) -> dict[str, dict[str, Any]]:
    return {p["url"].replace(SITE, ""): p for p in storage.fetch_all_pages()}


def issues_for(page: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [i for i in page.get("issues", []) if i["issue"] == name]
