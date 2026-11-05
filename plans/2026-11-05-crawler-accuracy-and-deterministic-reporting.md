# Plan: Crawler Accuracy & Deterministic Reporting

**Date:** 2026-11-05
**Repo:** `local-seo-crawler-fs` (baseline `af10fac`)
**Trigger:** I reviewed the facetofacespa.com crawl of 2026-11-05 (`artifacts_facetofacespa_fresh/`) against the live site. The crawl data is now largely accurate, but some rules still misclassify pages or hide real defects, and the Markdown report contradicts its own data. The reference write-up is in the F2F WordPress repo: `docs/LOCAL-SEO-GAP-ANALYSIS-2026-11-04.md` (corrected 2026-11-05).
**Revision 2 (2026-11-05):** after Gemini Argon's additions to WS4, I checked the location-status rules against all 42 F2F clinic pages and corrected them:
- **Moved vs. open:** "moved" is now decided by whether the displayed address matches another clinic page, not by whether an address and map are shown.
- **New status:** `ownership_transferred` (Canton Crossings).
- **Scoped notices:** status notices are read only from the page's opening section.
- **New checks:** stale status notices and duplicate clinic addresses.
- **No fixed total:** "38 open clinics" is removed.
- **Maryland address:** I also corrected my own original claim that the Maryland pages show no address.

---

## 1. Goals and Non-Goals

**Goals**
1. **Resolve links the way browsers do,** and report links users can't actually follow.
2. **Make sure no infrastructure failure goes missing** from the broken-link rollups. TLS, DNS and timeout errors count too, including at the end of redirect chains.
3. **Classify pages deterministically** (clinic, directory, utility, service and so on), so rules like *Missing LocalBusiness schema* apply to the right pages.
4. **Detect prototype and staging pages** without false positives.
5. **Generate the Markdown report from the database,** so the numbers and statements in the report always match the crawl data.
6. **Label crawler-measured timing honestly,** and optionally add real-user performance data.

**Non-goals**
- Changing the fetch and transport stack (FlareSolverr escalation, session pool, proxies).
- Rewriting duplicate detection. The Dice-similarity clusters checked out against the live site.
- Building a site-specific rule set. Facts about F2F are used only as test fixtures and acceptance checks, never hard-coded in `src/`.

---

## 2. Evidence: Defects to Fix (from the F2F Nov 5 crawl)

| # | Defect | Where in code | Observed |
| :--- | :--- | :--- | :--- |
| D1 | Bare-domain links are rewritten to `https://<href>` and treated as external | `src/adapters/competitor.py` ~L35 (`BARE_DOMAIN_PATTERN`), ~L419 | 7 blog links such as `href="www.youtube.com/watch?v=…"`. Browsers open `/blog/<post>/www.youtube.com/…`: 6 return 404 and 1 lands back on its own post through a WordPress redirect. All are hidden as external. `tests/test_gap_analysis_regressions.py::test_gap5_bare_domain_link_extraction` locks in the wrong behaviour. |
| D2 | Links resolve against the **requested** URL, not the final URL after redirects | `competitor.py` ~L426 (`normalize_url(..., base_url=url)`) | A page reached through `/x` → 301 → `/x/` resolves relative links one directory too high. This caused the earlier `/blog/www…` paths. `<base href>` is ignored too. |
| D3 | Transport errors aren't counted as broken links | `src/storage.py::post_crawl_sync` (step 1b only uses `redirect_target_status >= 400`) | `/franchising/` → 301 → `facetofacefranchising.com` fails with a TLS hostname mismatch. The page carries a "Transport Error" issue, but no source page gets a broken outlink, and the report omits it. |
| D4 | Location-page detection relies on URL substrings | `src/seo_auditor.py` ~L420–424 (`/locations/`, `/austin`, `/ranch`, `/spa`, `-tx` …) | "Missing LocalBusiness" flags 83 pages: 38 clinics, 43 thank-you pages under `/locations/`, plus `/locations/` and `/ideal-image-locations/`. It misses the 3 Maryland clinics: they have no map embed, and the address extractor misses their `3989 Boston Street Baltimore, MD 21224` format (see D9). |
| D5 | Prototype detection is a narrow substring list | `seo_auditor.py` ~L300 (`prototype`, `staging`, `test-page`, `temp-`, `draft-`) | Flags only `/map-test-page/`. Misses `/full-locations-demo/`, `/careers-old/` and `/memberships-new/`. Simply adding `test`, `new` or `old` would wrongly match `/reviews-and-testimonials/` and the "new location" blog posts. |
| D6 | No report command; the narrative is written outside the crawler | `src/main.py` (no report mode) | The report contradicts its data: "Apache" (the crawl detected Cloudflare), `/locations/*` "parallel architecture" (the data shows 301s), Downtown Austin under the identical title (it isn't), Promenade at Crocker as a duplicate (it isn't flagged), "1 prototype page" but two listed, and `/franchising` missing. |
| D7 | Crawler timing is presented as server performance | `seo_auditor.py` ~L442–460; summary ~L720–769 | The average changed 1,824 → 2,943 → 854 ms across three runs on an unchanged site, yet it produces the largest warning category. |
| D8 | Internal links to redirecting URLs aren't reported | — | 84 slash-less → slash 301s, plus `/services/microneedling/` → `/services/micro-needling/`, all linked internally. |
| D9 | The address extractor needs a comma between street and city | NAP extraction in `competitor.py` (`nap_found.addresses`) | `/canton-crossings/`, `/towson/` and `/arundel-mills-hanover/` display `3989 Boston Street Baltimore, MD 21224`, but `nap_found.addresses` is empty. Comma-separated forms such as `14900 Avery Ranch Blvd, C-100 Austin, TX` are extracted. |

