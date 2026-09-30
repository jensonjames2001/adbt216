"""Shared scaffolding for the Phase 0 probes.

Every probe imports this module and gets the same behaviour:

* the repo root on ``sys.path``, so ``python probes/probe_x.py`` and
  ``python -m probes.probe_x`` both work from the repo root;
* ``.env`` and ``config.yaml`` loaded exactly as the service will load them;
* one output folder per run: ``fixtures/<name>/live/<YYYYmmddTHHMMSSZ>/``;
* responses saved with every key, token and password stripped from URLs and
  headers before anything touches the disk (build-brief rule 3);
* a request budget that prints the plan and refuses to exceed a per-run cap
  (rule 4);
* one shared HTTP client: 10 s timeout, honest User-Agent, no retries, no
  redirect following (rules 6 and 9).

Nothing in this module touches the network by itself.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, NoReturn, Sequence
from urllib.parse import unquote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402  (after the sys.path fix on purpose)

from rgalerts.config import load_config, load_dotenv, redact, secret  # noqa: E402,F401

USER_AGENT = "rgalerts-probe/0.1 (+https://recoverygiantuk.com)"
TIMEOUT_S = 10.0

# Exit codes shared by all probes.
EXIT_OK = 0            # ran to the end
EXIT_FAILED = 1        # a request failed or the provider answered with an error
EXIT_BLOCKED = 2       # Waze only: this machine is refused
EXIT_MISSING_KEY = 3   # a required value is missing from .env

NO_TZDATA_MESSAGE = (
    "No time-zone database found (normal on Windows). Install it with\n"
    "    .venv\\Scripts\\python -m pip install tzdata\n"
    "(Mac/Linux: .venv/bin/python -m pip install tzdata) and run this again."
)

try:
    LONDON = ZoneInfo("Europe/London")
except ZoneInfoNotFoundError:
    # Windows Python ships no tz database; the tzdata package supplies one.
    # Stop here with a plain message rather than a traceback (rule 7 needs
    # Europe/London for every displayed time).
    print(NO_TZDATA_MESSAGE, file=sys.stderr, flush=True)
    sys.exit(EXIT_FAILED)

# Query parameters whose VALUE is a secret. Matched case-insensitively, and
# any parameter whose name contains one of _SECRET_WORDS is masked as well.
SECRET_QUERY_PARAMS = {
    "key", "apikey", "api_key", "subscription-key", "subscription_key",
    "token", "access_token", "auth", "password", "secret",
}
SECRET_HEADER_NAMES = {
    "ocp-apim-subscription-key", "authorization", "proxy-authorization",
    "x-api-key", "cookie", "set-cookie",
}
_SECRET_WORDS = ("key", "token", "secret", "password")
# Environment variables whose values must never appear in output.
SECRET_ENV_NAMES = ("TELEGRAM_BOT_TOKEN", "TOMTOM_API_KEY", "NH_API_KEY", "WAZE_PROVIDER_KEY")
# Any other environment variable counts as a secret only when its name ENDS
# with one of these (AWS_SECRET_ACCESS_KEY, GITHUB_TOKEN, ...) or is exactly
# one of _SECRET_ENV_EXACT. A name that merely contains the word, such as
# GNOME_KEYRING_CONTROL or KEYBOARD_LAYOUT, is not: scrubbing its value would
# replace unrelated text in findings.md with ***.
_SECRET_ENV_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASS", "APIKEY", "API_TOKEN")
_SECRET_ENV_EXACT = ("KEY", "TOKEN", "SECRET", "PASSWORD")

_BOT_PATH = re.compile(r"/bot[^/]+")   # Telegram: /bot<token>/method


def _utf8_console() -> None:
    """Windows consoles and redirected output may not be UTF-8; the alert
    text contains emoji, so never let printing raise."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


_utf8_console()


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

def now_utc() -> datetime:
    """The current time as a tz-aware UTC datetime (rule 7)."""
    return datetime.now(timezone.utc)


