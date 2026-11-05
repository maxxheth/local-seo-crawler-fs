# Local SEO Crawl Report: `spa.example.com`

- Crawl date: `2026-11-05`
- Database: `crawl_results.db`
- Sample URLs per finding: 10
- Generated from the crawl database. Every number and URL below is a query result; anything that wasn't measured is reported as "not measured".

## Summary

- Pages crawled: 30
- Issues: 235 total (58 errors, 118 warnings, 59 info)
- Broken internal link targets: 3 (linked from 25 pages)
- Redirecting URLs crawled: 3
- Duplicate content clusters: 2 (5 pages)

| HTTP status | Pages |
| ---: | ---: |
| 200 | 25 |
| 301 | 3 |
| 404 | 2 |

A status of 0 means the request failed before any HTTP response (transport error).

## Issues by Type

Full list: Excel sheet *SEO Issues*.

| Severity | Issue | Category | Occurrences | Pages |
| --- | --- | --- | ---: | ---: |
| error | `Broken Internal Link` | `Links` | 26 | 25 |
| error | `Missing Meta Description` | `SEO` | 25 | 25 |
| error | `Broken Page (HTTP 404)` | `Technical` | 2 | 2 |
| error | `Indexable Prototype / Staging Page` | `Indexability` | 1 | 1 |
| error | `Malformed Relative Link` | `Links` | 1 | 1 |
| error | `Noindex Tag Present` | `Indexability` | 1 | 1 |
| error | `Staging Hostname Reference` | `Technical` | 1 | 1 |
| error | `Transport Error` | `Technical` | 1 | 1 |
| warning | `Thin Content` | `Content` | 25 | 25 |
| warning | `Title Too Short` | `SEO` | 25 | 25 |
| warning | `Missing Canonical Tag` | `Technical` | 24 | 24 |
| warning | `Missing Structured Data` | `Structured Data` | 24 | 24 |
| warning | `Missing LocalBusiness / Clinic Schema` | `Structured Data` | 6 | 6 |
| warning | `Duplicate Content Detected` | `Duplication` | 5 | 5 |
| warning | `Duplicate Clinic Address` | `Local SEO` | 2 | 2 |
| warning | `Indexable Prototype / Staging Page` | `Indexability` | 2 | 2 |
| warning | `Indexable Thank-You / Confirmation Page` | `Indexability` | 2 | 2 |
| warning | `Clinic Page Missing Visible Address` | `Local SEO` | 1 | 1 |
| warning | `Malformed Relative Link` | `Links` | 1 | 1 |
| warning | `Stale Status Notice` | `Local SEO` | 1 | 1 |
| info | `Missing OpenGraph Tags` | `Social` | 25 | 25 |
| info | `Missing Twitter Card Tags` | `Social` | 25 | 25 |
| info | `Closed/moved clinic page: confirm indexing and schema approach` | `Local SEO` | 2 | 2 |
| info | `HTTP 301 Redirect` | `Technical` | 2 | 2 |
| info | `Canonical Points to Different URL` | `Technical` | 1 | 1 |
| info | `Clinic Page Missing Visible Address` | `Local SEO` | 1 | 1 |
| info | `Internal Link to Redirect` | `Links` | 1 | 1 |
| info | `Ownership transferred: confirm LocalBusiness schema` | `Local SEO` | 1 | 1 |
| info | `Prototype / Staging Page` | `Indexability` | 1 | 1 |

| Category | Occurrences |
| --- | ---: |
| `SEO` | 50 |
| `Social` | 50 |
| `Technical` | 31 |
| `Structured Data` | 30 |
| `Links` | 29 |
| `Content` | 25 |
| `Local SEO` | 8 |
| `Indexability` | 7 |
| `Duplication` | 5 |

## Broken Internal Link Targets

Targets returning HTTP 4xx/5xx or failing at transport level (TLS, DNS, timeout), including redirects whose final hop fails. Full list: Excel sheet *Internal Links & Broken*.

| Error type | Targets |
| --- | ---: |
| `http_404` | 2 |
| `tls_hostname_mismatch` | 1 |

| Target | HTTP status | Error type | Linking pages | Sample sources |
| --- | ---: | --- | ---: | --- |
| `/privacy/` | 404 | — | 25 | `/`, `/aspen/`, `/birch-bay/`, `/blog/congratulations-to-our-newest-team/`, `/blog/new-location-at-oak-hill/`, `/book/`, `/careers-old/`, `/cedar-ridge/`, `/elm-park/`, `/full-locations-demo/` |
| `/blog/new-location-at-oak-hill/www.youtube.com/watch?v=abc` | 404 | — | 1 | `/blog/new-location-at-oak-hill/` |
| `/franchising/` | 0 | `tls_hostname_mismatch` | 1 | `/` |