---

## 3. Workstreams

### WS1: RFC 3986 link resolution (D1, D2)

**Changes**
- **Remove the rewrite.** Delete the `BARE_DOMAIN_PATTERN` rewrite in `competitor.py`. Every `href` without a scheme and not starting with `//` goes through standard `urljoin` against the **document base**.
- **Document base** = `<base href>` if present (itself resolved against the final URL), otherwise the **final URL** after redirects (`effective_final_url`), keeping its trailing slash.
- **Annotate, don't resolve:** keep a bare-domain check only for labelling. When a scheme-less `href` looks like a hostname (`www.…`, or `name.tld/…`), mark the edge `malformed_relative=1` and record `intended_host` (for example `www.youtube.com`).
- **Storage:** add columns `malformed_relative INTEGER DEFAULT 0` and `intended_host TEXT` to `internal_links`, using an idempotent `ALTER TABLE … ADD COLUMN` in `SQLiteStorage._init_db`.
- **New post-crawl issue: "Malformed Relative Link"** (category Links), reported on the source page with the raw `href`, the resolved URL and the target status. The severity depends on the outcome:
  - **error** if the resolved target returns 4xx or 5xx or has a transport error; such links are also counted in `broken_outlinks`;
  - **warning** if it resolves with 2xx or 3xx, because it still doesn't reach the intended external site.

**Tests**
- Rewrite `test_gap5_bare_domain_link_extraction` to expect **relative resolution** plus the malformed flag.
- New: a page fetched as `/blog/post` → 301 → `/blog/post/`, with `href="www.youtube.com/watch?v=x"`. It must resolve to `/blog/post/www.youtube.com/watch?v=x` and not `/blog/www.youtube.com/…`.
- New: `<base href="/foo/">` is honoured.
- New: a malformed link whose target is 404 → error plus broken outlink; one whose target is 301 → 200 → warning only.
- Regression: `mailto:`, `tel:`, `javascript:`, `#frag`, `//cdn.example.com/x` and absolute URLs behave exactly as before.

**Done when** a re-crawl of F2F reports all 7 blog links as Malformed Relative Link: 6 errors and 1 warning, with the correct resolved URLs.

---

### WS2: Transport-error propagation (D3)

**Changes**
- **`scraper.py`: classify transport errors** into a structured `transport_error_type`: `tls_hostname_mismatch`, `tls_verify_failed`, `dns_failure`, `timeout`, `connection_refused`, `connection_reset` or `other`. Keep the raw message in `transport_error`.
- **`storage.post_crawl_sync`: treat a transport failure as broken.** A link target is broken when its status is ≥ 400 **or** it has a transport error, **or** it's a redirect whose final hop has a transport error. Store this as `target_status = 0` plus a new column `target_error_type` on `internal_links`, and include such targets in `broken_outlinks`.
- **Make the rollups agree:** `SiteAuditSummary.broken_internal_links_count`, the Excel "Broken Links" sheet and the report all count transport-broken targets, broken down by error type.

