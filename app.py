"""Music4Life — web UI cục bộ để tải nhạc audio bằng spotDL + yt-dlp.

Chạy: ./run.sh  (hoặc: .venv/bin/python app.py)
Mở:   http://127.0.0.1:8000
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# --- Đường dẫn & công cụ -----------------------------------------------------
BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / ".music4life.json"


def _load_download_dir() -> Path:
    """Đọc thư mục lưu từ config (nếu có), ngược lại dùng ./downloads."""
    default = BASE / "downloads"
    try:
        saved = json.loads(CONFIG_FILE.read_text()).get("download_dir", "")
        if saved:
            p = Path(os.path.expanduser(saved))
            p.mkdir(parents=True, exist_ok=True)
            return p.resolve()
    except Exception:
        pass
    default.mkdir(exist_ok=True)
    return default.resolve()


DOWNLOADS = _load_download_dir()  # có thể đổi lúc chạy qua /config/folder
VENV_BIN = Path(sys.executable).parent
SPOTDL = str(VENV_BIN / "spotdl")
YTDLP = str(VENV_BIN / "yt-dlp")
SPOTDL_CONFIG = Path(os.path.expanduser("~/.spotdl/config.json"))

# --- Spotify credentials (tái dùng từ config spotDL) -------------------------
def _spotify_creds():
    try:
        cfg = json.loads(SPOTDL_CONFIG.read_text())
        return cfg.get("client_id"), cfg.get("client_secret")
    except Exception:
        return None, None


def _spotify_client():
    """Client spotipy fail-nhanh (không retry, timeout ngắn) hoặc None nếu thiếu creds."""
    cid, secret = _spotify_creds()
    if not (cid and secret):
        return None
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    return spotipy.Spotify(
        auth_manager=SpotifyClientCredentials(client_id=cid, client_secret=secret),
        requests_timeout=6, retries=0, status_retries=0,
    )


# --- Phân loại input ---------------------------------------------------------
def classify(text: str) -> str:
    t = text.strip().lower()
    if "open.spotify.com" in t or t.startswith("spotify:"):
        return "spotify"
    if "youtube.com" in t or "youtu.be" in t:
        return "youtube"
    return "name"


def _fmt(sec: float | int | None) -> str:
    s = int(sec or 0)
    return f"{s // 60}:{s % 60:02d}"


# --- Tìm kiếm ----------------------------------------------------------------
def search_youtube(query: str, limit: int = 5) -> list[dict]:
    from yt_dlp import YoutubeDL
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    out = []
    for e in info.get("entries", []) or []:
        vid = e.get("id")
        out.append({
            "kind": "youtube",
            "title": e.get("title") or "(không tên)",
            "artist": e.get("channel") or e.get("uploader") or "",
            "duration": _fmt(e.get("duration")),
            "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else "",
            "url": f"https://youtu.be/{vid}" if vid else (e.get("url") or ""),
        })
    return out


SPOTIFY_NOTE = {
    "ok": None,
    "no_key": "Chưa có Spotify API key (chỉ hiện kết quả YouTube).",
    "rate_limited": "Spotify đang bị giới hạn truy vấn (chỉ hiện kết quả YouTube).",
    "error": "Không gọi được Spotify (chỉ hiện kết quả YouTube).",
}


def search_spotify(query: str, limit: int = 5) -> tuple[list[dict], str]:
    """Trả về (results, status). status: ok | no_key | rate_limited | error.

    Bọc trong hard-timeout 8s để không bao giờ treo dù Spotify retry/limit.
    """
    cid, secret = _spotify_creds()
    if not (cid and secret):
        return [], "no_key"

    def _do() -> list[dict]:
        sp = _spotify_client()
        r = sp.search(q=query, type="track", limit=limit)
        out = []
        for t in r["tracks"]["items"]:
            imgs = t["album"].get("images") or []
            out.append({
                "kind": "spotify",
                "title": t["name"],
                "artist": ", ".join(a["name"] for a in t["artists"]),
                "duration": _fmt(t["duration_ms"] / 1000),
                "thumb": imgs[-1]["url"] if imgs else "",
                "url": t["external_urls"]["spotify"],
            })
        return out

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(_do)
    try:
        return fut.result(timeout=8), "ok"
    except concurrent.futures.TimeoutError:
        return [], "rate_limited"
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "429" in msg or "rate" in msg or "limit" in msg:
            return [], "rate_limited"
        return [], "error"
    finally:
        ex.shutdown(wait=False)


# --- Lấy thông tin 1 bài từ URL (xem trước trước khi tải) --------------------
def resolve_youtube(url: str) -> dict:
    from yt_dlp import YoutubeDL
    # noplaylist=True: chỉ lấy 1 video kể cả khi URL có &list=… (tránh bung playlist)
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    with YoutubeDL(opts) as ydl:
        e = ydl.extract_info(url, download=False)
    vid = e.get("id")
    return {
        "kind": "youtube",
        "title": e.get("title") or url,
        "artist": e.get("channel") or e.get("uploader") or "",
        "duration": _fmt(e.get("duration")),
        "thumb": e.get("thumbnail") or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ""),
        "url": f"https://youtu.be/{vid}" if vid else url,   # URL sạch -> tải đúng 1 bài
    }


def _spotify_og(url: str) -> dict | None:
    """Lấy thông tin từ thẻ Open Graph của trang Spotify công khai.
    Không cần API key, không bị rate-limit."""
    import re
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "ignore")

    def og(prop: str) -> str | None:
        m = (re.search(r'<meta[^>]+property="og:%s"[^>]+content="([^"]*)"' % prop, html)
             or re.search(r'<meta[^>]+content="([^"]*)"[^>]+property="og:%s"' % prop, html))
        return m.group(1) if m else None

    title = og("title")
    if not title:
        return None
    desc = og("description") or ""
    is_track = "/track/" in url or url.startswith("spotify:track:")
    m = re.search(r'<meta[^>]+name="music:duration"[^>]+content="(\d+)"', html)
    return {
        "kind": "spotify",
        "title": title,
        "artist": desc.split("·")[0].strip() if (is_track and "·" in desc) else "",
        "duration": _fmt(int(m.group(1))) if m else "",
        "thumb": og("image") or "",
        "url": url,
        "is_list": not is_track,
    }


def _youtube_list_tracks(url: str, limit: int = 200) -> tuple[str | None, list[dict]]:
    """Tách playlist YouTube thành từng video (extract_flat, không tải)."""
    import re
    from yt_dlp import YoutubeDL
    # Chuẩn hoá về dạng /playlist?list=… — dạng watch?v=…&list=… trả 0 entry.
    m = re.search(r"[?&]list=([^&]+)", url)
    if m:
        url = f"https://www.youtube.com/playlist?list={m.group(1)}"
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "skip_download": True, "playlistend": limit}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    title = info.get("title")
    items = []
    for e in info.get("entries") or []:
        if not e:
            continue
        vid = e.get("id")
        link = (f"https://youtu.be/{vid}" if vid else None) or e.get("url")
        if not link:
            continue
        items.append({
            "kind": "youtube",
            "title": e.get("title") or "(không tên)",
            "artist": e.get("channel") or e.get("uploader") or "",
            "duration": _fmt(e.get("duration")),
            "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else "",
            "url": link,
        })
    return title, items


def resolve_spotify(url: str) -> tuple[dict | None, str]:
    """Trả về (item, status). status: ok | list | error.

    Dùng Open Graph (không API) nên xem trước được kể cả khi Spotify API bị giới hạn.
    """
    try:
        item = _spotify_og(url)
    except Exception:  # noqa: BLE001
        item = None
    if item:
        return item, ("list" if item.get("is_list") else "ok")
    if "/track/" not in url and not url.startswith("spotify:track:"):
        return None, "list"
    return None, "error"


def _spotify_list_tracks(url: str) -> tuple[str | None, list[dict]]:
    """Tách album/playlist Spotify thành từng track qua Open Graph (không cần API).

    Trang công khai có các thẻ <meta name="music:song" content="track-url">.
    Trả về (tên-danh-sách, [item-track...]).
    """
    import re
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore")
    m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', html)
    title = m.group(1) if m else None
    if title:
        title = title.split(" | ")[0]
        title = re.sub(r" - (Album|Single|EP|Playlist|Compilation) by .*$", "", title)

    seen, ordered = set(), []
    for u in re.findall(r'<meta\s+name="music:song"\s+content="([^"]+)"', html):
        if u not in seen:
            seen.add(u)
            ordered.append(u)

    items: list[dict] = []
    if ordered:
        def _one(u: str):
            try:
                return _spotify_og(u)
            except Exception:  # noqa: BLE001
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            items = [it for it in ex.map(_one, ordered) if it]
    return title, items


# --- Thư viện: quét file đã tải + phát hiện trùng ----------------------------
def _norm(s: str) -> str:
    import re
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (s or "").lower())).strip()


def scan_library() -> list[dict]:
    """Quét downloads/*.mp3, đọc ID3 (mutagen). Sắp xếp mới nhất trước."""
    from mutagen import File as MutagenFile
    items = []
    for p in sorted(DOWNLOADS.glob("*.mp3"), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.name.startswith("."):  # bỏ file ẩn & AppleDouble (._xxx macOS tạo trên ổ ngoài)
            continue
        title = artist = album = ""
        dur = 0.0
        try:
            mf = MutagenFile(p, easy=True)
            if mf is not None:
                title = (mf.get("title") or [""])[0]
                artist = ", ".join(mf.get("artist") or [])
                album = (mf.get("album") or [""])[0]
                dur = float(getattr(mf.info, "length", 0) or 0)
        except Exception:
            pass
        st = p.stat()
        items.append({
            "file": p.name,
            "title": title or p.stem,
            "artist": artist,
            "album": album,
            "duration": _fmt(dur),
            "size_mb": round(st.st_size / 1048576, 1),
            "mtime": st.st_mtime,
            "_ntitle": _norm(title or p.stem),
            "_nartist": _norm(artist),
        })
    return items


def library_match(artist: str, title: str, lib: list[dict]) -> str | None:
    """So khớp trùng theo TỰA bài (fuzzy). Tên ca sĩ chỉ dùng để loại nhầm
    khi tựa giống nhưng ca sĩ khác hẳn. Trả tên file trùng hoặc None."""
    from rapidfuzz import fuzz
    qt = _norm(title)
    if not qt:
        return None
    qa = _norm(artist)
    for it in lib:
        if fuzz.token_set_ratio(qt, it["_ntitle"]) < 87:
            continue
        la = it["_nartist"]
        if qa and la and fuzz.token_set_ratio(qa, la) < 40:
            continue  # cùng tựa nhưng ca sĩ khác hẳn -> không tính trùng
        return it["file"]
    return None


def annotate_in_library(items: list[dict], lib: list[dict]) -> list[dict]:
    for it in items:
        m = library_match(it.get("artist", ""), it.get("title", ""), lib)
        it["in_library"] = bool(m)
        it["lib_file"] = m
    return items


# --- Hàng đợi tải (1 worker, tuần tự) ----------------------------------------
JOBS: list[dict] = []          # trạng thái hiển thị; index = job id
JOBS_LOCK = threading.Lock()
WORK_Q: "queue.Queue[int]" = queue.Queue()
_worker_started = False


def _set(idx: int, **kw):
    with JOBS_LOCK:
        JOBS[idx].update(kw)


def _process(idx: int):
    with JOBS_LOCK:
        job = dict(JOBS[idx])
    kind, url = job["kind"], job["url"]
    _set(idx, state="downloading", log="Bắt đầu…")

    if kind == "spotify":
        cmd = [SPOTDL, "download", url,
               "--output", str(DOWNLOADS / "{artists} - {title}.{output-ext}")]
    else:  # youtube
        cmd = [YTDLP, "-x", "--audio-format", "mp3", "--audio-quality", "0",
               "--embed-thumbnail", "--embed-metadata", "--no-playlist",
               "-o", str(DOWNLOADS / "%(title)s.%(ext)s"), url]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            if "[ExtractAudio]" in line or "[ffmpeg]" in line or "Converting" in line:
                _set(idx, state="converting", log=line[:120])
            elif line.startswith("[download]") or "Downloading" in line or "Downloaded" in line:
                _set(idx, state="downloading", log=line[:120])
            else:
                _set(idx, log=line[:120])
        proc.wait()
        if proc.returncode == 0:
            _set(idx, state="done", log="Hoàn tất ✓")
        else:
            _set(idx, state="error", log=f"Lỗi (mã {proc.returncode})")
    except Exception as e:  # noqa: BLE001
        _set(idx, state="error", log=str(e)[:120])


def _worker():
    while True:
        idx = WORK_Q.get()
        _process(idx)
        WORK_Q.task_done()


def _ensure_worker():
    global _worker_started
    if not _worker_started:
        threading.Thread(target=_worker, daemon=True).start()
        _worker_started = True


# --- App ---------------------------------------------------------------------
# --- "Lưu để tải sau" (wishlist, bền qua restart) ----------------------------
SAVED_FILE = BASE / ".music4life.saved.json"
SAVED: list[dict] = []
SAVED_LOCK = threading.Lock()


def _load_saved():
    global SAVED
    try:
        data = json.loads(SAVED_FILE.read_text())
        SAVED = data if isinstance(data, list) else []
    except Exception:
        SAVED = []


def _persist_saved():
    try:
        SAVED_FILE.write_text(json.dumps(SAVED, ensure_ascii=False))
    except Exception:
        pass


_load_saved()


app = FastAPI(title="Music4Life")


class SearchReq(BaseModel):
    query: str


class ResolveReq(BaseModel):
    url: str


class Item(BaseModel):
    kind: str          # "spotify" | "youtube"
    url: str
    title: str = ""
    artist: str = ""


class DownloadReq(BaseModel):
    items: list[Item]


class SavedItem(BaseModel):
    kind: str
    url: str
    title: str = ""
    artist: str = ""
    thumb: str = ""
    duration: str = ""


class SaveReq(BaseModel):
    items: list[SavedItem]


class UrlsReq(BaseModel):
    urls: list[str]


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/favicon.ico")
@app.get("/favicon.svg")
def favicon():
    return FileResponse(BASE / "static" / "favicon.svg", media_type="image/svg+xml")


@app.post("/search")
def search(req: SearchReq):
    q = req.query.strip()
    lib = scan_library()
    yt = annotate_in_library(search_youtube(q), lib)
    sp, sp_status = search_spotify(q)
    annotate_in_library(sp, lib)
    return {"spotify": sp, "youtube": yt, "spotify_note": SPOTIFY_NOTE.get(sp_status)}


RESOLVE_NOTE = {
    "no_key": "Chưa có Spotify API key — vẫn tải được, chỉ là không xem trước được thông tin.",
    "rate_limited": "Spotify giới hạn hiển thị thông tin, vẫn có thể tải",
    "list": "Link danh sách/album Spotify — sẽ tải toàn bộ khi bấm Tải.",
    "error": "Không lấy được thông tin — vẫn tải được khi bấm Tải.",
}


@app.post("/resolve")
def resolve(req: ResolveReq):
    """Lấy thông tin 1 bài từ URL để xem trước. Luôn trả 'item' (kể cả khi không
    xem trước được) để dòng vẫn tải được."""
    url = req.url.strip()
    kind = classify(url)
    lib = scan_library()

    def _one(item, note=None):
        annotate_in_library([item], lib)
        return {"ok": True, "multi": False, "note": note, "items": [item]}

    if kind == "youtube":
        import re
        m = re.search(r"[?&]list=([^&]+)", url)
        list_id = m.group(1) if m else None
        # Mix/radio (list=RD…) là playlist tự sinh -> coi như video đơn, không bung.
        is_playlist = ("/playlist" in url) or (list_id and not list_id.startswith("RD"))
        if is_playlist:
            try:
                title, tracks = _youtube_list_tracks(url)
            except Exception:  # noqa: BLE001
                title, tracks = None, []
            if tracks:
                annotate_in_library(tracks, lib)
                return {"ok": True, "multi": True, "note": None,
                        "title": title or "Playlist YouTube", "items": tracks}
            return {"ok": True, "multi": False,
                    "note": ("Không tách được danh sách — có thể playlist ở chế độ riêng tư "
                             "(private) hoặc bị hạn chế. Playlist riêng tư cần đăng nhập mới "
                             "truy cập được; hãy đổi sang Public/Unlisted rồi thử lại."),
                    "items": [{"kind": "youtube", "title": url, "artist": "", "duration": "", "thumb": "", "url": url}]}
        try:
            item = resolve_youtube(url)
        except Exception:  # noqa: BLE001
            return {"ok": False, "multi": False,
                    "note": "Không lấy được thông tin video — vẫn tải được khi bấm Tải.",
                    "items": [{"kind": "youtube", "title": url, "artist": "", "duration": "", "thumb": "", "url": url}]}
        return _one(item)

    if kind == "spotify":
        is_track = "/track/" in url or url.startswith("spotify:track:")
        if is_track:
            item, status = resolve_spotify(url)
            if item:
                return _one(item)
            return {"ok": False, "multi": False, "note": RESOLVE_NOTE.get(status, ""),
                    "items": [{"kind": "spotify", "title": url, "artist": "", "duration": "", "thumb": "", "url": url}]}
        # album / playlist / artist -> tách thành từng bài
        try:
            title, tracks = _spotify_list_tracks(url)
        except Exception:  # noqa: BLE001
            title, tracks = None, []
        if tracks:
            annotate_in_library(tracks, lib)
            return {"ok": True, "multi": True, "note": None,
                    "title": title or "Danh sách Spotify", "items": tracks}
        # không tách được -> tải nguyên cụm bằng spotDL (1 mục)
        try:
            whole = _spotify_og(url)
        except Exception:  # noqa: BLE001
            whole = None
        whole = whole or {"kind": "spotify", "title": url, "artist": "", "duration": "", "thumb": "", "url": url}
        return _one(whole, "Không tách được từng bài — sẽ tải toàn bộ trong 1 mục khi bấm Tải.")

    return {"ok": False, "multi": False, "note": "Không phải link Spotify/YouTube hợp lệ.", "items": []}


def _pkg(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "?"


@app.get("/health")
def health():
    """Trạng thái sẵn sàng của công cụ (cho nút 'Kiểm tra trạng thái')."""
    # ffmpeg
    ff = shutil.which("ffmpeg") or shutil.which(str(VENV_BIN / "ffmpeg"))
    ff_info = ""
    if ff:
        try:
            ff_info = subprocess.run([ff, "-version"], capture_output=True, text=True,
                                     timeout=5).stdout.splitlines()[0].split(" version ")[-1].split()[0]
        except Exception:
            ff_info = "có"
    # spotify probe (fail nhanh)
    cid, secret = _spotify_creds()
    if not (cid and secret):
        sp_status = "no_key"
    else:
        _, sp_status = search_spotify("test", limit=1)

    return {
        "ffmpeg":  {"ok": bool(ff), "info": ff_info or "không tìm thấy"},
        "yt_dlp":  {"ok": True, "info": _pkg("yt-dlp")},
        "spotdl":  {"ok": True, "info": _pkg("spotdl")},
        "spotify": {"ok": sp_status == "ok", "status": sp_status,
                    "info": {"ok": "Sẵn sàng", "no_key": "Chưa có API key",
                             "rate_limited": "Bị giới hạn (~24h)",
                             "error": "Lỗi kết nối"}[sp_status]},
    }


@app.post("/download")
def download(req: DownloadReq):
    _ensure_worker()
    ids = []
    with JOBS_LOCK:
        # Bắt đầu mẻ mới: xoá job cũ nếu không còn job nào đang chạy
        # (an toàn vì hàng đợi đã rỗng khi không còn active -> chỉ số không lệch).
        active = any(j["state"] in ("queued", "downloading", "converting") for j in JOBS)
        if not active:
            JOBS.clear()
        for it in req.items:
            idx = len(JOBS)
            JOBS.append({
                "id": idx, "kind": it.kind, "url": it.url,
                "title": it.title or it.url, "artist": it.artist,
                "state": "queued", "log": "Đang chờ…",
            })
            ids.append(idx)
    for idx in ids:
        WORK_Q.put(idx)
    return {"ids": ids}


@app.get("/status")
def status():
    with JOBS_LOCK:
        jobs = [dict(j) for j in JOBS]
    total = len(jobs)
    done = sum(1 for j in jobs if j["state"] == "done")
    err = sum(1 for j in jobs if j["state"] == "error")
    active = any(j["state"] in ("queued", "downloading", "converting") for j in jobs)
    return {"jobs": jobs, "total": total, "done": done, "error": err, "active": active}


@app.get("/library")
def library():
    items = scan_library()
    return {"items": items, "total": len(items)}


class DeleteReq(BaseModel):
    file: str


@app.post("/library/open")
def library_open():
    """Mở thư mục downloads/ trong Finder (chỉ chạy cục bộ trên máy)."""
    try:
        subprocess.Popen(["open", str(DOWNLOADS)])
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


class DeleteManyReq(BaseModel):
    files: list[str]


@app.post("/library/delete-many")
def library_delete_many(req: DeleteManyReq):
    base = DOWNLOADS.resolve()
    deleted, errors = 0, []
    for f in req.files:
        p = (DOWNLOADS / os.path.basename(f)).resolve()
        if p.parent != base or p.suffix.lower() != ".mp3" or not p.exists():
            errors.append(os.path.basename(f))
            continue
        try:
            p.unlink()
            deleted += 1
        except Exception:  # noqa: BLE001
            errors.append(p.name)
    return {"ok": True, "deleted": deleted, "errors": errors}


@app.post("/library/delete")
def library_delete(req: DeleteReq):
    name = os.path.basename(req.file)
    p = (DOWNLOADS / name).resolve()
    if p.parent != DOWNLOADS.resolve() or p.suffix.lower() != ".mp3" or not p.exists():
        return {"ok": False, "error": "File không hợp lệ."}
    try:
        p.unlink()
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.get("/saved")
def saved_list():
    with SAVED_LOCK:
        items = [dict(x) for x in SAVED]
    annotate_in_library(items, scan_library())
    return {"items": items, "total": len(items)}


@app.post("/saved/add")
def saved_add(req: SaveReq):
    with SAVED_LOCK:
        have = {x.get("url") for x in SAVED}
        added = 0
        for it in req.items:
            if it.url and it.url not in have:
                SAVED.append(it.model_dump())
                have.add(it.url)
                added += 1
        _persist_saved()
        total = len(SAVED)
    return {"ok": True, "added": added, "total": total}


@app.post("/saved/remove")
def saved_remove(req: UrlsReq):
    with SAVED_LOCK:
        urls = set(req.urls)
        before = len(SAVED)
        SAVED[:] = [x for x in SAVED if x.get("url") not in urls]
        _persist_saved()
        removed = before - len(SAVED)
        total = len(SAVED)
    return {"ok": True, "removed": removed, "total": total}


class FolderReq(BaseModel):
    path: str


@app.get("/config")
def get_config():
    return {"download_dir": str(DOWNLOADS)}


def _use_download_dir(p: Path) -> None:
    """Đặt thư mục lưu hiện hành + lưu vào config."""
    global DOWNLOADS
    DOWNLOADS = p
    try:
        CONFIG_FILE.write_text(json.dumps({"download_dir": str(p)}, ensure_ascii=False))
    except Exception:
        pass


@app.post("/config/folder")
def set_folder(req: FolderReq):
    """Đổi thư mục lưu bằng đường dẫn nhập tay (áp dụng cả tải lẫn thư viện)."""
    raw = (req.path or "").strip()
    if not raw:
        return {"ok": False, "error": "Đường dẫn trống."}
    p = Path(os.path.expanduser(raw)).resolve()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Không tạo được thư mục: {e}"}
    if not p.is_dir():
        return {"ok": False, "error": "Không phải thư mục hợp lệ."}
    _use_download_dir(p)
    return {"ok": True, "download_dir": str(p)}


@app.post("/config/pick-folder")
def pick_folder():
    """Mở hộp chọn thư mục NATIVE của macOS (osascript). Chỉ chạy cục bộ."""
    default_loc = str(DOWNLOADS) if DOWNLOADS.exists() else os.path.expanduser("~")
    script = (
        f'choose folder with prompt "Chọn thư mục lưu nhạc" '
        f'default location (POSIX file "{default_loc}")'
    )
    try:
        r = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to activate',
             "-e", f"POSIX path of ({script})"],
            capture_output=True, text=True, timeout=300,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    if r.returncode != 0:  # người dùng bấm Cancel -> osascript báo lỗi
        return {"ok": False, "canceled": True}
    path = r.stdout.strip()
    if not path:
        return {"ok": False, "canceled": True}
    p = Path(path).resolve()
    _use_download_dir(p)
    return {"ok": True, "download_dir": str(p)}


app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
