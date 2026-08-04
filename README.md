# Phoenix Pitching Lab — Player Progress Tracker

A website for tracking your pitching students' progress: upload stat CSVs, upload video clips, leave feedback via comments, and see each player's trends on their own page.

## 1. Install (one-time)

You need Python 3.9+ installed. Then, in a terminal, from this folder:

```
pip install -r requirements.txt
```

## 2. Run it

```
python app.py
```

Then open **http://127.0.0.1:5000** in your browser. Leave the terminal window open while you use the site — closing it stops the server. Press `Ctrl+C` in the terminal to stop.

Your data (players, stats, videos, comments) is saved in this folder — `pvtracker.db` for the database, and `static/uploads/` for videos and photos. Back up this whole folder to back up everything.

## 3. The site password

The whole site is behind a single shared password so it's not wide open to the internet. It's set in `app.py`:

```python
SITE_PASSWORD = os.environ.get("PHX_SITE_PASSWORD", "RiseUp2026")
```

**To change it:** open `app.py` in any text editor, find that line near the top, and edit the text between the quotes (the second one, after the comma).

Give the password to anyone you want on the site — that's all they need, no account required. Anyone can log out with the "Log Out" button in the top right.

## 4. Using it

**Add players** first (Roster → Add Player). Name, jersey number, position, grad year, and a photo are optional except name. You can edit any player's info later from their page ("Edit Info").

**Import stats** (Import Stats CSV): upload any CSV with a `Player` column (must match a roster name, e.g. "Jake Thompson") and ideally a `Date` column. Every other numeric column is tracked automatically as its own stat — ERA, IP, K, BB, WHIP, pitch count, strikes, velo, or any drill/skills score you use. Blank cells, or cells that just say `NULL`, `N/A`, `-`, or `none`, are skipped rather than imported as zero.