**Tests**
- A fixture where page A links to `/franchising/`, which 301s to an external host failing TLS: A gets a broken outlink with `tls_hostname_mismatch`, and the summary counts it.
- A DNS failure and a timeout map to their types.
- The existing `test_gap3_external_ssl_error_on_redirect_does_not_escalate_to_500` still passes (no false 500).

**Done when** the F2F re-crawl lists `/franchising/` as a broken target, linked from its real source pages and labelled `tls_hostname_mismatch`.

---

### WS3: Deterministic report command (D6)

**Changes**
- **New CLI mode:** `--mode report --db <crawl_results.db> [--output <file.md>] [--format md|json]`. It reads an existing database and never crawls. A `report` mode keeps `--mode` as the single dispatch point.
- **New module `src/report.py`:**
  - **SQL aggregations:**
    - totals and status distribution;
    - issues by severity, category and type;
    - broken targets with their sources and error types;
    - redirects grouped by pattern;
    - duplicate clusters (connected components of the duplicate edges);
    - schema coverage by `page_type` (from WS4);
    - the server and stack values actually seen in headers (`server_header`, `cache_status`);
    - timing (labelled as in WS6).
  - **A fixed Markdown template with conditional sections.** Every number and URL in it comes from a query result. Each finding lists up to N sample URLs (default 10) and links to the matching Excel sheet.
  - **No free-text claims.** If something wasn't measured, the report says "not measured". Stack statements come only from observed headers.
  - **`report_summary.json` alongside:** the same numbers in machine-readable form, for downstream agents.
- **Optional `--mode report --verify-narrative <md>`:** extracts numbers and URL lists from an externally written narrative and fails (non-zero exit) if any of them disagrees with `report_summary.json`. This guards any AI-written commentary layer.
- **Old databases:** works on crawls without WS4 columns by omitting the page-type sections with a note.

**Tests**
- A golden-file test on a small fixture database: the rendered Markdown matches the expected file exactly.
- Every number in the rendered report appears in `report_summary.json`, and vice versa.
- `--verify-narrative` catches an injected wrong count, a wrong stack ("Apache" when headers say Cloudflare), and a duplicate claim that isn't in the duplicate clusters.

**Done when** the F2F report has no claims beyond the data (no "Apache"; `/locations/*` described as redirects; Austin duplicates are exactly the pages in the clusters) and includes `/franchising` and the malformed blog links.

---

### WS4: Post-crawl page classification (D4)

**Where it runs:** a new **post-crawl stage**, because a single page's parse doesn't have the directory pages and the link graph it needs.
- **Placement:** in `orchestrator.run`, after link verification and before `detect_duplicate_pages` / `post_crawl_sync`.
- **Context-dependent rules move out of the crawl:** the rules that depend on page type move from per-page `audit_page` (run in workers from `scraper.py:436`) to a new `seo_auditor.audit_site_context(pages, links, cfg)`.

**New module `src/classifier.py`.** It writes these to `pages` (new columns) and `raw_json`:
- `page_type`: `clinic_location`, `clinic_service`, `directory_hub`, `utility_page`, `service_page`, `blog_post`, `blog_archive` or `other`;
- `page_type_confidence` (0–1), plus `page_type_reasons` (JSON list) explaining why;
- `location_status`: `open`, `coming_soon`, `temporarily_closed`, `moved`, `ownership_transferred`, `closed` or `unknown`. Only for clinic pages.
- `moved_to`: the clinic page whose address a moved page now displays, or empty.
- `relocated` (boolean): true when an open clinic announces a move to a new address that no other clinic page uses.
- `status_evidence`: the notice text and where it was found (directory label, hero block or locations file).

**Steps, applied in order and recorded in `page_type_reasons`:**
1. **Utility exclusion:** whole slug segments containing `thank-you`, `thankyou`, `confirmation`, `submitted`, `cart`, `checkout`, `order-received`, `success` → `utility_page`. The list is configurable.
2. **Directory hubs:** pages whose **body-placement** links point to at least `--hub-min-links` (default 10) distinct candidate pages that share a shape (for example single-segment slugs).
   - Use the existing `internal_links.placement`. **Ignore `navigation` and `footer` links.** The F2F mega-menu lists every clinic on every page, so all-link counting can't tell hubs apart.
   - An optional `--directory-url` (repeatable) overrides auto-detection.
