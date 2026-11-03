# Southwest 7-Day Fire Potential Verification

Archives the Predictive Services 7-Day Significant Fire Potential forecast for every
Southwest PSA, every day, all seven lead days. It then checks observed weather against the
Wind and Burn Environment criteria (which you edit and switch from the website), lets you confirm
Lightning days by hand, and verifies against recorded fires. The site has a light/dark switch.

- `scripts/archive_forecast.py` pulls Day 1 to Day 7 from the NPSG `outlooks_forecast` service.
- `scripts/verify_weather.py` checks the previous days' observed weather PSA by PSA.
- `config/criteria.json` holds every threshold. Edit it from the website's **Criteria** tab.
- `scripts/apply_criteria.py` checks and saves criteria changes sent from the website.
- `.github/workflows/archive.yml` runs the archive and weather check every night at about 3 am Mountain.
- `.github/workflows/apply-criteria.yml` applies criteria changes sent from the website.
- `index.html` is the GitHub Pages site.
- `data/forecasts.csv` is the forecast archive (one row per issue date × lead day × PSA).
- `data/observed.csv` is the weather check (one row per PSA per day, with the stations that hit).
- `data/fires.csv` is where fire occurrence goes (you edit this).
- `data/lightning.csv` is where confirmed lightning days go (you edit this).
- `data/raw/` and `data/obs_raw/` keep the untouched data from each pull, for auditing and re-scoring.

## One-time setup

1. **Create the repo.** On GitHub: New repository → name it (for example `sw-7day-verification`) → Public → Create.
2. **Upload the files.** On the new repo page choose "uploading an existing file", drag in
   everything from this folder, and commit. Make sure the hidden `.github` folder comes along
   (on a Mac, press Cmd+Shift+. in Finder to show it). It holds two workflow files,
   `archive.yml` and `apply-criteria.yml`; both are needed.
3. **Let the Action write to the repo.** Settings → Actions → General → Workflow permissions →
   "Read and write permissions" → Save.
4. **Turn on the website.** Settings → Pages → Branch `main`, folder `/ (root)` → Save.
5. **Run it once now.** Actions tab → "Archive 7-Day Fire Potential" → Run workflow.

After that it runs on its own every night. The first run also back-checks the weather for the
previous 10 days.

## Schedule

Everything runs at about **3 am Mountain (New Mexico) time, year-round**. The forecast is
issued between 7 am and 12 pm, so the 3 am pull captures the previous day's finished forecast and
saves it under that day's issue date. (Any pull before 7 am Mountain is treated this way.) The
weather check at the same time covers the day that just ended.

GitHub schedules run in UTC and don't follow daylight saving, so the workflow is scheduled at both
09:05 and 10:05 UTC and skips any run that lands before 3 am local. That way it runs at 3:05 am in
both summer (MDT) and winter (MST). A backup run at 12:05 UTC (5–6 am) only saves if the earlier
runs failed. GitHub sometimes starts scheduled runs 10–30 minutes late, which doesn't matter here.

Manual runs (Actions tab → Run workflow) go right away. A manual run after 7 am saves today's
forecast; if today's isn't issued yet, wait until it is. If the product is re-issued and you want
the archive to hold the new version, run the workflow manually with **force** checked.

## How observed High Risk is decided

Each RAWS station inside a PSA is checked on its own. A PSA meets a trigger when enough stations
hit it: normally 2, or 1 for PSAs with 3 or fewer reporting stations. It does not use the PSA average.

| Trigger | Counts when (starting values) |
|---|---|
| **W - Wind** | An hour with sustained wind ≥ 20 mph and RH ≤ 15%, or 2+ hours with gusts ≥ 35 mph and RH ≤ 15% |
| **B - Burn Environment** | Afternoon max temp ≥ 5°F above typical peak-season max with RH ≤ 15%, plus Haines ≥ 5 from the nearest 00Z sounding |
| **L - Lightning** | Confirmed by hand in `data/lightning.csv` (not automatic) |

**B?** means the station part was met but no sounding was available.

### Changing the criteria from the website

Open the site's **Criteria** tab. You can:

- **Edit any number** in a criteria set (wind speeds, gusts, RH, Burn Environment temperature,
  Haines, how many stations must hit).
- **Keep several named sets**, for example "Standard" and "Hot season" (needing less wind when it's
  hot). Add, duplicate, rename or delete sets.
- **Switch which set is in use** starting on any date you pick, with an optional reason. The history
  of switches is kept, and each day is checked with the set that was in use that day. Pick a past
  start date to re-check earlier days with the new set.
- **Set the typical peak-season max temperature** for each PSA (used by Burn Environment). Blank
  means it's estimated from station elevation.

When you click **Save on GitHub**, GitHub opens with your changes filled in as a new issue.
Click **Submit new issue**. An Action then:

1. checks every number is sensible (it rejects anything out of range and explains why),
2. saves `config/criteria.json`,
3. re-checks all saved days with the updated criteria,
4. replies on the issue with what changed, and closes it.

