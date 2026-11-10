#!/usr/bin/env python3
"""danbooru-bridge - Monbooru plugin that speaks the Danbooru v2 JSON API.

Booru apps (Anime Boxes & co.) talk to this service as if it were a Danbooru
site. It translates every request into calls against the Monbooru REST API
and converts the answers back into Danbooru-shaped JSON. Image and thumbnail
downloads are streamed through, so the phone never needs the Monbooru token.

Pure standard library, Python 3.8+. No pip install needed.

Environment variables
---------------------
MONBOORU_URL         Base URL of Monbooru, e.g. http://monbooru:8080 (set by Monbooru
                     itself when it launches the plugin).
BRIDGE_SELF_URL      Where *Monbooru* reaches this bridge (health check during pairing),
                     e.g. http://danbooru-bridge:8456. Default http://127.0.0.1:<port>.
BRIDGE_PUBLIC_URL    Base URL the *phone* uses, e.g. https://dan.example.org. Used for the
                     file_url / preview_file_url fields. Default: derived from the Host
                     header of each request.
BRIDGE_PORT          Listen port (default 8456).
BRIDGE_BIND          Listen address (default 0.0.0.0).
BRIDGE_DATA_DIR      Where the pairing token is stored (default ./data). Keep it persistent.
BRIDGE_GALLERY       Monbooru gallery to expose (default: the active gallery).
BRIDGE_FAVORITES     "1" = also request the write scope and map Danbooru favorites to
                     Monbooru favorites. Default: read-only.
BRIDGE_API_KEY       Shared secret. When set, every API call must carry it (as the Danbooru
                     api_key parameter, as HTTP Basic password or as Bearer token). Image
                     URLs get a derived media token appended, so they need no login.
                     /health stays open for Monbooru. Unset = open access (warning logged).
BRIDGE_FAVICON_URL   Icon served at /favicon.ico (default: <MONBOORU_URL>/favicon.ico).
BRIDGE_ALLOW         Optional comma-separated client networks, e.g.
                     192.168.178.0/24,100.64.0.0/10. Other clients get 403 (except /health).
BRIDGE_RATING_TAGS   Mapping Danbooru rating -> tag name in Monbooru's rating category.
                     Default: g=general,s=sensitive,q=questionable,e=explicit
BRIDGE_DEFAULT_RATING  Rating for images without a rating tag (default g).
BRIDGE_LOG           Log level (default INFO; DEBUG shows dropped search terms).

Start with --managed when Monbooru launches this from its plugins/ folder
(exits when stdin is closed, as the plugin contract asks).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
APP_NAME = "danbooru-bridge"
CONTRACT = 1


def env(name, default=""):
    return os.environ.get(name, default)


MONBOORU_URL = env("MONBOORU_URL", "http://127.0.0.1:8080").rstrip("/")
PORT = int(env("BRIDGE_PORT", "8456"))
BIND = env("BRIDGE_BIND", "0.0.0.0")
SELF_URL = env("BRIDGE_SELF_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
PUBLIC_URL = env("BRIDGE_PUBLIC_URL").rstrip("/")
DATA_DIR = env("BRIDGE_DATA_DIR", "data")
GALLERY = env("BRIDGE_GALLERY")
FAVORITES = env("BRIDGE_FAVORITES") == "1"
DEFAULT_RATING = env("BRIDGE_DEFAULT_RATING", "g")
API_KEY = env("BRIDGE_API_KEY")
# Derived token for image URLs: a leaked image link never reveals the API key.
MEDIA_TOKEN = (hmac.new(API_KEY.encode(), b"danbooru-bridge-media", hashlib.sha256).hexdigest()[:32]
               if API_KEY else "")
MEDIA_QS = f"?k={MEDIA_TOKEN}" if API_KEY else ""
ALLOW_NETS = [ipaddress.ip_network(n.strip(), strict=False) for n in env("BRIDGE_ALLOW").split(",") if n.strip()]


def client_allowed(addr):
    if not ALLOW_NETS:
        return True
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in ALLOW_NETS)

log = logging.getLogger(APP_NAME)

# --------------------------------------------------------------------------
# Mapping tables
# --------------------------------------------------------------------------

CAT_TO_ID = {"general": 0, "artist": 1, "copyright": 3, "character": 4, "meta": 5}
ID_TO_CAT = {v: k for k, v in CAT_TO_ID.items()}
EPOCH = "1970-01-01T00:00:00.000+00:00"


def _parse_rating_tags(spec):
    out = {}
    for part in spec.split(","):
        code, _, name = part.partition("=")
        if code.strip() and name.strip():
            out[code.strip()] = name.strip()
    return out


RATING_TAGS = _parse_rating_tags(env("BRIDGE_RATING_TAGS", "g=general,s=sensitive,q=questionable,e=explicit"))
RATING_ORDER = "gsqe"
RATING_ALIASES = {"general": "g", "safe": "g", "sensitive": "s", "questionable": "q", "explicit": "e"}
for _code, _name in RATING_TAGS.items():
    RATING_ALIASES[_name.lower()] = _code


def rating_code(value):
    v = (value or "").strip().lower()
    if v in RATING_TAGS:
        return v
    return RATING_ALIASES.get(v)


# Danbooru meta-tags Monbooru has no equivalent for; they are dropped from searches.
DROP_KEYS = {
    "id", "user", "approver", "commenter", "comm", "noter", "artcomm", "commenttext",
    "notetext", "status", "pool", "ordpool", "parent", "child", "filetype", "ext", "md5",
    "score", "favcount", "tagcount", "gentags", "arttags", "chartags", "copytags",
    "metatags", "age", "updater", "pixiv", "pixiv_id", "limit", "has", "hascomments",
    "hasnotes", "isparent", "ischild", "upvote", "downvote", "favgroup", "flagger",
    "appealer", "searchtags", "ratio", "mpixels", "duration", "random",
}

# Endpoints apps poll that Monbooru has no data for: answer with an empty list.
STUB_LISTS = (
    r"/pools", r"/artists", r"/comments", r"/notes", r"/post_versions", r"/saved_searches",
    r"/forum_topics", r"/wiki_pages", r"/dmails", r"/post_appeals", r"/post_flags",
    r"/tag_aliases", r"/tag_implications", r"/favorite_groups", r"/post_replacements",
)

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def to_int(value, default, lo, hi):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def parse_page(value):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        if value:
            log.warning("cursor-style page=%r is not supported, using page 1", value)
        return 1


def iso(ts):
    """Return a Danbooru-style timestamp (millisecond precision, +hh:mm offset)."""
    if not ts:
        return EPOCH
    s = re.sub(r"(\.\d{6})\d+", r"\1", str(ts).replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return EPOCH
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------
# Pairing state + Monbooru client
# --------------------------------------------------------------------------


class State:
    def __init__(self):
        self.token = None
        self.request_id = None
        self.need_pair = threading.Event()
        self.path = os.path.join(DATA_DIR, "state.json")
        try:
            with open(self.path) as f:
                data = json.load(f)
            self.token = data.get("token")
            self.request_id = data.get("request_id")
        except (OSError, ValueError):
            pass

    def save(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"token": self.token, "request_id": self.request_id}, f)
        os.replace(tmp, self.path)

    def reset_token(self):
        log.warning("Monbooru rejected the token - starting a new pairing")
        self.token = None
        self.request_id = None
        self.save()
        self.need_pair.set()


STATE = State()


class MonbooruError(Exception):
    def __init__(self, status, detail=""):
        super().__init__(f"Monbooru answered {status}: {detail}")
        self.status = status
        self.detail = detail


def mb_open(method, path, params=None, body=None, headers=None):
    if not STATE.token:
        raise MonbooruError(503, "bridge is not paired with Monbooru yet")
    query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    if GALLERY:
        query.setdefault("gallery", GALLERY)
    url = f"{MONBOORU_URL}/api/v1{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {STATE.token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        return urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as exc:
        detail = exc.read(500).decode("utf-8", "replace")
        if exc.code == 401:
            STATE.reset_token()
        raise MonbooruError(exc.code, detail) from None
    except urllib.error.URLError as exc:
        raise MonbooruError(502, str(exc.reason)) from None


def mb_json(method, path, params=None, body=None):
    with mb_open(method, path, params, body) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def http_json(method, url, body=None, timeout=15):
    """Unauthenticated JSON call (pairing)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def pair_once():
    if not STATE.request_id:
        res = http_json("POST", f"{MONBOORU_URL}/api/v1/pair/request", {
            "app": APP_NAME,
            "url": SELF_URL,
            "version": VERSION,
            "contract": CONTRACT,
            "requested_scopes": ["read", "write"] if FAVORITES else ["read"],
        })
        STATE.request_id = res["request_id"]
        STATE.save()
        log.info("Pairing requested - approve '%s' in the Monbooru settings", APP_NAME)
    while True:
        url = f"{MONBOORU_URL}/api/v1/pair/status?" + urllib.parse.urlencode({"id": STATE.request_id})
        st = http_json("GET", url)
        status = st.get("status")
        if status == "approved":
            if not st.get("token"):
                log.error("approved, but no token came back (it is only shown once); requesting again")
                STATE.request_id = None
                STATE.save()
                return
            STATE.token = st["token"]
            STATE.request_id = None
            STATE.save()
            log.info("Paired with Monbooru")
            return
        if status == "denied":
            log.error("Pairing was denied in Monbooru; will ask again in a minute")
            STATE.request_id = None
            STATE.save()
            time.sleep(60)
            return
        time.sleep(2)