Pick a **session type** on the upload form — Bullpen, Pulldown, Game, Live BP, Flat Ground, Practice, or a custom one — so those stats stay grouped separately on the player page (a bullpen ERA-equivalent stat won't get mixed in with game stats, for example).

**Strike %** is calculated for you: include `Strikes` and `Pitches` (or `Total Pitches`) columns and the importer adds a Strike % stat automatically, no need to compute it yourself.

**ERA and K/7** are calculated for you too, on the **7-inning high school basis** (not MLB's 9-inning basis): include `IP` and `ER` columns and the importer adds an `ERA` stat computed as `(ER ÷ IP) × 7`, replacing any raw `ERA` column you included so there's no conflicting duplicate. Include `IP` and `K` columns and it also adds a `K/7` stat (`(K ÷ IP) × 7`) alongside your raw `K` count. Baseball's innings-pitched notation is handled correctly — `5.1` means 5⅓ innings (one out into the 6th) and `5.2` means 5⅔ innings, not literal tenths.

**Per-pitch velo**: add one column per pitch a pitcher actually throws in that session, e.g. `FB Velo`, `SI Velo`, `CT Velo`, `SL Velo`, `SWP Velo`, `CB Velo`, `CH Velo`, `SPL Velo` (four-seam, sinker/two-seam, cutter, slider, sweeper, curveball, changeup, splitter). Leave a pitch's column blank on days it wasn't thrown — blank cells are ignored, not treated as a 0 mph pitch.

Three sample files are in `sample_data/`: `sample_pitching_stats.csv` (game outing), `sample_bullpen_stats.csv` (Strikes/Pitches + per-pitch velo, including some blank/N/A cells), and `sample_pulldown_stats.csv` (max-effort velo work).

You can re-import as often as you like — each upload adds new dated rows, building a history per stat, which is what powers the trend charts on each player's page.

**Add videos** (Add Video): pick a player, date, and file (MP4/MOV/M4V/WEBM/AVI), add a title and notes (e.g. "Bullpen, working on staying tall through release"). It shows up in that player's video timeline, playable right in the browser.

**Comments**: anyone who's logged in with the site password can leave feedback — either on a specific video clip (click "Comments" under that clip) or general thoughts on a player (the "Player Feedback" section at the bottom of their page). Commenters just type their name each time; there are no separate logins. Any comment can be deleted from the same spot it was posted.

**Player pages**: under "Progress Over Time," stats with "Velo" in the name (FB Velo, Velo, SL Velo, etc.) get a line chart with Bullpen, Pulldown, and Game as separate colored lines together on one chart, so you can see whether velo is trending up or down in each context.

Below that, every stat (including velo) is laid out as a spreadsheet — one table per session type (Bullpen, Pulldown, Game, and any others you've used), stats as columns, dates as rows, with a bold AVG row at the bottom of each column, just like a totals row in Excel. If the same date/stat gets re-imported, the newest value wins rather than showing duplicate rows. Plus their video history newest-first with comment threads.

**Calendar** (Calendar in the nav): one shared calendar for the whole lab — a normal month grid, click Prev/Next to move between months. Add a lesson day with a date and a message, where you write whatever's useful (a player's name, session type, time — anything). Click the &times; on any entry to remove it.

**Manage Uploads** (Manage Uploads in the nav): every CSV upload is listed as its own row — file name, session type, when it was imported, dates covered, and row count — with a Delete button that removes just that one import (handy for undoing a duplicate or bad upload without touching anything else). Below that is every video across the whole roster with its own Delete button, so you don't have to hunt through individual player pages to clean things up.

## Troubleshooting

**Velo charts look blank:** they load a small charting library from the internet (jsdelivr, with unpkg and cdnjs as backups). If your network blocks all three — rare, but school/work firewalls sometimes block one CDN — the velo cards will just show without a chart. Nothing else on the page depends on this; every other stat is a plain table with no internet dependency at all.

## Notes on video file size

There's no upload size limit on this app (raised to 1 GB per file), but very large video files will take a while to upload and will use disk space on this computer. If you're recording a lot of video, periodically check the size of `static/uploads/videos/`.

## Sharing this with players, parents, or other coaches

Right now this only runs on your computer (`127.0.0.1` means "this machine only") — nobody else can reach it over the internet yet. To let others view and comment from their own devices, it needs to be deployed to a host that keeps the app running and keeps your uploaded videos around permanently. A few things to know when you're ready for that step:

- **Pick a host with persistent storage**, not just a "free web service." Plain free tiers on hosts like Render or Railway wipe the filesystem on every restart/redeploy, which would delete your videos and database. Look for a host with an attached persistent disk/volume, or plan to move video storage to something like an S3-compatible bucket. PythonAnywhere's free tier has persistent storage and is one of the simpler options to set up by hand (though its free tier caps total storage at 512 MB, which fills up fast with video — the paid Developer tier gives 5 GB and full internet access for chart rendering).
- **Keep the password gate on** (already built in) — since this involves video of players, don't make the site fully public even once it's online; only share the password with people you trust.
- **The site password isn't strong security** — it's a shared secret, good enough for a small lesson business, but anyone you give it to could pass it along. If that becomes a concern later, per-person logins would be a bigger follow-up project.

## Secrets and config

Two values have safe local defaults baked into `app.py`, but should be set as **environment variables** on any host you deploy to (rather than left as the defaults), so a copy of this code sitting on GitHub doesn't reveal your live password or session key:

- `PHX_SITE_PASSWORD` — the shared site password (defaults to `RiseUp2026`)
- `PHX_SECRET_KEY` — used internally by Flask to sign login sessions (defaults to a fixed placeholder string)

Most hosts (Render, Railway, PythonAnywhere, Fly.io, etc.) have a place in their dashboard to set environment variables for your app — set both there once you deploy.

## Putting this on GitHub

If you push this to GitHub, keep the repo **private** — `.gitignore` already excludes `pvtracker.db` (your real roster/stats) and everything in `static/uploads/` (real videos/photos), so that data stays local and never gets committed. Only the app code, templates, styling, and the sample CSVs go up.

Pushing to GitHub does not make the site itself viewable to anyone — GitHub only stores code, it doesn't run the Flask app. To let anyone else actually use the site, you still need to deploy it to a host as described above.
