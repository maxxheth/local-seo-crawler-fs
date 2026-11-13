# Plan: Google API access for the local SEO crawler (workstation, keyless)

**Date:** 2026-11-09
**Status:** Plan only. Nothing has been configured yet.
**Runs from:** local workstation (not AWS)
**APIs:** Google Analytics (GA4 Data API), Google Search Console API, Google Business Profile APIs

## Why this approach

The marvenio.com Google Cloud org enforces `iam.disableServiceAccountKeyCreation`, so we can't
download service account JSON keys. Workload Identity Federation (WIF) isn't needed either. WIF is for
workloads that have no Google identity of their own. On a workstation, the crawler can use a person's
own Google sign-in through **Application Default Credentials (ADC)**. No service account key gets
created or stored.

## Verified state (read-only checks, 2026-11-09)

| Item | Finding |
|---|---|
| Google Cloud org | `marvenio.com`, org ID `700750643344` |
| admin@marvenio.com org role | Organization Administrator |
| admin@marvenio.com project role | Owner on `marvenio-workspace-admin` |
| Service account key creation | Blocked (`iam.disableServiceAccountKeyCreation` active) |
| Service account creation | Allowed (`iam.managed.disableServiceAccountCreation` inactive) |
| Domain restricted sharing | Active, scoped to the Workspace customer ID |

Not checked yet: access levels in GA4, Search Console and Business Profile.

## Steps (about 6, plus a separate Business Profile track)

### 1. Choose the project and enable APIs
- [ ] Pick a project. Option: create a dedicated one, e.g. `edfx-seo-crawler`, instead of reusing
      `marvenio-workspace-admin`.
- [ ] Enable `analyticsdata.googleapis.com` (GA4 Data API)
- [ ] Enable `searchconsole.googleapis.com` (Search Console API)
- [ ] Optional: enable `analyticsadmin.googleapis.com` if the crawler lists accounts or properties

### 2. Configure the OAuth consent screen (Google Auth Platform)
- [ ] Audience: **Internal**, if everyone who signs in uses a marvenio.com account. That avoids Google
      app verification.
- [ ] If set to External, publish to **Production**. Apps left in Testing get refresh tokens that
      expire after 7 days.

### 3. Create an OAuth client
- [ ] Application type: **Desktop app**
- [ ] Download the client JSON and keep it outside the repo, e.g. `~/.config/edfx/oauth-client.json`
- This step is required because Google blocks the Analytics and Search Console scopes on gcloud's
  built-in OAuth client ("This app is blocked").

### 4. Sign in once on the workstation
```bash
gcloud auth application-default login \
  --client-id-file="$HOME/.config/edfx/oauth-client.json" \
  --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/analytics.readonly,https://www.googleapis.com/auth/webmasters.readonly
```
- [ ] Sign in with an account that already has read access to the target GA4 and Search Console properties.
- The resulting file is `~/.config/gcloud/application_default_credentials.json`. It holds a refresh
  token, so treat it as a secret and never commit it.

### 5. Set the quota project
```bash
gcloud auth application-default set-quota-project <PROJECT_ID>
```

### 6. Wire up the crawler and test
- [ ] Load credentials with `google.auth.default(scopes=[...])` (Python). The GA4 and Search Console
      client libraries pick up ADC automatically.
- [ ] Don't set `GOOGLE_APPLICATION_CREDENTIALS` unless the ADC file lives somewhere non-default.
      If you do set it, use an absolute path.
- [ ] Add any new env vars (property IDs, site URLs) to `.env.example` only, never to committed config.
- [ ] Smoke test with one read-only call per API: a 1-day GA4 report, and a Search Console
      `sites.list`.

## Google Business Profile: separate track (start early)

- The Business Profile APIs aren't open by default. Submit **"Application for Basic API Access"**
  (https://support.google.com/business/contact/api_default) with the chosen project's **number**.
  Approval is per project and can take days to weeks. Quota shows 0 QPM until approved.
- [ ] Submit the access request for the same project as step 1
- [ ] After approval, enable the Business Profile APIs (Account Management, Business Information,
      Performance, and Google My Business v4.9 for reviews and posts)
- [ ] Repeat step 4 with `https://www.googleapis.com/auth/business.manage` added to `--scopes`
- [ ] The signing-in account must be an owner or manager on the Business Profiles

## Privileges needed

- **Steps 1–3:** project Owner. admin@marvenio.com already has this, and marvenio.com users can create
  projects.
- **Step 4 account:** read access is enough. Any user level on GA4 and Search Console, plus
  owner or manager on Business Profiles.

## Alternative for a shared team setup

Each person's ADC impersonates a shared service account that has no key:
`gcloud auth application-default login --impersonate-service-account=<SA_EMAIL> --scopes=...`
- Pros: access belongs to the service account, not to individuals.
- Cons: the service account must be added as a user in GA4 (needs Admin there) and in Search Console
  (needs Owner there). It doesn't work for Business Profile.

## Open decisions
- [ ] Dedicated project vs. `marvenio-workspace-admin`
- [ ] Which Google account signs in (admin@marvenio.com or a personal work account)
- [ ] Solo ADC vs. shared impersonation