def pair_loop():
    while True:
        if STATE.token:
            STATE.need_pair.wait()
            STATE.need_pair.clear()
            continue
        try:
            pair_once()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # pending request expired
                STATE.request_id = None
                STATE.save()
            log.warning("pairing: HTTP %s from Monbooru (retrying)", exc.code)
            time.sleep(10)
        except Exception as exc:  # noqa: BLE001 - keep retrying, Monbooru may still be starting
            log.warning("pairing: %s (retrying)", exc)
            time.sleep(10)
        if not STATE.token:
            time.sleep(3)


# --------------------------------------------------------------------------
# Monbooru -> Danbooru conversion
# --------------------------------------------------------------------------


def split_tags(img):
    groups = {"general": [], "artist": [], "copyright": [], "character": [], "meta": []}
    rating = None
    for tag in img.get("tags") or []:
        cat = (tag.get("category") or "general").lower()
        name = (tag.get("name") or "").replace(" ", "_")
        if not name:
            continue
        if cat == "rating":
            code = rating_code(name.split(":", 1)[-1])
            if code and (rating is None or RATING_ORDER.index(code) > RATING_ORDER.index(rating)):
                rating = code
            continue
        groups.get(cat, groups["general"]).append(name)
    return groups, rating


def to_post(img, base):
    iid = img["id"]
    ftype = (img.get("file_type") or "jpeg").lower()
    ext = {"jpeg": "jpg", "cbz": "jpg"}.get(ftype, ftype)
    preview = f"{base}/preview/{iid}.jpg{MEDIA_QS}"
    if ftype == "cbz":  # archives: show the cover thumbnail
        file_url = large_url = preview
    else:
        file_url = f"{base}/data/{iid}.{ext}{MEDIA_QS}"
        large_url = f"{base}/sample/{iid}.webp{MEDIA_QS}" if ftype in ("avif", "jxl") else file_url
    groups, rating = split_tags(img)
    all_tags = [t for g in ("artist", "copyright", "character", "general", "meta") for t in groups[g]]
    sources = img.get("sources") or []
    source = (sources[0].get("url") if sources else "") or img.get("url") or ""
    ts = iso(img.get("ingested_at"))
    post = {
        "id": iid,
        "created_at": ts,
        "updated_at": ts,
        "uploader_id": 1,
        "approver_id": None,
        "score": 0,
        "up_score": 0,
        "down_score": 0,
        "fav_count": 1 if img.get("is_favorited") else 0,
        "source": source,
        "md5": img.get("md5") or "",
        "rating": rating or DEFAULT_RATING,
        "image_width": img.get("width") or 0,
        "image_height": img.get("height") or 0,
        "file_size": img.get("file_size") or 0,
        "file_ext": ext,
        "parent_id": None,
        "has_children": False,
        "has_active_children": False,
        "has_visible_children": False,
        "is_pending": False,
        "is_flagged": False,
        "is_deleted": False,
        "is_banned": False,
        "pixiv_id": None,
        "last_commented_at": None,
        "last_comment_bumped_at": None,
        "last_noted_at": None,
        "bit_flags": 0,
        "has_large": large_url != file_url,
        "tag_string": " ".join(all_tags),
        "tag_string_general": " ".join(groups["general"]),
        "tag_string_artist": " ".join(groups["artist"]),
        "tag_string_copyright": " ".join(groups["copyright"]),
        "tag_string_character": " ".join(groups["character"]),
        "tag_string_meta": " ".join(groups["meta"]),
        "tag_count": len(all_tags),
        "tag_count_general": len(groups["general"]),
        "tag_count_artist": len(groups["artist"]),
        "tag_count_copyright": len(groups["copyright"]),
        "tag_count_character": len(groups["character"]),
        "tag_count_meta": len(groups["meta"]),
        "file_url": file_url,
        "large_file_url": large_url,
        "preview_file_url": preview,
    }
    return post