The site shows the new criteria in about 1–3 minutes. Only people with write access to the
repository (you, or collaborators you add) can change criteria this way; anyone else's request is
rejected automatically. You need to be signed in to GitHub in the same browser.

The "Hot season" set ships as an example with placeholder numbers. Adjust it before using it.
Each row in `data/observed.csv` records which criteria set was used.

### Confirming lightning

Lightning isn't checked automatically. On the website's **Criteria** tab, under **Lightning days
observed**, pick a date, tick the PSAs that hit (PSAs with an L forecast that day are marked),
add an optional note, click **Add lightning day**, then **Save on GitHub** and **Submit new issue**.
The days are written to `data/lightning.csv`. Click × on a recorded PSA to remove it. Confirmed
days count as observed High Risk on the Weather check and Scorecard tabs.

## Map

The **Map** tab draws the Southwest PSAs (from `data/psa_polygons.geojson`, saved by the
nightly weather check) and colors them three ways:

- **Forecast as issued** for any date and forecast day, in the 7-day map colors, with observed
  triggers ("obs W") and fires marked. Use ‹ › to step through days or **Play** to animate.
- **Outcome**: hit (blue, H), false alarm (amber, F), miss (magenta, M) or correct quiet (gray).
- **Season score by PSA** over a date range: skill, events caught, false alarm ratio, bias
  (blue = under-forecasting, orange = over-forecasting), number of events or yes forecasts.

Hover a PSA for details; click it to open that PSA's scorecard.

## Analysis

The **Analysis** tab has four views, all for the date range and forecast day you pick:

- **Reliability by conditions:** forecasts grouped by observed ERC, minimum RH or peak gust,
  showing how often an event happened and how often "yes" was forecast in each group.
- **Calendar:** one row per PSA, one square per day, colored by outcome, to spot runs of misses
  or false alarms.
- **Forecast consistency:** how often the category changed between Day 7 and Day 1, and how
  many High Risk flip-flops each PSA had.
- **Season trend:** weekly skill (CSI) and bias.

ERC comes from FEMS NFDRS outputs collected each night (the median of the PSA's stations' daily
maximum). It uses NFDRS 2016 fuel model **Y** unless you add `"erc_fuel_model": "X"` (or V, W, Z)
to `config/criteria.json`. ERC is collected from now on; days checked before this update show
no ERC.

## Exporting

The **Export** tab creates, for any date range and any set of PSAs:

- **Excel workbook (.xlsx):** a sheet for each section, with category colors.
- **PDF report:** opens the print window; choose "Save as PDF" as the destination.
- **Data (CSV):** one row per PSA forecast with what happened (hit, miss, false alarm,
  correct quiet), for use in other tools.

Sections you can include: scorecard (hits and misses, by category with chart, by lead time,
by PSA), scores by month, misses and false alarms to review, the color-coded forecast grid,
forecast evolution (Day 7 → Day 1), observed weather check, lightning days, fires, the criteria
used, and (Excel only) every archived forecast. Quick picks cover the last 7 days, last 30 days,
this year, or everything.

### Other limits

- **Burn Environment temperature:** the typical peak-season max is estimated from each station's
  elevation (about 106°F at 1,000 ft, dropping 3.5°F per 1,000 ft). For better results, enter a
  value per PSA on the Criteria tab.
- **Little/Low/Moderate risk levels** depend on fuels (ERC and other indices), so the weather
  check covers the High Risk triggers only.

### Data sources (all public, no login)

- **RAWS hourly observations:** FEMS Climatology API. Public access covers about the last two
  weeks, which is why the check runs daily and saves what it pulls.
- **Soundings (Haines):** Iowa Environmental Mesonet upper-air archive: ABQ, EPZ, FGZ, TUS, AMA, MAF, GJT.
- **PSA boundaries:** the NPSG 7-day service, cached in `data/psa_polygons.geojson`. The station
  list (`data/stations_raws.csv`) is refreshed every 30 days.

## Recording fires

Edit `data/fires.csv` on GitHub (open the file → pencil icon → commit). One row per fire:

```
date,psa,fire_name,acres,cause,notes
2026-11-05,SW05,Example Fire,1250,Lightning,
```

`date` is the discovery date in Mountain time. `psa` must match the PSA code (SW01, SW02, ...).

## Scoring

Each PSA-day is one forecast/observation pair, scored separately for Day 1 through Day 7.
On the Scorecard you choose what counts as the event: significant fires, observed High Risk
weather, or either. The page reports POD, FAR, CSI and bias by lead day, and the event rate
for each Day 1 category.

## If something fails

The Actions tab shows a red X on any run with a failed step. Click into the run, open the
failed step, and read the message. Results from the parts that worked are still saved, and
missed weather days are retried automatically for up to 10 days. `data/verify_log.csv` keeps a
line for each day checked.

## Notes

- The service names the GACC field `gacc` on Day 1 and `gacc_name` on Days 2 to 7; the script handles both.
- `valid_date` is computed as issue date + lead day − 1. The raw `service_timestamp` is also stored.
- To use a different GACC, set `PSA_PREFIX` (for example `NR`) as an environment variable in the workflow.