def london(dt: datetime) -> str:
    """'HH:MM' in Europe/London for display. Naive datetimes are taken as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LONDON).strftime("%H:%M")


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def run_dir_name(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------

def is_secret_env_name(name: str) -> bool:
    """True for the service's own secrets and for names that end in _KEY,
    _TOKEN, _SECRET, _PASSWORD and the like. See _SECRET_ENV_SUFFIXES."""
    upper = name.strip().upper()
    return (upper in SECRET_ENV_NAMES or upper in _SECRET_ENV_EXACT
            or upper.endswith(_SECRET_ENV_SUFFIXES))


def known_secrets() -> list[str]:
    """Every secret-looking value in the environment, so that any text we are
    about to print or save can be scrubbed of it as a last line of defence."""
    values: list[str] = []
    for name, value in os.environ.items():
        if is_secret_env_name(name):
            v = value.strip()
            if len(v) >= 8 and v not in values:   # short values would over-match
                values.append(v)
    return values


def scrub(text: str) -> str:
    """Replace every known secret value in text with '***'."""
    return redact(str(text), *known_secrets())


def _is_secret_name(name: str) -> bool:
    n = unquote(name).strip().lower()
    return n in SECRET_QUERY_PARAMS or any(w in n for w in _SECRET_WORDS)


def redacted_url(url: str) -> str:
    """The URL with secret query values masked, '/bot<token>/' path segments
    replaced by '/bot***/' and any user:password@ dropped. Percent-encoding of
    the other parameters is kept as it was."""
    parts = urlsplit(str(url))
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "***@" + netloc.rsplit("@", 1)[1]
    path = _BOT_PATH.sub("/bot***", parts.path)
    pieces: list[str] = []
    for piece in parts.query.split("&") if parts.query else []:
        if not piece:
            continue
        name, sep, value = piece.partition("=")
        if _is_secret_name(name):
            pieces.append(f"{name}=***")
        else:
            pieces.append(piece)
    fragment = "***" if parts.fragment and _is_secret_name(parts.fragment.partition("=")[0]) else parts.fragment
    return scrub(urlunsplit((parts.scheme, netloc, path, "&".join(pieces), fragment)))


def _is_secret_header(name: str) -> bool:
    n = name.strip().lower()
    return n in SECRET_HEADER_NAMES or any(w in n for w in _SECRET_WORDS)


def redacted_headers(headers: Mapping[str, Any] | Iterable[tuple[Any, Any]] | None) -> dict[str, str]:
    """A plain dict of headers with secret-carrying ones masked. Header values
    that are URLs (for example NH's x-next) are passed through redacted_url."""
    if headers is None:
        return {}
    items = headers.items() if hasattr(headers, "items") else headers
    out: dict[str, str] = {}
    for k, v in items:
        key = k.decode("latin-1") if isinstance(k, bytes) else str(k)
        val = v.decode("latin-1") if isinstance(v, bytes) else str(v)
        if _is_secret_header(key):
            out[key] = "***"
        elif val.lower().startswith(("http://", "https://")):
            out[key] = redacted_url(val)
        else:
            out[key] = scrub(val)
    return out


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def write_json(path: Path | str, obj: Any) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(scrub(json.dumps(obj, indent=2, ensure_ascii=False, default=str)) + "\n", encoding="utf-8")
    return p


def display_path(p: Path | str) -> str:
    p = Path(p)
    try:
        return str(p.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


def _body_kind(content_type: str, url: str | None, content: bytes) -> str:
    ct = (content_type or "").lower()
    if "json" in ct:
        return "json"
    if any(x in ct for x in ("protobuf", "vector-tile", "octet-stream")):
        return "pbf"
    if url and urlsplit(url).path.lower().endswith(".pbf"):
        return "pbf"
    if ct.startswith("text/") or "html" in ct or "xml" in ct:
        return "txt"
    head = content.lstrip()[:1]
    if head in (b"{", b"["):
        return "json"
    try:
        content.decode("utf-8")
        return "txt"
    except UnicodeDecodeError:
        return "bin"


def save_response(
    outdir: Path | str,
    stem: str,
    response: httpx.Response,
    *,
    body: bool = True,
    max_body_bytes: int | None = None,
    elapsed_s: float | None = None,
    note: str | None = None,
) -> Path:
    """Write <stem>.meta.json and (unless body=False) <stem>.body.<json|txt|pbf>.

    The meta file holds the REDACTED request URL, method, status, redacted
    request and response headers, elapsed seconds and the fetch time. The URL
    is never written unredacted anywhere. Text bodies are scrubbed of known
    secrets; JSON is re-dumped indented when it parses. Pass body=False when
    the body holds personal data and the probe saves its own cleaned copy.
    Returns the meta file's path; the meta records the body file's name."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        request = response.request
    except RuntimeError:          # a Response built without a Request
        request = None
    url = redacted_url(str(request.url)) if request is not None else None
    method = request.method if request is not None else None
    req_headers = redacted_headers(request.headers) if request is not None else {}
    if elapsed_s is None:
        try:
            elapsed_s = response.elapsed.total_seconds()
        except RuntimeError:
            elapsed_s = None

    content = response.content or b""
    content_type = response.headers.get("content-type", "")
    body_file: str | None = None
    truncated = False
    if body and content:
        kind = _body_kind(content_type, url, content)
        if max_body_bytes is not None and len(content) > max_body_bytes:
            content = content[:max_body_bytes]
            truncated = True
            if kind == "json":
                kind = "txt"
        if kind == "json":
            try:
                text = json.dumps(json.loads(content), indent=2, ensure_ascii=False)
            except ValueError:
                kind = "txt"
            else:
                body_file = f"{stem}.body.json"
                (outdir / body_file).write_text(scrub(text) + "\n", encoding="utf-8")
        if kind == "txt":
            body_file = f"{stem}.body.txt"
            (outdir / body_file).write_text(scrub(content.decode("utf-8", errors="replace")), encoding="utf-8")
        elif kind in ("pbf", "bin"):
            body_file = f"{stem}.body.{kind}"
            (outdir / body_file).write_bytes(content)

    meta = {
        "url": url,
        "method": method,
        "status": response.status_code,
        "reason": response.reason_phrase,
        "headers": redacted_headers(response.headers),
        "request_headers": req_headers,
        "elapsed_s": round(elapsed_s, 3) if elapsed_s is not None else None,
        "fetched_at": iso(now_utc()),
        "body_bytes": len(response.content or b""),
        "body_file": body_file,
        "body_truncated": truncated,
        "note": note,
    }
    meta_path = outdir / f"{stem}.meta.json"
    write_json(meta_path, meta)
    return meta_path


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------

def _fmt(value: Any) -> str:
    """Plain text for findings.md: no Python reprs. A dict renders as
    'waze = 1, total = 1', a list as 'a, b', an empty one as '(none)'."""
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    if isinstance(value, dict):
        return ", ".join(f"{k} = {_fmt(v)}" for k, v in value.items()) if value else "(none)"
    if isinstance(value, (list, tuple, set, frozenset)):
        return ", ".join(_fmt(v) for v in value) if value else "(none)"
    if value is None:
        return "unknown"
    return str(value)


class Findings:
    """Collects the probe's findings as Markdown, printing each line as it is
    added, and writes findings.md plus summary.json at the end."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.lines: list[str] = [f"# {title}", ""]
        self.summary: dict[str, Any] = {}
        self._section: str | None = None
        print(f"# {title}", flush=True)

    def add(self, line: str = "") -> None:
        line = scrub(str(line))
        self.lines.append(line)
        print(line, flush=True)

    def section(self, title: str) -> None:
        self._section = title
        self.lines += ["", f"## {title}", ""]
        print(f"\n## {title}\n", flush=True)

    def kv(self, key: str, value: Any) -> None:
        self.add(f"- {key}: {_fmt(value)}")
        self.summary[key] = value

    def table(self, rows: Sequence[Sequence[Any]], header: Sequence[Any] | None = None) -> None:
        rows = [list(r) for r in rows]
        if header is None:
            if not rows:
                self.add("(no rows)")
                return
            header, rows = rows[0], rows[1:]
        cells = lambda r: [str(_fmt(c)).replace("|", "\\|").replace("\n", " ") for c in r]  # noqa: E731
        head = cells(header)
        body = [cells(r) for r in rows]
        # Pad every row (and the header) to the widest one, so a row that is
        # shorter or longer than the header can never abort a probe after its
        # requests were sent.
        n = max([len(head)] + [len(r) for r in body])
        if n == 0:
            self.add("(no rows)")
            return
        head = head + [""] * (n - len(head))
        body = [r + [""] * (n - len(r)) for r in body]
        widths = [max(len(head[i]), *(len(r[i]) for r in body)) if body else len(head[i]) for i in range(n)]
        line = lambda r: "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(r)) + " |"  # noqa: E731
        self.add(line(head))
        self.add("|" + "|".join("-" * (w + 2) for w in widths) + "|")
        if not body:
            self.add(line(["(none)"] + [""] * (n - 1)))
        for r in body:
            self.add(line(r))

    def write(self, outdir: Path | str) -> Path:
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        md = outdir / "findings.md"
        md.write_text(scrub("\n".join(self.lines)) + "\n", encoding="utf-8")
        write_json(outdir / "summary.json", {
            "title": self.title,
            "written_at": iso(now_utc()),
            "values": self.summary,
        })
        print(f"\nWrote {display_path(md)} and summary.json", flush=True)
        return md


# --------------------------------------------------------------------------
# Request budget
# --------------------------------------------------------------------------

class RequestBudget:
    """Counts requests per product and refuses to go past a per-run cap.

    Call ``check(n)`` with the planned total BEFORE the first request (it
    prints the plan) and ``count(product)`` BEFORE each request (it raises
    SystemExit instead of letting the request go out once the cap is hit)."""

    def __init__(self, max_requests: int) -> None:
        self.max_requests = int(max_requests)
        if self.max_requests < 1:
            raise ValueError("max_requests must be at least 1")
        self.counts: dict[str, int] = {}

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def check(self, n_planned: int, what: str = "") -> None:
        suffix = f" ({what})" if what else ""
        if n_planned > self.max_requests:
            raise SystemExit(
                f"Refusing to start: this run plans {n_planned} requests{suffix} but the "
                f"per-run cap is {self.max_requests}. Lower the plan, or pass "
                f"--max-requests {n_planned} if you mean it."
            )
        print(f"Planned requests: {n_planned}{suffix}; per-run cap {self.max_requests}", flush=True)

    def count(self, product: str, n: int = 1) -> int:
        if self.total + n > self.max_requests:
            raise SystemExit(
                f"Stopping: request {self.total + n} ({product}) would exceed the per-run "
                f"cap of {self.max_requests}. Nothing more was sent. Totals so far: {self.totals()}"
            )
        self.counts[product] = self.counts.get(product, 0) + n
        return self.counts[product]

    def totals(self) -> dict[str, int]:
        return {**self.counts, "total": self.total}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http_client(**extra_headers: str) -> httpx.Client:
    """The one client every probe uses: 10 s timeout, honest User-Agent, no
    retries (httpx does none by default) and no redirect following."""
    headers = {"User-Agent": USER_AGENT}
    headers.update(extra_headers)
    return httpx.Client(timeout=httpx.Timeout(TIMEOUT_S), headers=headers, follow_redirects=False)


def timed_request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> tuple[httpx.Response, float]:
    """One request, no retry. Returns (response, elapsed seconds). Exceptions
    propagate; describe them with explain_exception()."""
    t0 = time.perf_counter()
    response = client.request(method, url, **kwargs)
    return response, time.perf_counter() - t0


def explain_exception(exc: BaseException) -> str:
    """A one-line, non-developer explanation of a failed request. Never
    includes the URL (it might carry a key) unless redacted by the caller."""
    if isinstance(exc, httpx.ConnectTimeout):
        return f"timed out after {TIMEOUT_S:.0f} s while connecting (is the internet up? a firewall?)"
    if isinstance(exc, httpx.ReadTimeout):
        return f"connected, but no answer within {TIMEOUT_S:.0f} s"
    if isinstance(exc, httpx.TimeoutException):
        return f"timed out after {TIMEOUT_S:.0f} s"
    if isinstance(exc, httpx.ConnectError):
        return f"could not connect ({scrub(str(exc)) or 'connection refused or DNS failed'})"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "the server closed the connection unexpectedly"
    if isinstance(exc, httpx.HTTPError):
        return f"{type(exc).__name__}: {scrub(str(exc))}"
    return f"{type(exc).__name__}: {scrub(str(exc))}"


def status_line(response: httpx.Response, elapsed_s: float | None = None) -> str:
    """'HTTP 200 OK, 1234 bytes, application/json, 0.41 s' for the log."""
    ct = response.headers.get("content-type", "?").split(";")[0].strip() or "?"
    size = len(response.content or b"")
    took = f", {elapsed_s:.2f} s" if elapsed_s is not None else ""
    return f"HTTP {response.status_code} {response.reason_phrase}, {size} bytes, {ct}{took}"


# --------------------------------------------------------------------------
# Missing keys
# --------------------------------------------------------------------------

def missing_key(name: str, outdir: Path | str | None = None) -> NoReturn:
    """Tell the owner what to set and exit with code 3. Never echoes values.
    Removes the run's output folder when it is still empty."""
    print(f"Set {name} in .env (see .env.example)", file=sys.stderr, flush=True)
    remove_if_empty(outdir)
    sys.exit(EXIT_MISSING_KEY)


def remove_if_empty(outdir: Path | str | None) -> None:
    if outdir is None:
        return
    p = Path(outdir)
    try:
        if p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    except OSError:
        pass


# --------------------------------------------------------------------------
# Set-up
# --------------------------------------------------------------------------

def positive_int(text: str) -> int:
    """argparse type for --max-requests: a whole number of at least 1, so a
    bad value is a one-line usage error before any output folder exists."""
    try:
        n = int(str(text).strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError("--max-requests must be at least 1")
    return n


def _add_common_args(parser: argparse.ArgumentParser, name: str, max_requests_default: int | None) -> None:
    parser.add_argument("--config", default=str(REPO_ROOT / "config.yaml"), metavar="FILE",
                        help="path to config.yaml (default: the repo's config.yaml)")
    parser.add_argument("--out", default=None, metavar="DIR",
                        help=f"write output here instead of fixtures/{name}/live/<timestamp>/")
    cap = f" (default: {max_requests_default})" if max_requests_default is not None else ""
    parser.add_argument("--max-requests", type=positive_int, default=max_requests_default, metavar="N",
                        help=f"per-run cap on HTTP requests; the probe refuses to exceed it{cap}")


def setup(
    name: str,
    argv_description: str,
    *,
    argv: Sequence[str] | None = None,
    max_requests_default: int | None = None,
) -> tuple[dict[str, Any], Path, argparse.ArgumentParser]:
    """Load .env and config.yaml, create fixtures/<name>/live/<timestamp>/ and
    return (cfg, outdir, parser).

    The parser already has --config, --out and --max-requests; the probe adds
    its own flags and calls ``parser.parse_args()``. When ``-h``/``--help`` is
    on the command line no output folder is created (parse_args prints the
    help and exits), so call parse_args before doing anything else."""
    args = list(sys.argv[1:] if argv is None else argv)
    wants_help = any(a in ("-h", "--help") for a in args)

    load_dotenv(REPO_ROOT / ".env")

    pre = argparse.ArgumentParser(prog=f"probe_{name}", add_help=False)
    _add_common_args(pre, name, max_requests_default)
    known, _ = pre.parse_known_args(args)

    parser = argparse.ArgumentParser(
        prog=f"probe_{name}",
        description=argv_description,
    )
    _add_common_args(parser, name, max_requests_default)

    config_path = Path(known.config).expanduser()
    cfg: dict[str, Any] = {}
    if config_path.is_file():
        try:
            cfg = load_config(config_path)
        except Exception as exc:  # a broken YAML file is the owner's to fix
            print(f"Could not read {config_path}: {exc}", file=sys.stderr)
            sys.exit(EXIT_FAILED)
    elif not wants_help:
        print(f"Config file not found: {config_path} (run from the repo root, or pass --config)",
              file=sys.stderr)
        sys.exit(EXIT_FAILED)

    if known.out:
        outdir = Path(known.out).expanduser()
    else:
        outdir = REPO_ROOT / "fixtures" / name / "live" / run_dir_name()
    if not wants_help:
        outdir.mkdir(parents=True, exist_ok=True)
        print(f"Output folder: {display_path(outdir)}", flush=True)
    return cfg, outdir, parser