3. **Clinic candidates:**
   - **Sources:** body-placement targets of the directory hubs, and an optional authoritative `--locations-file` (CSV/JSON of URL, name, address, phone and status, for example a store-locator export). When the file is given, it wins over candidates detected from pages.
4. **DOM signals → confidence, never a hard requirement.**
   - **Signals:**
     - a **local** phone number, meaning one that appears on fewer than X% of pages so corporate numbers are excluded;
     - a visible street address;
     - a map embed;
     - a booking call-to-action;
     - an H1 or title pattern like "… in <City>, <ST>".
   - **Missing signals are findings in their own right.** If a confirmed clinic has no visible address, raise **"Clinic Page Missing Visible Address"**. F2F's `/little-rock/` shows none, which is deliberate while it's closed.
   - **Fix the address extractor first (D9).** Accept a street line followed directly by `City, ST 12345` without a comma, and also full state names (`Orem, Utah 84057`). Otherwise the missing-address check would report false positives such as the Maryland pages.
5. **Clinic service pages:** `/<clinic>/<service>/` where `<clinic>` is a `clinic_location` → `clinic_service`.
6. **Location status.**
   - **Sources, in priority order** (the source used is recorded in `status_evidence`):
     1. `--locations-file`, when given.
     2. **Directory labels:** status words in the store's entry on a directory hub. F2F's store locator marks closed stores in the store title, for example "… - Temporarily Closed"; see `ClosedLocationContentMigration::closedLocationName` in the F2F WordPress repo.
     3. **On-page notices, limited to the opening section:** the H1/H2 hero, or the first heading block before the main content. Whole-page matching misfires. The three F2F Ohio clinics (Strongsville, Mayfield Heights, Cuyahoga Falls) show "Become a member… Coming soon" about memberships, and they're open.
   - **Mapping** (phrases are case-insensitive, configurable with `--status-markers`, and include common variants):
     - **Temporarily closed** — "temporarily closed", "temporary closure" → `temporarily_closed`.
     - **Coming soon** — "coming soon", "grand opening" in the opening section or in a directory label → `coming_soon`.
     - **Closed** — "permanently closed", or a directory label saying closed → `closed`.
     - **Moved** — "has moved", "we've moved" / "we’ve moved", "we have moved" or "relocated". Then compare the address the page displays with every other clinic page's address (normalised):
       - **If it matches another clinic page** → `moved`, with `moved_to` set to that page. On F2F: `/st-paul-grand-ave/` → `/woodbury/`; `/towson/` and `/arundel-mills-hanover/` → `/canton-crossings/`.
       - **If it's a new address no other clinic page uses** → `open`, with `relocated = true`. On F2F: `/orem/` → "relocated to 560 W 800 N, Suite 121, Orem".
       - **Showing an address or map doesn't decide this.** `/st-paul-grand-ave/` shows the Woodbury address and map, but it has moved.
     - **Ownership transferred** — "under new ownership", "now owned by" → `ownership_transferred`. **Never mapped to `moved`.** On F2F, `/canton-crossings/` was deliberately reworded from "Has Moved!" to "Is Under New Ownership" (ENG-1065). Whether such a page should carry the brand's schema is a business decision, so the crawler flags it for review rather than guessing.
     - **Otherwise:** `open` if the page has clinic signals, or `unknown`.
   - **Out-of-date notices:** if a status notice names a month or date that has already passed at crawl time ("We anticipate reopening in October" on a crawl dated November), raise a **warning: "Stale Status Notice"**, quoting the text. On F2F that's `/little-rock/` and `/woodbury/`.
   - **Shared addresses:** if two or more clinic pages display the same normalised address and none of them has `moved_to` pointing at the others, raise a **warning: "Duplicate Clinic Address"** listing the pages. This prevents two open business entities sharing one address.

**Rule changes in `audit_site_context`:**
- **Remove** the URL-substring `is_location_page` heuristic at `seo_auditor.py` ~L420.
- **"Missing LocalBusiness / Clinic Schema"** applies only when `page_type = clinic_location`, `location_status ∈ {open, unknown}`, and the page is indexable.
- **Clinics that are closed, temporarily closed or moved** get an **info** issue instead: "Closed/moved clinic page: confirm indexing and schema approach". Moved pages name their `moved_to` page.
- **Clinics with `ownership_transferred`** get an **info** issue: "Ownership transferred: confirm whether this page should carry the brand's LocalBusiness schema".
- The new **"Stale Status Notice"** and **"Duplicate Clinic Address"** checks from step 6 run here as well.
- **Thank-you pages** keep the existing "Indexable Thank-You / Confirmation Page" rule, now keyed on `page_type = utility_page`.

