# Cron US Fandango scraper process

This guide describes the GitHub Actions application entry point,
`fandango_noblockv4.py`, and its V2 publisher in `utils/analytics_v2.py`.
It explains the scrape pipeline, current/advance publication, cumulative
recovery, and how the scheduled-project integrations differ from the local
Fandango app.

## Configure and launch

Set `SHOW_DATE`, `GLOBAL_MOVIE_SLUG`, and `FORCE_UPDATE_ADVANCE` in
`fandango_noblockv4.py`. The selected movie's Fandango URL, language,
average/max ticket prices, EXTRA adjustment, poster, cover, and release date
are loaded from `data/movie_config.json`.

The script also configures target states, worker count, seat-count mode,
screen-level cached/physical overrides, theater merge IDs, and sold-out chain
filters. Firebase credentials and database URL are supplied through the
workflow environment/secrets. `OVERWRITE_SNAPSHOT` is environment-controlled
and defaults to `false`.

The repository has two GitHub Actions workflows: the ordinary scrape
workflow sets `OVERWRITE_SNAPSHOT=false`; the daily snapshot workflow sets it
to `true`. Their schedule triggers are currently commented out, so they run
when manually dispatched unless those triggers are enabled.

## End-to-end run

1. Initialize configuration and Firebase, load
   `state_theatre_mapping.json`, and enqueue theaters from configured states.
2. Read the published Firebase active run for previous-show reconciliation
   and previous-run comparisons. Read the daily comparison baseline separately
   for report momentum and daily comparison metrics.
3. Fetch theater showtimes and seat maps concurrently with Playwright. The
   worker groups formats/amenities, detects dead-room blocks, clusters
   overlapping maps, applies physical/cache capacity logic, and learns hash
   capacities.
4. Merge worker output, process sold-out review items, add configured EXTRA
   rows, consolidate configured multi-tier theaters, and apply sold-out-chain
   exclusions.
5. Reconcile with the previous Firebase active run (cumulative recovery;
   described below).
6. Generate Excel, HTML, and image reports in `reports/`. These are uploaded
   as GitHub Actions artifacts with a 30-day retention period; the scraper
   does not send the local app's email report.
7. If Firebase is initialized and show rows exist, publish newly learned hash
   entries, V2 analytics/detail pages, movie metadata, and refreshed USA/global
   portal views.

If credentials are unavailable, scraping and local report generation can
still proceed, but there is no previous Firebase run to reconcile and no
analytics/portal publication. If no show rows were produced, reports and
analytics publication are skipped.

## Seat/map processing outcomes

- Successful seat maps are grouped by theater, showtime, and amenity
  signature. Map overlap and shared seat geometry are used to merge duplicate
  tiers; shared available seats prevent a false merge.
- Physical seat dots, cached area counts, configured screen overrides,
  front-row heuristics, dead-room detection, and learned hash capacities are
  combined to estimate capacity, booked tickets, and gross.
- Fully sold-out map failures may be repaired from cached hash capacity. An
  unrecoverable sold-out tier enters the manual-review queue rather than
  inventing a measured result.
- Available map failures produce an `Available (Map Error)` row. If a cluster
  has valid tiers as well as a failed tier that was marked available, the
  cluster's final status remains `Available (Map Error)` so recovery can spot
  potentially incomplete measurements.
- The all-failed standalone path can use a cached hash to reconstruct
  capacity/gross; the final status still reflects an available failed tier.

## Current run versus advance sales

The scraper uses the America/Chicago date (`us_today`) for the advance
decision. The shared analytics publisher also contains a same-day first-run
fallback:

| Target `SHOW_DATE` relative to today's Chicago date | `active_run_id` | `advance_run_id` |
|---|---|---|
| Before today | Updated to this run; the run contributes to current sales | Preserved unless `FORCE_UPDATE_ADVANCE=True` |
| Today, no previous active or advance pointer | Updated to this first same-day scrape | Initialized to this first same-day scrape |
| Today, a pointer already exists | Updated to this scrape | Preserved unless forced |
| After today | Updated to this run; it is the latest scrape but not yet current sales | Updated to this run as advance data |
| Any date with `FORCE_UPDATE_ADVANCE=True` | Updated to this run | Updated to this run |

Every successful Firebase publish makes the run active, whether the target
date is future, today, or historical. `active` means latest published scrape;
it does not itself mean the show date is already current. The date index's
`has_current` reflects whether `SHOW_DATE` is on/before the Chicago as-of date.
A same-day first run is also an advance snapshot only if neither pointer
exists; later same-day runs update active but preserve the existing advance
snapshot.

If no show rows are produced or Firebase publication is unavailable, no new
active/advance pointer is committed. `OVERWRITE_SNAPSHOT` controls the
Firebase daily comparison baseline; it does not control active publication or
the advance rule.

## Previous-run cumulative recovery

This is a post-scrape reconciliation pass, not a mode that skips scraping or
prevents the API from being queried again. It runs whenever previous active
show details were successfully loaded from Firebase.

The previous rows come from
`markets/usa/movies/<movie>/dates/<date>/published/active_run_id` and its
versioned detail pages. They are not the daily comparison baseline.

Matching and actions:

1. Match on theater ID + normalized showtime + normalized format.
2. Ignore previous EXTRA rows and `Fan Event / Manual` rows; configured
   adjustments are regenerated for the current run.
3. If current results contain a matching row that is not `Available (Map
   Error)`, trust the live result, including lower ticket/gross values.
4. If a matching current row is `Available (Map Error)`, replace it with the
   previous row's valid values and repair the theater summary totals.
5. If a previous row is missing from current results, parse its Fandango
   showtime in that state's timezone. Drop it only when it is strictly more
   than one hour in the future. Otherwise restore it and add its totals back
   into the theater summary. If parsing/timezone handling cannot establish
   that it is far in the future, retain it conservatively.

The reconciliation runs for future-date as well as same-day scrapes. It does
not preserve all future shows: a missing show more than one hour away is
dropped, while a missing past/imminent show is rescued. When the first run has
no Firebase active pointer, there is no prior show list to reconcile.

## Distinct baselines and snapshots

The cron application has two Firebase inputs used for different purposes:

- **Active Firebase run:** immediately previous successful scraper output;
  used for cumulative recovery and previous-run analytics comparisons.
- **Firebase daily comparison baseline:** independently selected baseline
  used for daily comparison pages and report momentum. The workflow's
  `OVERWRITE_SNAPSHOT` setting controls whether the successful run replaces it.

The V2 monthly `cumulative/v1` rollups are separate again: snapshots on/before
the Chicago as-of date update current metrics; future-date runs update advance
metrics when designated advance. They aggregate dated snapshots and are not
the previous-run show-list recovery algorithm.

## How this app differs from the local Fandango app

The core theater/show grouping and seat calculation follow the same design,
including the same D-BOX/base-tier routing and the same `Available (Map Error)`
status for available failed tiers. The operational differences are intentional:

- Cron reads/writes hash capacities in Firebase; local additionally loads and
  saves a local cache and merges it with Firebase.
- Cron uses workflow-supplied Firebase configuration and fixed USA paths;
  local has optional Firebase and a configurable root path.
- Cron uses environment-controlled `OVERWRITE_SNAPSHOT` and GitHub Actions
  artifacts. Local keeps a JSON momentum snapshot, currently enables its
  overwrite flag in code, and emails reports.
- Cron emits workflow/console logs and does not build the local app's
  per-run detailed log or rate-limit summary.

