# Southwest 7-Day Fire Potential Verification

Archives the Predictive Services 7-Day Significant Fire Potential forecast for every
Southwest PSA, every day, all seven lead days. It then checks observed weather against the
Wind and Burn Environment criteria (which you edit and switch from the website), lets you confirm
Lightning days by hand, and verifies against recorded fires. The site has a light/dark switch.

- `scripts/archive_forecast.py` pulls Day 1 to Day 7 from the NPSG `outlooks_forecast` service.
- `scripts/verify_weather.py` checks the previous days' observed weather PSA by PSA.
- `config/criteria.json` holds every threshold. Edit it from the website's **Criteria** tab.
- `scripts/apply_criteria.py` checks and saves criteria changes sent from the website.
- `.github/workflows/archive.yml` runs the archive and weather check every evening.
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

After that it runs on its own every evening. The first run also back-checks the weather for the
previous 10 days.

## Schedule

The forecast is issued between 7 am and 12 pm Mountain, so everything runs at about
**6 pm Mountain time, year-round**. GitHub schedules run in UTC and don't follow daylight saving,
so the workflow is scheduled at both 00:05 and 01:05 UTC and the archiver skips any run that lands
before 6 pm local. A backup run at 03:05 UTC only saves if the earlier runs failed.

Manual runs skip the 6 pm check. If the product is re-issued and you want the archive to hold the
new version, run the workflow manually with **force** checked.

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

Lightning isn't checked automatically. The Weather check tab lists Lightning forecasts waiting
for review, with how many stations were dry enough (RH below 20%) as context. When a day verifies,
add it to `data/lightning.csv` on GitHub:

```
date,psa,notes
2026-07-14,SW05,scattered dry storms, RH 12-18%
```

The site picks it up right away; no rescore needed. Confirmed days count as observed High Risk on
the Weather check and Scorecard tabs.

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