**Exports:** add `page_type` and `location_status` to the CSV, the JSON artifact (additive fields only) and the Excel "All Crawled Pages" sheet. Add a "Page Types" sheet.

**Tests**
- **Synthetic fixtures:**
  - the menu lists all clinics on every page and must not create hubs;
  - a body-list directory page must be detected as a hub;
  - thank-you pages under `/locations/` must be `utility_page`;
  - a clinic with phone but no address must still be a `clinic_location` with a missing-address issue;
  - a "Temporary Closure Notice… temporarily closed" hero must give `temporarily_closed`;
  - "We've Moved!" with an address equal to another clinic page's must give `moved` with `moved_to` set to that page, even when a map embed is present;
  - "We Have Moved!… relocated to <new address>", where no other clinic uses that address, must give `open` with `relocated = true`;
  - "Is Under New Ownership… now owned by …" must give `ownership_transferred`, not `moved`;
  - "Coming soon" outside the opening section (for example in a memberships block) must **not** change the status;
  - "reopening in October" on a crawl dated in November must raise "Stale Status Notice";
  - two clinic pages with the same address and no `moved_to` must raise "Duplicate Clinic Address";
  - `3989 Boston Street Baltimore, MD 21224` and `560 W 800 N, Suite 121 Orem, Utah 84057` must be extracted as addresses;
  - `/<clinic>/botox/` must be `clinic_service`.
- **Assert on specific URLs, never on fixed totals.** There's no "expect 38". The count must come from the data.

**Done when** the F2F re-crawl gives this classification:
- **Utility pages:** all 43 thank-you pages are `utility_page`.
- **Directory hubs:** `/locations/`, `/ideal-image-locations/` and `/maryland/` are `directory_hub`.
- **Clinics:** every clinic slug linked from the directory body is `clinic_location`, with these statuses (checked against the live pages on 2026-11-05):
  - `/st-paul-grand-ave/` → `moved`, `moved_to = /woodbury/`;
  - `/towson/`, `/arundel-mills-hanover/` → `moved`, `moved_to = /canton-crossings/`;
  - `/canton-crossings/` → `ownership_transferred`;
  - `/woodbury/`, `/little-rock/` → `temporarily_closed`, each with "Stale Status Notice";
  - `/orem/` → `open`, `relocated = true`;
  - `/strongsville/`, `/mayfield-heights/`, `/cuyahoga-falls/` → `open` (the memberships "Coming soon" is ignored).
- **Schema rule:**
  - "Missing LocalBusiness" is raised on open and unknown clinics, including Orem and the three Ohio clinics.
  - It is **not** raised on the moved, temporarily closed and ownership-transferred pages listed above. They get the info issues instead.
  - Assert on these URLs; don't assert a total.
- **No "Duplicate Clinic Address"** is raised for the Woodbury or Canton groups, because the `moved_to` links explain them.

---

### WS5: Prototype and staging detection (D5)

**Changes (moved into `audit_site_context`):**
- **Token matching on whole slug segments only.** Split the path on `/` and `-` and match `demo`, `old`, `new`, `test`, `staging`, `temp`, `draft`, `backup`, `prototype`, `sandbox` and `copy`. Matching:
  - **Don't match** substrings (`testimonials`, `newest`, `bold`), title text or blog posts (`page_type = blog_post`).
  - **Do match** sequences like `test-page`.
  - **Configurable** allowlist and token list.
- **Severity:**
  - **error** — indexable, and either in an XML sitemap or linked internally;
  - **warning** — indexable only;
  - **info** — noindex.
- **New check: "Staging Hostname Reference".** Flag internal links, canonicals, `og:url` and hreflang that point at staging-like hosts. The patterns are configurable, with defaults `staging*`, `*.on-forge.com`, `*-staging.*` and `dev.*`.

**Tests**
- **Must flag:** `/careers-old/`, `/memberships-new/`, `/full-locations-demo/` and `/map-test-page/`.
- **Must not flag:** `/reviews-and-testimonials/`, `/blog/new-location-at-promenade-at-crocker-park-in-westlake-ohio/` and `/blog/congratulations-to-our-newest-…/`.
- A canonical pointing at `staging-2026.example.com` is flagged.

