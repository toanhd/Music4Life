# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **local, single-user web UI** that wraps two CLI tools — **spotDL** (Spotify) and **yt-dlp** (YouTube) — to download audio as mp3. FastAPI backend + a single vanilla-HTML/JS page. No build step, no framework on the frontend, no database. Output mp3s go to a **runtime-configurable folder** (default `downloads/`).

## Commands

All Python runs through the project venv (Python 3.14): `.venv/bin/python`.

```bash
./run.sh          # start server in background (port 8000) + open browser
./run.sh stop     # stop it (reads .server.pid, falls back to killing port 8000)
./run.sh log      # tail -f .server.log
.venv/bin/python app.py   # run in foreground (logs to stdout)
```

There is **no test suite, no linter, and no requirements file** configured. Dependencies were `pip install`ed directly into `.venv` (`fastapi`, `uvicorn`, `spotdl`, `yt-dlp`, `spotipy`); `ffmpeg` is a **system binary installed via Homebrew** (required for mp3 conversion). `deno` (via brew) is used by yt-dlp to solve YouTube JS challenges.

Smoke-test endpoints with curl, e.g. `curl -s localhost:8000/health | python -m json.tool`.

## Architecture

Everything backend lives in `app.py`; the entire UI is `static/index.html` (inline CSS + JS). Three request flows, each routing by input type via `classify()` (spotify URL / youtube URL / bare name):

- **Search** (`POST /search`, bare name) — YouTube via the **yt-dlp Python API** (`extract_flat`), Spotify via **spotipy**. Results are merged and shown for the user to pick.
- **Resolve** (`POST /resolve`, a URL) — preview before downloading. Returns a uniform `{ok, multi, items[], title?, note}` contract. YouTube uses yt-dlp `extract_info`. **Spotify uses Open Graph scraping of the public `open.spotify.com` page (`_spotify_og`), NOT the API** — so previews work even when the Spotify API is rate-limited. **Album/playlist URLs are expanded into individual tracks** → `multi: true`: Spotify via `_spotify_list_tracks` (scrapes `<meta name="music:song">` + OG-resolves each concurrently); YouTube via `_youtube_list_tracks` (yt-dlp `extract_flat`, capped at `playlistend=200`; the URL is normalized to `/playlist?list=<id>` first because the `watch?v=…&list=…` form returns zero entries). YouTube playlist detection: `/playlist` in URL or a `list=` param that isn't an `RD…` auto-radio/mix. Single YouTube videos use `noplaylist=True` and the URL is normalized to `youtu.be/<id>` so a stray `&list=` never expands; downloads also pass `--no-playlist`. The frontend (`renderMulti`, source-agnostic) pre-selects only non-duplicate tracks and offers "Toàn bộ" vs "Chỉ bài chưa có".
- **Sưu tầm / saved-for-later** (`GET /saved`, `POST /saved/add`, `POST /saved/remove`) — a persistent wishlist in `.music4life.saved.json` (deduped by URL). The "🔖 Lưu để tải sau" button saves the currently-selected items; the **Sưu tầm** tab lists them with bulk download/remove. `/saved` re-runs `annotate_in_library` on every call, and the frontend re-fetches it whenever the folder is (re)scanned or changed — so the "đã có" badges track the current folder.
- **Download** (`POST /download`) — both engines run as **subprocesses** (not the Python API) so they reuse the exact proven CLI flags and spotDL's config. stdout is parsed line-by-line to drive the state machine.
- **Library** (`GET /library`, `POST /library/delete`, `POST /library/delete-many` for multi-select bulk delete, `POST /library/open` reveals the folder in Finder via `open`) — `scan_library()` reads ID3 tags from `downloads/*.mp3` via mutagen, **skipping dot-files** — on external/exFAT drives macOS creates `._<name>.mp3` AppleDouble sidecars that `glob("*.mp3")` would otherwise list as phantom 0-byte tracks. Search/resolve results are tagged with `in_library` using **fuzzy matching** (`library_match`, rapidfuzz `token_set_ratio` ≥ 87 on the normalized **title**, with a loose artist guard to drop same-title/different-artist) — title-based, because matching on "artist title" made every track by an already-owned artist a false positive. Duplicates are **warned, never auto-skipped** — the user decides. `library_delete` only removes basename-resolved `.mp3` files inside `downloads/` (path-traversal guarded).

### Download worker (the part that needs cross-file understanding)

- A **single daemon worker thread** drains `WORK_Q` and processes jobs **sequentially** (one at a time, by design).
- `JOBS` is a shared list of job dicts; **a job's id is its index in `JOBS`**. `JOBS_LOCK` guards all access. Do not reorder or remove entries mid-flight or indices in `WORK_Q` will dangle.
- `/download` clears `JOBS` to start a fresh batch, **but only when no job is active** (queued/downloading/converting) — that is the safe moment because the queue is drained. Preserve this invariant.
- The frontend **polls `GET /status`** every 750ms and stops when `active` is false. State machine: `queued → downloading → converting → done | error`.

## Critical constraints (do not break these)

- **FastAPI is pinned at 0.103.2 by spotDL.** Do not use newer FastAPI APIs (e.g. `fastapi.sse.EventSourceResponse` does not exist in this version). This is why progress uses **polling, not SSE**. Upgrading FastAPI risks breaking spotDL.
- **Spotify uses the shared client credentials bundled in `~/.spotdl/config.json`**, which Spotify frequently rate-limits (24h). Handle Spotify calls **fail-fast**: spotipy is built with `retries=0` and wrapped in an 8s hard-timeout (`search_spotify`). On failure, search **degrades to YouTube-only** with a note; resolve falls back to OG scrape. Never let a Spotify call block a request.
- **spotDL download settings live in `~/.spotdl/config.json`** (notably `bitrate: 320k`), applied automatically because the subprocess inherits it. yt-dlp flags are hard-coded in `_process()`.
- **The output folder is the module global `DOWNLOADS`**, reassignable at runtime via `POST /config/folder` and persisted to `.music4life.json` (loaded on startup). The download worker, `scan_library`, delete and open-folder all read this global at call time — so changing it affects both download and library. Functions reference `DOWNLOADS` by name (never capture it); preserve that so changes take effect.
- This server binds **127.0.0.1 only** and is meant to run locally; there is no auth.

## Working conventions

- **Use the Context7 MCP to fetch current docs for any library before writing code against it** (FastAPI, yt-dlp, spotipy, etc.) rather than relying on recalled APIs — library versions here are specific and some APIs differ from the latest docs.
- Frontend changes need only a browser hard-refresh (FileResponse reads the file each request); backend changes require restarting the server.
