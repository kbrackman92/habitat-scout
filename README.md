# Toulouse Habitat Scout

Daily-updated apartment hunting tool for Toulouse: scrapes Leboncoin and SeLoger
listings ≤ 160 000 € near university metro stations, with price-vs-market
indicators. Runs entirely on GitHub's free tier.

## How it works

Three pieces, all in this repo:

1. **`scrape_daily.py`** — Python scraper. Pulls Leboncoin and SeLoger's Toulouse
   apartments-for-sale pages, extracts the embedded Next.js JSON state, filters
   to ≤ 160 000 €, writes `listings.json`.
2. **`.github/workflows/scrape.yml`** — GitHub Action that runs the scraper
   once a day, commits `listings.json` back to the repo.
3. **`index.html`** — the front-end. Fetches `listings.json` on load, renders
   cards with real listing URLs.

GitHub Pages serves all three files. Visit `https://YOUR-USERNAME.github.io/REPO-NAME/`
and you see today's listings.

## Setup (≈ 10 minutes)

### 1. Create the repo

- Create a new public GitHub repo (let's call it `habitat-scout`).
- Upload the four files from this folder: `index.html`, `scrape_daily.py`,
  `listings.json`, `.github/workflows/scrape.yml`.

### 2. Enable GitHub Pages

- Repo → **Settings** → **Pages**.
- Source: **Deploy from a branch**, branch `main`, folder `/ (root)`.
- Save. After ~1 min the site is live at `https://YOUR-USERNAME.github.io/habitat-scout/`.

### 3. Allow the Action to commit back

- Repo → **Settings** → **Actions** → **General**.
- Under "Workflow permissions", select **Read and write permissions**. Save.

This lets the daily workflow push the updated `listings.json` to the repo.

### 4. Test the workflow

- Repo → **Actions** tab → click "Daily scrape" → **Run workflow**.
- Wait ~3 minutes. If it succeeds, `listings.json` is updated and the site
  shows real listings.
- If it fails (most often: Datadome blocked the GitHub IP), it will say so in
  the log. The `listings.json` from your last successful run is still served,
  so the site doesn't break.

The schedule is `30 6 * * *` (06:30 UTC = 07:30 Paris in winter, 08:30 in
summer). GitHub's cron is best-effort and often runs 10-30 min late.

## Realistic expectations

GitHub Actions runs from datacenter IPs that Datadome flags more aggressively
than residential IPs. Roughly:

- **Leboncoin**: ~50% success rate from GitHub's IPs
- **SeLoger**: ~25% success rate

Some days both will work, some days only Leboncoin, some days neither. When
nothing works, yesterday's `listings.json` keeps serving — the site degrades
to "yesterday's data" rather than going blank.

If you find the success rate too low, two options:

1. **Run the scraper at home instead.** Get a Raspberry Pi, run the same
   `scrape_daily.py` via cron, and `git push` the result to this repo. Home
   IPs have ~80% success rates.
2. **Use email alerts as a complement.** Set up Leboncoin saved searches with
   email alerts (free, sanctioned, real-time). Use the scraper as a periodic
   sweep for completeness.

## Maintenance

When a portal redesigns its front-end, the scraper's JSON walker may pull in
junk or miss listings. Symptoms: workflow succeeds but listings count drops
suddenly, or shows weird titles/prices. Open `scrape_daily.py`, look at the
`normalize_leboncoin` / `normalize_seloger` functions, adjust the field names
to match the new schema. Realistic: once or twice per year, ~30 min each time.

## Cost

€0. GitHub Actions gives 2 000 free minutes/month for public repos; daily
scrapes use ~3 minutes each = ~90 min/month.