def to_tag(row):
    name = (row.get("name") or "").replace(" ", "_")
    ts = iso(row.get("last_used_at"))
    return {
        "id": row.get("id", 0),
        "name": name,
        "post_count": row.get("usage_count", 0),
        "category": CAT_TO_ID.get((row.get("category") or "general").lower(), 0),
        "created_at": ts,
        "updated_at": ts,
        "is_deprecated": False,
        "words": name.split("_"),
    }


# --------------------------------------------------------------------------
# Danbooru search syntax -> Monbooru search syntax
# --------------------------------------------------------------------------


def translate_query(raw):
    terms, ors = [], []
    sort = order = None
    for tok in (raw or "").split():
        neg = tok.startswith("-")
        body = tok[1:] if neg else tok
        if not body or body.strip("*") == "":  # "*" alone means "everything" (Boorusphere's server probe)
            continue
        if tok.lower() == "or":
            terms.append("OR")
            continue
        if body[0] == "~" and not neg and len(body) > 1:
            ors.append(body[1:])
            continue
        key, sep, val = body.partition(":")
        k = key.lower()
        if sep and k == "order":
            v = val.lower()
            if v == "random":
                sort = "random"
            elif v in ("id", "id_asc"):
                sort, order = "newest", "asc"
            elif v == "id_desc":
                sort, order = "newest", "desc"
            elif v.startswith("filesize"):
                sort, order = "filesize", ("asc" if v.endswith("_asc") else "desc")
            else:
                log.debug("ignoring %s", tok)
            continue
        if sep and k == "rating":
            code = rating_code(val)
            name = RATING_TAGS.get(code) if code else None
            if name:
                terms.append(("-" if neg else "") + f"rating:{name}")
            else:
                log.warning("unsupported rating filter %r dropped", tok)
            continue
        if sep and k in ("fav", "ordfav"):
            terms.append(("-" if neg else "") + "fav:true")
            continue
        if sep and k in DROP_KEYS:
            log.debug("dropping unsupported meta-tag %s", tok)
            continue
        terms.append(tok)
    if ors:
        terms.append(" OR ".join(ors))
    return {"q": " ".join(terms), "sort": sort, "order": order}


