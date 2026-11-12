"""Auth via Netscape cookies.txt — 100% compatible with gallery-dl / yt-dlp.

gallery-dl and yt-dlp load cookies with http.cookiejar.MozillaCookieJar
which requires:
  - Line 1: "# Netscape HTTP Cookie File" or "# HTTP Cookie File"
  - 7 fields separated by TAB: domain\\tTRUE/FALSE\\tpath\\tTRUE/FALSE\\texpires\\tname\\tvalue
  - Domain with "." prefix (e.g. .tiktok.com) = sent to subdomains (TRUE)
  - Expiry 0 = session cookie

This module replicates that behavior so the same cookies.txt works
in tikstractormf, gallery-dl and yt-dlp without conversion.
"""

from __future__ import annotations

import http.cookiejar
import time
from pathlib import Path
from typing import Any

import httpx


def _parse_netscape_lines(path: Path) -> list[str]:
    """Normalise a Netscape cookie file to TAB-separated lines, **in memory**.

    Tolerates the things real exports contain:
    - a ``scrapmf-source`` line above the MAGIC header
    - spaces instead of TABs (many generators emit spaces)
    - ``#HttpOnly_`` prefixes, which are stripped here and re-applied by
      :func:`load_netscape_cookies`
    - empty and comment lines, and malformed rows

    Returns the normalised lines; nothing is written to disk. This used to build
    a temp file purely because ``MozillaCookieJar.load()`` demands a filename,
    which put live session cookies on disk as ``0o644``.
    """
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    # Find MAGIC header
    magic_idx = -1
    for i, line in enumerate(lines):
        if "HTTP Cookie File" in line:
            magic_idx = i
            break
    if magic_idx == -1:
        raise http.cookiejar.LoadError(f"{path} does not look like a Netscape format cookies file")
    # Rebuild from MAGIC onwards. The header itself is NOT returned: it carries
    # no cookie, only MozillaCookieJar needed it to validate the file, and
    # ensure_netscape_header() already does that.
    out_lines: list[str] = []
    for raw_line in lines[magic_idx + 1 :]:
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and "HTTP Cookie File" in stripped:
            continue
        if stripped.startswith("#") and not stripped.startswith("#HttpOnly_"):
            # regular comment: keep pure comments?
            # gallery-dl ignores them, we skip them
            if stripped.startswith("# This"):
                continue
            # other comments: ignore
            if stripped.startswith("#"):
                # if it is #HttpOnly_, processed below
                if not stripped.startswith("#HttpOnly_"):
                    continue
        # Keep the #HttpOnly_ prefix: it is a real cookie attribute and the
        # consumer re-applies it as rest={"HttpOnly": ""}.
        httponly = raw_line.startswith("#HttpOnly_")
        line = raw_line[len("#HttpOnly_") :] if httponly else raw_line
        # If line still starts with #, it is a comment -> skip
        if line.strip().startswith("#"):
            continue
        # Split by TAB or by 2+ spaces
        # MozillaCookieJar requires TABs, so we convert
        # First split by TAB if tabs are present
        if "\t" in line:
            parts = line.split("\t")
        else:
            # split by whitespace 2+ or \s+ but preserving values with spaces?
            # value may contain spaces / JSON -> use maxsplit=6
            import re

            parts = re.split(r"\s+", line.strip(), maxsplit=6)
        if len(parts) != 7:
            # malformed line, skip silently (gallery-dl does the same)
            continue
        domain, flag, cpath, secure, expires, name, value = parts
        # Rebuild with TABs, re-attaching the prefix so the HttpOnly flag is
        # not lost.
        rebuilt = "\t".join([domain, flag, cpath, secure, expires, name, value])
        out_lines.append(f"#HttpOnly_{rebuilt}" if httponly else rebuilt)
    return out_lines