## Malformed Relative Links

Links whose `href` has no scheme but looks like a domain name: 2. Browsers resolve these relative to the page, so they never reach the intended site.

- Resolved URL fails (severity error): 1
- Resolved URL works but misses the intended site (severity warning): 1

| Severity | Source page | href | Resolves to | Outcome |
| --- | --- | --- | --- | --- |
| error | `/blog/new-location-at-oak-hill/` | `www.youtube.com/watch?v=abc` | `/blog/new-location-at-oak-hill/www.youtube.com/watch?v=abc` | `HTTP 404` |
| warning | `/blog/new-location-at-oak-hill/` | `www.youtube.com/watch?v=ok` | `/blog/new-location-at-oak-hill/www.youtube.com/watch?v=ok` | `HTTP 301` |

## Redirects

| Pattern | Pages | Samples |
| --- | ---: | --- |
| Content redirect (different path) | 2 | `/blog/new-location-at-oak-hill/www.youtube.com/watch?v=ok` → `/blog/new-location-at-oak-hill/`, `/old-page` → `/oak-hill/` |
| Cross-domain | 1 | `/franchising/` → `https://spa-franchising.example.org/` |

## Internal Links to Redirects

Redirecting URLs linked internally: 1 (1 source/target pairs).

- Trailing-slash normalisation: 0 targets, 0 links
- Other redirects: 1 targets, 1 links

**Other redirects**

| Linked URL | Redirects to | Linking pages |
| --- | --- | ---: |
| `/old-page` | `/oak-hill/` | 1 |

## Duplicate Content Clusters

- Cluster 1 (3 pages): `/elm-park/`, `/oak-hill/`, `/pine-grove/`
- Cluster 2 (2 pages): `/aspen/`, `/spruce/`

## Page Classification

Per-page types: Excel sheet *Page Types*.

| Page type | Pages | HTTP 200 | LocalBusiness-family schema | Any schema |
| --- | ---: | ---: | ---: | ---: |
| `blog_post` | 2 | 2 | 0 | 0 |
| `clinic_location` | 10 | 10 | 1 | 1 |
| `clinic_service` | 1 | 1 | 0 | 0 |
| `directory_hub` | 2 | 2 | 0 | 0 |
| `other` | 12 | 7 | 0 | 0 |
| `service_page` | 1 | 1 | 0 | 0 |
| `utility_page` | 2 | 2 | 0 | 0 |

Directory hubs: `/locations/`, `/washington/`

## Clinic Locations

Clinic location pages: 10.

Clinic pages were detected as entries of the directory hubs above.

| Location status | Pages |
| --- | ---: |
| `moved` | 1 |
| `open` | 7 |
| `ownership_transferred` | 1 |
| `temporarily_closed` | 1 |

| Clinic page | Status | Moved to | Relocated | Evidence |
| --- | --- | --- | --- | --- |
| `/cedar-ridge/` | `temporarily_closed` | — | no | `directory_label` |
| `/harbor/` | `ownership_transferred` | — | no | `on_page_notice` |
| `/maple-st/` | `moved` | `/birch-bay/` | no | `on_page_notice` |
| `/willow/` | `open` | — | yes | `on_page_notice` |

## Prototype and Staging Pages

- Prototype or staging pages: 4
- Pages referencing staging hosts: 1

- error: `/careers-old/` (`Indexable Prototype / Staging Page`)
- warning: `/full-locations-demo/` (`Indexable Prototype / Staging Page`)
- info: `/map-test-page/` (`Prototype / Staging Page`)
- warning: `/memberships-new/` (`Indexable Prototype / Staging Page`)
- error: `/services/facials/` (`Staging Hostname Reference`)

## Server and Stack (Observed Headers Only)

Only values seen in response headers and generator meta tags are reported.

- `Server` header: not observed
- Cache header: not observed
- Generator meta tag: not observed

## Performance

Crawl timings are crawler-measured: they reflect crawler concurrency and network path, not user experience.

- Crawler-measured fetch time: not measured
- Crawler-measured TTFB: not measured
- Sequential TTFB sample: not measured
- Real-user field data (CrUX): not measured