def search_params(q, page, limit):
    out = {"q": q["q"], "page": page, "limit": limit, "sort": q.get("sort"), "order": q.get("order")}
    if q.get("sort") == "random":
        out["seed"] = str(int(time.time() // 600))  # keeps paging of a random search stable
    return out


# --------------------------------------------------------------------------
# Route handlers: fn(handler, match, params, head)
# --------------------------------------------------------------------------


def h_health(h, m, p, head):
    h.send_json({"version": VERSION, "app": APP_NAME, "paired": bool(STATE.token)}, head=head)


def h_posts(h, m, p, head):
    q = translate_query(p.get("tags", ""))
    if p.get("random") == "true":
        q["sort"] = "random"
    limit = to_int(p.get("limit"), 20, 1, 200)
    data = mb_json("GET", "/images/search", search_params(q, parse_page(p.get("page")), limit))
    base = h.base_url()
    h.send_json([to_post(i, base) for i in data.get("results") or []], head=head)


def h_post_random(h, m, p, head):
    q = translate_query(p.get("tags", ""))
    q["sort"] = "random"
    data = mb_json("GET", "/images/search", dict(search_params(q, 1, 1), seed=str(time.time())))
    results = data.get("results") or []
    if not results:
        raise MonbooruError(404, "no match")
    h.send_json(to_post(results[0], h.base_url()), head=head)


def h_post(h, m, p, head):
    img = mb_json("GET", f"/images/{int(m.group(1))}")
    h.send_json(to_post(img, h.base_url()), head=head)


def h_counts(h, m, p, head):
    q = translate_query(p.get("tags", ""))
    data = mb_json("GET", "/images/search", search_params(q, 1, 1))
    h.send_json({"counts": {"posts": data.get("total", 0)}}, head=head)


def h_tags(h, m, p, head):
    names = p.get("search[name_comma]")
    if names:  # clients use this to look up the categories of a list of tags
        rows = []
        for name in [n.strip() for n in names.split(",") if n.strip()][:100]:
            res = mb_json("GET", "/tags", {"q": name, "type": "tag", "limit": 10})
            rows += [r for r in res.get("results") or [] if r["name"].lower() == name.lower()]
        return h.send_json([to_tag(r) for r in rows], head=head)
    params = {"type": "tag", "limit": to_int(p.get("limit"), 20, 1, 500), "page": parse_page(p.get("page"))}
    exact = p.get("search[name]")
    pattern = p.get("search[name_matches]") or exact or p.get("search[name_normalize]")
    if pattern:
        params["q"] = pattern
    cat = p.get("search[category]")
    if cat:
        cname = ID_TO_CAT.get(to_int(cat, -1, -1, 99))
        if not cname:
            return h.send_json([], head=head)
        params["category"] = cname
    params["sort"] = {"count": "usage", "date": "created", "name": "name"}.get(p.get("search[order]", "count"), "usage")
    if p.get("search[hide_empty]") == "true":
        params["show_zero"] = "0"
    rows = mb_json("GET", "/tags", params).get("results") or []
    if exact:
        rows = [r for r in rows if r["name"].lower() == exact.lower()]
    h.send_json([to_tag(r) for r in rows], head=head)


def h_autocomplete(h, m, p, head):
    if p.get("search[type]", "tag_query") not in ("tag_query", "tag", "tag_word"):
        return h.send_json([], head=head)
    words = (p.get("search[query]") or p.get("search[name_matches]") or "").split()
    word = words[-1].lstrip("-~") if words else ""
    if not word:
        return h.send_json([], head=head)
    res = mb_json("GET", "/tags", {"q": word, "type": "tag", "sort": "usage",
                                   "limit": to_int(p.get("limit"), 10, 1, 50), "show_zero": "0"})
    out = []
    for row in res.get("results") or []:
        name = row["name"].replace(" ", "_")
        out.append({
            "type": "tag-word",
            "label": name.replace("_", " "),
            "value": name,
            "category": CAT_TO_ID.get((row.get("category") or "general").lower(), 0),
            "post_count": row.get("usage_count", 0),
        })
    h.send_json(out, head=head)


def h_tags_autocomplete(h, m, p, head):  # older Danbooru versions
    word = (p.get("search[name_matches]") or "").strip()
    if not word:
        return h.send_json([], head=head)
    res = mb_json("GET", "/tags", {"q": word, "type": "tag", "sort": "usage",
                                   "limit": to_int(p.get("limit"), 10, 1, 50), "show_zero": "0"})
    h.send_json([to_tag(r) for r in res.get("results") or []], head=head)


def h_profile(h, m, p, head):
    h.send_json({
        "id": 1, "name": "monbooru", "level": 20, "level_string": "Member", "created_at": EPOCH,
        "inviter_id": None, "post_upload_count": 0, "post_update_count": 0, "note_update_count": 0,
        "is_banned": False, "can_upload_free": False, "favorite_count": 0, "blacklisted_tags": "",
        "favorite_tags": "", "comment_threshold": -20, "default_image_size": "large",
        "per_page": 20, "show_deleted_children": False, "api_regen_multiplier": 1,
    }, head=head)


def h_favorites(h, m, p, head):
    params = {"q": "fav:true", "page": parse_page(p.get("page")), "limit": to_int(p.get("limit"), 20, 1, 200)}
    data = mb_json("GET", "/images/search", params)
    h.send_json([{"id": i["id"], "user_id": 1, "post_id": i["id"]} for i in data.get("results") or []], head=head)


def _set_favorite(post_id, value):
    return mb_json("PATCH", f"/images/{post_id}", body={"is_favorited": value})


def h_fav_create(h, m, p, head):
    if not FAVORITES:
        return h.send_json({"success": False, "error": "favorites are disabled (set BRIDGE_FAVORITES=1)"}, 403)
    pid = to_int(p.get("post_id"), 0, 0, 10 ** 12)
    if not pid:
        return h.send_json({"success": False, "error": "post_id missing"}, 400)
    h.send_json(to_post(_set_favorite(pid, True), h.base_url()))


def h_fav_delete(h, m, p, head):
    if not FAVORITES:
        return h.send_json({"success": False, "error": "favorites are disabled (set BRIDGE_FAVORITES=1)"}, 403)
    _set_favorite(int(m.group(1)), False)
    h.send_empty(204)


FAVICON_URL = env("BRIDGE_FAVICON_URL") or f"{MONBOORU_URL}/favicon.ico"
_FAVICON = {"data": None, "type": "image/x-icon", "at": 0.0}


def h_favicon(h, m, p, head):
    """Pass Monbooru's icon through (cached for a day) so the bridge looks like Monbooru."""
    if _FAVICON["data"] is None or time.time() - _FAVICON["at"] > 86400:
        try:
            req = urllib.request.Request(FAVICON_URL, headers={"User-Agent": f"{APP_NAME}/{VERSION}"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                _FAVICON.update(data=resp.read(1 << 20), at=time.time(),
                                type=resp.headers.get("Content-Type") or "image/x-icon")
        except Exception as exc:  # noqa: BLE001
            log.warning("favicon from %s not available: %s", FAVICON_URL, exc)
            if _FAVICON["data"] is None:
                return h.send_empty(404)
    data = _FAVICON["data"]
    h.status, h.sent = 200, True
    h.send_response(200)
    h.send_header("Content-Type", _FAVICON["type"])
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "public, max-age=86400")
    h.end_headers()
    if not head:
        h.wfile.write(data)


def h_stub_list(h, m, p, head):
    h.send_json([], head=head)


def h_stub_related(h, m, p, head):
    h.send_json({"query": p.get("query", ""), "related_tags": [], "wiki_page_tags": []}, head=head)


def _file_route(upstream):
    def handler(h, m, p, head):
        h.stream(upstream.format(id=int(m.group(1))), head)
    return handler


ROUTES = [  # (method, path regex, handler, access) - access: open | api | media
    ("GET", r"^/(?:health|up)$", h_health, "open"),
    ("GET", r"^/(?:index\.json)?$", h_health, "open"),
    ("GET", r"^/(?:favicon\.ico|apple-touch-icon(?:-precomposed)?\.png)$", h_favicon, "open"),
    ("GET", r"^/posts\.json$", h_posts, "api"),
    ("GET", r"^/posts/random\.json$", h_post_random, "api"),
    ("GET", r"^/posts/(\d+)\.json$", h_post, "api"),
    ("GET", r"^/counts/posts\.json$", h_counts, "api"),
    ("GET", r"^/tags\.json$", h_tags, "api"),
    ("GET", r"^/autocomplete\.json$", h_autocomplete, "api"),
    ("GET", r"^/tags/autocomplete\.json$", h_tags_autocomplete, "api"),
    ("GET", r"^/(?:profile|users/\d+)\.json$", h_profile, "api"),
    ("GET", r"^/favorites\.json$", h_favorites, "api"),
    ("POST", r"^/favorites\.json$", h_fav_create, "api"),
    ("DELETE", r"^/favorites/(\d+)(?:\.json)?$", h_fav_delete, "api"),
    ("GET", r"^/related_tag\.json$", h_stub_related, "api"),
    ("GET", r"^(?:" + "|".join(STUB_LISTS) + r")\.json$", h_stub_list, "api"),
    ("GET", r"^/data/(\d+)\.\w+$", _file_route("/images/{id}/file"), "media"),
    ("GET", r"^/sample/(\d+)\.\w+$", _file_route("/images/{id}/still"), "media"),
    ("GET", r"^/preview/(\d+)\.\w+$", _file_route("/images/{id}/thumbnail"), "media"),
]
ROUTES = [(meth, re.compile(rx), fn, access) for meth, rx, fn, access in ROUTES]

# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"{APP_NAME}/{VERSION}"
    timeout = 60  # close idle keep-alive connections instead of holding a thread forever

    def log_message(self, fmt, *args):  # replaced by our own access log
        pass

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("HEAD")

    def do_POST(self):
        self.dispatch("POST")

    def do_DELETE(self):
        self.dispatch("DELETE")

    # -- helpers used by handlers ------------------------------------------

    def base_url(self):
        if PUBLIC_URL:
            return PUBLIC_URL
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or f"127.0.0.1:{PORT}"
        return f"{proto}://{host}"

    def send_json(self, obj, status=200, head=False, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.status = status
        self.sent = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def send_empty(self, status):
        self.status = status
        self.sent = True
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def stream(self, upstream_path, head):
        headers = {}
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        resp = mb_open("GET", upstream_path, headers=headers)
        try:
            self.status = resp.status
            self.sent = True
            self.send_response(resp.status)
            for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
                if resp.headers.get(name):
                    self.send_header(name, resp.headers[name])
            self.send_header("Cache-Control", "public, max-age=86400")
            if not resp.headers.get("Content-Length"):
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if not head:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        finally:
            resp.close()

    # -- access control ---------------------------------------------------------

    def authorized(self, params, access):
        if access == "open" or not API_KEY:
            return True
        if access == "media":
            return hmac.compare_digest(params.get("k", "").encode(), MEDIA_TOKEN.encode())
        candidates = [params.get("api_key", "")]
        scheme, _, value = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() == "bearer":
            candidates.append(value.strip())
        elif scheme.lower() == "basic":
            try:
                candidates.append(base64.b64decode(value).decode("utf-8", "replace").partition(":")[2])
            except ValueError:
                pass
        return any(c and hmac.compare_digest(c.encode(), API_KEY.encode()) for c in candidates)

    def deny(self, method, params, access):
        scheme = self.headers.get("Authorization", "").partition(" ")[0] or "-"
        log.warning("401 %s %s (no valid key; params=%s, Authorization=%s)",
                    method, self.safe_path.split("?")[0], sorted(params), scheme)
        headers = {"WWW-Authenticate": 'Basic realm="danbooru-bridge"'} if access == "api" else None
        self.send_json({"success": False, "error": "Unauthorized", "message": "missing or invalid api_key"},
                       401, head=method == "HEAD", headers=headers)

    # -- dispatch -------------------------------------------------------------

    def read_params(self, parsed):
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
        length = to_int(self.headers.get("Content-Length"), 0, 0, 1 << 20)
        if length:
            raw = self.rfile.read(length)
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            try:
                if ctype == "application/json":
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        params.update({k: str(v) for k, v in obj.items()})
                else:
                    form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
                    params.update({k: v[-1] for k, v in form.items()})
            except ValueError:
                pass
        return params

    def dispatch(self, method):
        started = time.time()
        self.status = 0
        self.sent = False
        self.safe_path = re.sub(r"\b(api_key|k)=[^&]*", r"\1=***", self.path)  # never log secrets
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        try:
            params = self.read_params(parsed)
            lookup = "GET" if method == "HEAD" else method
            for meth, rx, fn, access in ROUTES:
                match = rx.match(path) if meth == lookup else None
                if match:
                    if access != "open" and not client_allowed(self.client_address[0]):
                        log.warning("403 %s %s from %s (not in BRIDGE_ALLOW)",
                                    method, self.safe_path.split("?")[0], self.client_address[0])
                        self.send_json({"success": False, "error": "Forbidden"}, 403, head=method == "HEAD")
                    elif self.authorized(params, access):
                        fn(self, match, params, method == "HEAD")
                    else:
                        self.deny(method, params, access)
                    break
            else:
                log.warning("UNHANDLED %s %s", method, self.safe_path)
                self.send_json({"success": False, "error": "NotFound",
                                "message": "not implemented by danbooru-bridge"}, 404, head=method == "HEAD")
        except MonbooruError as exc:
            log.warning("%s %s -> %s", method, self.safe_path, exc)
            if not self.sent:
                code = {404: 404, 403: 403, 503: 503}.get(exc.status, 502)
                err = "RecordNotFound" if code == 404 else "UpstreamError"
                self.send_json({"success": False, "error": err, "message": exc.detail[:200]}, code)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("%s %s failed", method, self.safe_path)
            if not self.sent:
                self.send_json({"success": False, "error": "InternalError"}, 500)
        log.info("%s %s -> %s (%d ms)", method, self.safe_path, self.status, (time.time() - started) * 1000)


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A phone app closing its keep-alive connections resets them; that is not an error.
        if isinstance(sys.exc_info()[1], (ConnectionError, socket.timeout, TimeoutError)):
            return
        log.exception("error while handling a request from %s", client_address)


def watch_stdin():
    """Monbooru stops a managed plugin by closing its stdin."""
    try:
        while sys.stdin.buffer.read(4096):
            pass
    except Exception:  # noqa: BLE001
        pass
    log.info("stdin closed, exiting")
    os._exit(0)


def main():
    logging.basicConfig(level=env("BRIDGE_LOG", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    os.makedirs(DATA_DIR, exist_ok=True)
    if "--managed" in sys.argv:
        threading.Thread(target=watch_stdin, daemon=True).start()
    server = BridgeServer((BIND, PORT), Handler)
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))  # PID 1 in a container ignores SIGTERM otherwise
    threading.Thread(target=pair_loop, daemon=True).start()
    log.info("%s %s listening on %s:%d, Monbooru at %s", APP_NAME, VERSION, BIND, PORT, MONBOORU_URL)
    if not API_KEY:
        log.warning("BRIDGE_API_KEY is not set - anyone who can reach this port can read the library")
    try:
        server.serve_forever(poll_interval=30)  # nothing to poll; wake up only for real connections
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