def load_netscape_cookies(
    path: str | Path, *, ignore_expires: bool = True, ignore_discard: bool = True
) -> http.cookiejar.CookieJar:
    """Load a Netscape cookies.txt and return a CookieJar ready for httpx.

    Returns a CookieJar (compatible with ``httpx.Client(cookies=jar)``).
    Tolerates scrapmf-source lines, spaces vs TABs, ``#HttpOnly_``, empty lines,
    and rows whose include-subdomains flag disagrees with the domain's leading
    dot. A CookieJar rather than ``httpx.Cookies`` avoids CookieConflict when the
    same name exists on different domains (e.g. sessionid on tiktok +
    instagram).

    The file is parsed in memory. It used to be rewritten into a
    ``tempfile.mktemp()`` file because ``MozillaCookieJar.load()`` requires a
    filename, which left live session cookies on disk as world-readable
    ``0o644`` and exposed a symlink race before the write.
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"cookies file not found: {p}")

    # Surface the actionable message first: exporting cookies as JSON is by far
    # the most common mistake, and the bare LoadError does not hint at it.
    ensure_netscape_header(p)

    jar = http.cookiejar.CookieJar()
    now = time.time()
    for raw in _parse_netscape_lines(p):
        httponly = raw.startswith("#HttpOnly_")
        if httponly:
            raw = raw[len("#HttpOnly_") :]
        domain, flag, cpath, secure, expires, name, value = raw.split("\t")

        initial_dot = domain.startswith(".")
        # MozillaCookieJar asserts domain_specified == initial_dot, so a file
        # whose flag disagrees with its own domain aborted the entire load with
        # an AssertionError. Derive the flag from the domain instead, which is
        # what the Netscape format actually means.
        domain_specified = flag == "TRUE" or initial_dot

        # expires == "" and expires == "0" both mean "session cookie". Reading
        # "0" as a timestamp would date it to 1970 and drop a live cookie.
        try:
            expires_at = int(expires) if expires not in ("", "0") else None
        except ValueError:
            expires_at = None
        discard = expires_at is None
        if ignore_discard:
            discard = False
        if not ignore_expires and expires_at is not None and expires_at < now:
            continue

        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=domain_specified,
                domain_initial_dot=initial_dot,
                path=cpath,
                # MozillaCookieJar assumes path_specified is false.
                path_specified=False,
                secure=secure == "TRUE",
                expires=expires_at,
                discard=discard,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": ""} if httponly else {},
            )
        )
    return jar


def load_cookies_dict(
    cookies: dict[str, str], *, domain: str = ".tiktok.com"
) -> http.cookiejar.CookieJar:
    """Build a CookieJar from a plain ``{name: value}`` mapping.

    Used by ``Tiktokscraper`` so every accepted ``cookies`` input ends up in the
    same shape, which the Playwright cookie converter requires.
    """
    jar = http.cookiejar.CookieJar()
    for k, v in cookies.items():
        c = http.cookiejar.Cookie(
            version=0,
            name=k,
            value=v,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=domain.startswith("."),
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
        )
        jar.set_cookie(c)
    return jar


def get_csrf_token(cookies: http.cookiejar.CookieJar | httpx.Cookies) -> str | None:
    """Extract a CSRF-like token from cookies (TikTok uses msToken/tt_chain_token)."""
    names = ("csrftoken", "msToken", "tt_chain_token", "sessionid")
    if isinstance(cookies, httpx.Cookies):
        for n in names:
            v = cookies.get(n)
            if v:
                return str(v)
        return None
    for c in cookies:
        if getattr(c, "name", None) in names:
            value = getattr(c, "value", None)
            if value:
                return str(value)
    return None
def _load_brave_profile(profile_dir: str, domain: str, browser_cookie3: Any) -> Any:
    """Read cookies from a specific Brave profile (Origin, Beta, Nightly).

    ``browser_cookie3.brave()`` only resolves the default ``Brave-Browser``
    profile, so a live session kept in ``Brave-Origin`` is invisible to it. The
    ``Brave`` class takes explicit paths, which is what this uses; the
    ``Local State`` file next to it holds the encryption key.
    """
    import browser_cookie3 as _bc3  # noqa: F401  (resolved by caller)

    root = Path.home() / ".config" / "BraveSoftware" / profile_dir
    cookie_file = root / "Default" / "Cookies"
    key_file = root / "Local State"
    if not cookie_file.exists():
        raise FileNotFoundError(
            f"no Brave profile at {cookie_file}. "
            f"Is '{profile_dir}' installed, or is this a non-Linux layout?"
        )
    return browser_cookie3.Brave(
        str(cookie_file), domain_name=domain, key_file=str(key_file)
    ).load()


def _brave_variant(profile_dir: str, browser_cookie3: Any) -> Any:
    """Build a loader bound to one Brave profile directory."""
    return lambda domain: _load_brave_profile(profile_dir, domain, browser_cookie3)


# Flatpak application ids for browsers that ship as sandboxes.
_FLATPAK_IDS = {
    "brave": "com.brave.Browser",
    "brave-browser": "com.brave.Browser",
    "brave-origin": "com.brave.Browser",
    "brave-origin-beta": "com.brave.Browser",
    "brave-origin-nightly": "com.brave.Browser",
    "brave-beta": "com.brave.Browser",
    "brave-nightly": "com.brave.Browser",
    "chrome": "com.google.Chrome",
    "chromium": "org.chromium.Chromium",
    "edge": "com.microsoft.Edge",
    "vivaldi": "com.vivaldi.Vivaldi",
    "opera": "com.opera.Opera",
}


def _flatpak_profile_dir(browser: str) -> Path | None:
    """Return the Flatpak profile root for `browser`, or None if not installed
    as a Flatpak."""
    app_id = _FLATPAK_IDS.get(browser.lower())
    if app_id is None:
        return None
    home = Path.home()
    base = home / ".var" / "app" / app_id / "config"
    # Brave keeps per-channel directories.
    rel_map = {
        "brave": "BraveSoftware/Brave-Browser",
        "brave-browser": "BraveSoftware/Brave-Browser",
        "brave-origin": "BraveSoftware/Brave-Origin",
        "brave-origin-beta": "BraveSoftware/Brave-Origin-Beta",
        "brave-origin-nightly": "BraveSoftware/Brave-Origin-Nightly",
        "brave-beta": "BraveSoftware/Brave-Browser-Beta",
        "brave-nightly": "BraveSoftware/Brave-Browser-Nightly",
        "chrome": "google-chrome",
        "chromium": "chromium",
        "edge": "microsoft-edge",
        "vivaldi": "vivaldi",
        "opera": "opera",
    }
    rel = rel_map.get(browser.lower())
    if rel is None:
        return None
    root = base / rel
    if not root.is_dir():
        return None
    return root


def _is_flatpak_browser(browser: str) -> bool:
    return _flatpak_profile_dir(browser) is not None


def _decrypt_chromium_value(
    encrypted: bytes, key: bytes, host: str
) -> str | None:
    """Decrypt a Chromium v10/v11 cookie value. Returns None on failure."""
    if not encrypted or len(encrypted) < 4:
        return None
    prefix = encrypted[:3]
    if prefix not in (b"v10", b"v11", b"v12"):
        # Unencrypted (old) value.
        try:
            return encrypted.decode("utf-8", errors="replace")
        except Exception:
            return None
    body = encrypted[3:]
    if len(body) < 16:
        return None
    try:
        from Cryptodome.Cipher import AES
    except Exception:
        try:
            from Crypto.Cipher import AES  # type: ignore
        except Exception:
            return None
    iv = b" " * 16
    try:
        cipher = AES.new(key, AES.MODE_CBC, iv)
        padded = cipher.decrypt(body)
    except Exception:
        return None
    # PKCS#7 unpad, validated.
    if not padded:
        return None
    pad = padded[-1]
    if pad < 1 or pad > 16 or padded[-pad:] != bytes([pad]) * pad:
        return None
    plain = padded[:-pad]
    # Schema >=24 prefixes a SHA-256(host) domain hash; strip if it matches.
    import hashlib

    host_hash = hashlib.sha256(host.encode()).digest()[:32]
    if len(plain) >= 32 and plain[:32] == host_hash[:32]:
        plain = plain[32:]
    try:
        return plain.decode("utf-8")
    except Exception:
        return None


def _chromium_key_from_secret_service(apps: list[str]) -> bytes | None:
    """Try to recover the safe-storage key via secret-tool for one of `apps`."""
    import subprocess

    for app in apps:
        for schema in ("chrome_libsecret_os_crypt_password_v2", None):
            cmd = ["secret-tool", "lookup"]
            if schema:
                cmd += ["xdg:schema", schema, "application", app]
            else:
                cmd += ["application", app]
            try:
                out = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=5
                )
            except Exception:
                continue
            if out.returncode == 0 and out.stdout:
                pw = out.stdout.rstrip("\n")
                if pw:
                    import hashlib

                    key = hashlib.pbkdf2_hmac(
                        "sha1", pw.encode(), b"saltysalt", 1, dklen=16
                    )
                    return key
    # Empty key (KWallet race) fallback.
    import hashlib

    return hashlib.pbkdf2_hmac("sha1", b"", b"saltysalt", 1, dklen=16)


def _load_firefox_direct(domain: str = "tiktok.com") -> http.cookiejar.CookieJar | None:
    """Read cookies directly from Firefox's SQLite DB.

    Firefox does not encrypt its cookies, so we can read them with the
    built-in sqlite3 module without browser_cookie3. This is more robust
    than browser_cookie3, which sometimes fails to find the profile
    directory (especially on Debian/Fedora with ESR or snap/flatpak).
    """
    import sqlite3

    home = Path.home()
    candidates = [
        home / ".mozilla" / "firefox",
        home / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
        home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
    ]

    profile_dir = None
    for base in candidates:
        if not base.is_dir():
            continue
        # Look for profiles.ini to find the active profile.
        ini = base / "profiles.ini"
        if ini.is_file():
            try:
                import configparser

                cfg = configparser.ConfigParser()
                cfg.read(str(ini))
                for section in cfg.sections():
                    if section.startswith("Profile"):
                        path_val = cfg.get(section, "Path", fallback=None)
                        if path_val:
                            p = base / path_val
                            if (p / "cookies.sqlite").is_file():
                                profile_dir = p
                                break
            except Exception:
                pass
        if profile_dir is not None:
            break
        # Fallback: pick the most recent *.default* directory with cookies.sqlite.
        if profile_dir is None:
            try:
                profiles = sorted(
                    (p for p in base.iterdir() if p.is_dir() and (p / "cookies.sqlite").is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                for p in profiles:
                    if "default" in p.name.lower():
                        profile_dir = p
                        break
                if profile_dir is None and profiles:
                    profile_dir = profiles[0]
            except Exception:
                pass
        if profile_dir is not None:
            break

    if profile_dir is None:
        return None

    db_path = profile_dir / "cookies.sqlite"
    if not db_path.is_file():
        return None

    # Firefox may lock the DB while running; copy to a temp file to be safe.
    import shutil
    import tempfile

    try:
        tmp = Path(tempfile.mktemp(suffix=".sqlite"))
        shutil.copy2(db_path, tmp)
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    except Exception:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            tmp = None
        except Exception:
            return None

    rows = []
    try:
        cur = conn.execute(
            "SELECT name, value, host, path, expiry, isSecure, isHttpOnly "
            "FROM moz_cookies"
        )
        rows = cur.fetchall()
    except Exception:
        conn.close()
        if tmp is not None:
            try:
                tmp.unlink()
            except Exception:
                pass
        return None
    conn.close()
    if tmp is not None:
        try:
            tmp.unlink()
        except Exception:
            pass

    target = domain.lstrip(".")
    jar = http.cookiejar.CookieJar()
    for name, value, host, path, expiry, is_secure, is_httponly in rows:
        hk = (host or "").lstrip(".")
        if not (hk == target or hk.endswith("." + target) or target.endswith("." + hk)):
            continue
        initial_dot = (host or "").startswith(".")
        expires = expiry if expiry and expiry > 0 else None
        try:
            ck = http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=host,
                domain_specified=True,
                domain_initial_dot=initial_dot,
                path=path or "/",
                path_specified=True,
                secure=bool(is_secure),
                expires=expires,
                discard=expires is None,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": ""} if is_httponly else {},
            )
            jar.set_cookie(ck)
        except Exception:
            continue
    return jar


def _load_flatpak_chromium(browser: str, domain: str) -> http.cookiejar.CookieJar | None:
    """Read cookies directly from a Flatpak browser's SQLite DB.

    browser_cookie3 cannot be used here because it tries to load the Flatpak's
    runtime Python (libpython3.14.so.1.0), which is not visible to the host.
    """
    import sqlite3

    root = _flatpak_profile_dir(browser)
    if root is None:
        return None

    # Find the Cookies DB. Brave keeps per-profile subdirs.
    db_path = None
    for cand in (
        root / "Default" / "Cookies",
        root / "Cookies",
    ):
        if cand.is_file():
            db_path = cand
            break
    if db_path is None:
        # Search one level.
        for p in root.rglob("Cookies"):
            db_path = p
            break
    if db_path is None:
        return None

    # Secret Service app attributes to try.
    app_candidates = {
        "brave": ["brave", "brave-origin"],
        "brave-browser": ["brave", "brave-origin"],
        "brave-origin": ["brave", "brave-origin"],
        "brave-origin-beta": ["brave", "brave-origin"],
        "brave-origin-nightly": ["brave", "brave-origin"],
        "brave-beta": ["brave", "brave-origin"],
        "brave-nightly": ["brave", "brave-origin"],
        "chrome": ["chrome", "chromium"],
        "chromium": ["chromium", "chrome"],
        "edge": ["microsoft-edge", "chrome"],
        "vivaldi": ["vivaldi", "chrome"],
        "opera": ["opera", "chrome"],
    }.get(browser, ["chrome"])

    key = _chromium_key_from_secret_service(app_candidates)
    if key is None:
        return None

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        return None
    rows = []
    try:
        cur = conn.execute(
            "SELECT host_key, name, value, path, expires_utc, is_secure, "
            "is_httponly, encrypted_value FROM cookies"
        )
        rows = cur.fetchall()
    except Exception:
        conn.close()
        return None
    conn.close()

    # Filter to the requested domain.
    def _host_matches(host_key: str) -> bool:
        hk = host_key.lstrip(".")
        target = domain.lstrip(".")
        return hk == target or hk.endswith("." + target) or target.endswith("." + hk)

    jar = http.cookiejar.CookieJar()
    for host_key, name, value, path, expires_utc, is_secure, is_httponly, enc in rows:
        if not _host_matches(host_key or ""):
            continue
        # Decrypt if needed.
        if value:
            cookie_value = value
        else:
            dec = _decrypt_chromium_value(enc or b"", key, host_key or "")
            if dec is None:
                continue
            cookie_value = dec
        # expires_utc is microseconds since 1601.
        if expires_utc and expires_utc > 0:
            expires = expires_utc / 1_000_000 - 11644473600
        else:
            expires = None
        initial_dot = (host_key or "").startswith(".")
        try:
            ck = http.cookiejar.Cookie(
                version=0,
                name=name,
                value=cookie_value,
                port=None,
                port_specified=False,
                domain=host_key,
                domain_specified=True,
                domain_initial_dot=initial_dot,
                path=path or "/",
                path_specified=True,
                secure=bool(is_secure),
                expires=expires,
                discard=expires is None,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": ""} if is_httponly else {},
            )
            jar.set_cookie(ck)
        except Exception:
            continue
    return jar


def load_from_browser(browser: str, *, domain: str = "tiktok.com") -> http.cookiejar.CookieJar:
    """Load cookies directly from the browser (brave/chrome/firefox)
    like gallery-dl --cookies-from-browser.

    Requires: pip install browser-cookie3
    On Linux copies the DB to /tmp to avoid database locked while the browser is open.
    Note: browser-cookie3 may leave a temporary copy of the cookie DB in /tmp;
    ensure /tmp is cleared on reboot or use a private temp dir.
    """
    try:
        import browser_cookie3  # type: ignore
    except ImportError as e:
        raise ImportError(
            "browser-cookie3 not installed. "
            "pip install 'tikstractormf[browser]' or 'browser-cookie3'"
        ) from e

    b = browser.lower()

    # Firefox: read directly from cookies.sqlite (unencrypted) instead of
    # browser_cookie3, which sometimes fails to find the profile directory.
    if b in ("firefox", "firefox-esr", "firefox-nightly"):
        firefox_jar = _load_firefox_direct(domain)
        if firefox_jar is not None and len(firefox_jar) > 0:
            return firefox_jar
        # Fall through to browser_cookie3 if direct read found nothing.

    # Flatpak browsers: browser_cookie3 cannot read them because their runtime
    # Python is not visible to the host (libpython3.14.so.1.0 missing). Read
    # the cookie DB directly instead.
    if _is_flatpak_browser(b):
        flatpak_jar = _load_flatpak_chromium(b, domain)
        if flatpak_jar is not None:
            return flatpak_jar
        raise ValueError(
            f"could not read cookies from Flatpak browser '{browser}'. "
            "Export cookies.txt manually (ElDevoraCucas) or use scrapmf's "
            "cookie capture (Configuration -> Cookie profiles)."
        )

    # Brave ships several independent profiles under the same config root, and
    # browser_cookie3 only has a shortcut for the default one:
    #
    #   browser_cookie3.brave()  ->  BraveSoftware/Brave-Browser   (the *stale* one)
    #
    # A user whose live session lives in Brave-Origin therefore gets an old jar
    # without any sign of it. The class accepts explicit paths, so each variant
    # is pointed at its own profile directory.
    loaders = {
        "brave": lambda d: browser_cookie3.brave(domain_name=d),
        "brave-browser": lambda d: browser_cookie3.brave(domain_name=d),
        "brave-origin": _brave_variant("Brave-Origin", browser_cookie3),
        "brave-nightly": _brave_variant("Brave-Browser-Nightly", browser_cookie3),
        "brave-beta": _brave_variant("Brave-Beta", browser_cookie3),
        "chrome": lambda d: browser_cookie3.chrome(domain_name=d),
        "chromium": lambda d: browser_cookie3.chromium(domain_name=d),
        "firefox": lambda d: browser_cookie3.firefox(domain_name=d),
        "edge": lambda d: browser_cookie3.edge(domain_name=d),
        "opera": lambda d: browser_cookie3.opera(domain_name=d),
        "vivaldi": lambda d: browser_cookie3.vivaldi(domain_name=d),
    }
    loader = loaders.get(b)
    if loader is None:
        raise ValueError(
            f"unsupported browser: {browser} (use {'|'.join(sorted(loaders))})"
        )
    jar: http.cookiejar.CookieJar = loader(domain)

    return jar


def ensure_netscape_header(path: str | Path) -> None:
    """Validate that the file carries the Netscape MAGIC header.

    The header is searched for anywhere in the file, not just on the first line,
    matching :func:`_parse_netscape_lines`: some exporters (scrapmf among them)
    prepend a source comment before the real header. Checking only line 1 would
    reject files the parser is happy to read.
    """
    p = Path(path)
    if not p.exists():
        return
    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    if not any("HTTP Cookie File" in line for line in lines):
        raise http.cookiejar.LoadError(
            f"{p} does not look like a Netscape format cookies file. "
            "Export with 'Get cookies.txt LOCALLY' (Netscape), not JSON. "
            "The file must contain a line '# Netscape HTTP Cookie File'."
        )