**Done when** the F2F re-crawl flags exactly those 4 prototype pages, with no false positives.

---

### WS6: Honest performance metrics (D7)

**Changes**
- **Re-label the rule** as **"Slow Crawl Response (crawler-measured)"**, and make it **info** by default. It's a warning only when timings come from the sequential sampling pass below.
- **Optional `--timing-sample N`:** after the crawl, fetch N URLs sequentially (concurrency 1, cache-busting query string, with `cf-cache-status` recorded) and report median and p95 time-to-first-byte separately from the crawl timings.
- **Optional `--psi-api-key KEY --psi-sample N`:** pull Chrome UX Report field data (TTFB, LCP, INP, CLS; origin plus sample URLs) from the PageSpeed Insights API into a "Real-User Performance" report section. Without a key, the report says "not measured".
- **Report wording:** crawl timings carry the note "reflects crawler concurrency and network path, not user experience".

**Tests:** default severity is info; the sampling pass runs sequentially (mocked client); the PSI client is mocked and its absence handled gracefully.

---

### WS7: Internal links to redirects (D8)

**Changes**
- **New post-crawl issue: "Internal Link to Redirect"** (info, category Links), raised on each source page for internal targets that return 3xx.
- **Report summary:** grouped by target with source counts. A slash-normalisation group is shown separately from content redirects such as `/services/microneedling/` → `/services/micro-needling/`.

**Tests:** a link to `/x` that 301s to `/x/` gives an info issue in the slash group; a link to a 301 → 200 content redirect is listed under its target.

---

## 4. Sequencing

| PR | Contents | Why this order |
| :--- | :--- | :--- |
| **PR 1** | WS1 + WS2 | Correctness: no real defect should be hidden. Small and self-contained. |
| **PR 2** | WS3 (report command) | Stops the narrative contradicting the data straight away, even before classification exists. |
| **PR 3** | WS4 + WS5 | Classification, plus the rules that depend on it. The largest change. |
| **PR 4** | WS6 + WS7 | Severity and labelling improvements. |

**Each PR:**
1. Unit tests plus the full existing suite, with the 28 current tests updated where they encoded wrong behaviour.
2. A fresh F2F crawl with the standard settings into a new `artifacts_facetofacespa_<pr>/` folder.
3. A comparison with the previous artifact, checking that changes are only the intended ones.
4. A check against that PR's "Done when" criteria.

---

## 5. Compatibility and Migration

- **Database:** all schema changes are additive (`ALTER TABLE … ADD COLUMN`, guarded by a `PRAGMA table_info` check). Older databases still open, and the report degrades gracefully.
- **Artifacts:** the JSON, CSV and Excel exports gain fields and columns but lose none. Downstream skills that read existing fields aren't affected.
- **Config:** new flags default to current behaviour, except where current behaviour is a defect: link resolution (WS1), transport-broken links (WS2), and the timing severity default (WS6). Call these out in the changelog.

---

## 6. Risks and Open Questions

1. **Corporate-phone threshold (WS4):** the share of pages above which a number counts as corporate (proposed 40%). Tune it on the F2F data and one other site.
2. **Directory data loaded with JavaScript:** on other sites the store locator may load its list with JavaScript, so body links won't list every clinic. `--locations-file` is the fallback, and FlareSolverr rendering of the directory page could be an option.
3. **Status markers are site-specific in wording.** Defaults cover common English phrases, and `--status-markers` allows overrides. On-page notices are the least reliable source, so prefer `--locations-file` or directory labels where a site has them, and limit page matching to the opening section.
4. **Ownership-transferred locations need a decision from the business,** not from the crawler. For F2F, whether `/canton-crossings/` (now NPOC-owned) should carry Face to Face Spa schema is tracked in the F2F plan's addendum.
5. **Stale-notice dates:** month-only phrases ("in October") are read as the most recent such month before the crawl date. Ambiguous phrases are reported as info, not warnings.
6. **Fixtures:** use synthetic HTML modelled on the F2F cases rather than saved client pages.
7. **Report template ownership:** should downstream skills (`local-seo-audit`, `lseo-doctrine`) consume `report_summary.json` directly instead of re-deriving numbers? That's recommended; their prompts may need updating.
