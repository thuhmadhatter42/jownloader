#!/usr/bin/env python3
"""Jownloader developer log: one file for the native host, the engine and the browser extension.

  ~/Library/Logs/Jownloader/jownloader.log      (5 files of 5 MB: jownloader.log, .1 ... .5)

Environment (all optional):
  JOWN_LOG=info              quieter (default debug). Also: warning, error.
  JOWN_LOG_DIR=<dir>         write there instead of ~/Library/Logs/Jownloader (tests use a temp dir)
  JOWN_LOG_MAX_BYTES=<n>     size of one file before it rotates (default 5242880)

Every sendNativeMessage is its own host process and the persistent port is another, so several processes
append to the one file at once. Each line is written under an flock on jownloader.log.lock, the file is
reopened for every line, and the size check + rotation happen under that same lock: no process ever
rotates a file another one still has open. Stdlib only (it must import under the bootstrap python3).

Rules the whole log follows:
  - never file data (chunks are counted, not logged), never cookie values: every line passes redact()
  - every subprocess is logged by run(): full argv, cwd, return code, duration, the tail of its output
  - a logging failure never breaks the host: this module swallows its own errors
"""
import contextlib
import fcntl
import json
import logging
import logging.handlers
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time

ROOT = "jownloader"
DEFAULT_MAX = 5 * 1024 * 1024
BACKUPS = 5
FILE = "jownloader.log"
logging.raiseExceptions = False        # a failed log write must not print into the host's stderr/stdout

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING, "warning": logging.WARNING,
           "error": logging.ERROR, "critical": logging.CRITICAL}


def log_dir():
    return os.environ.get("JOWN_LOG_DIR") or os.path.join(os.path.expanduser("~"), "Library", "Logs", "Jownloader")


def log_path():
    return os.path.join(log_dir(), FILE)


# ---------------------------------------------------------------------------------------------- redaction
# Cookie / token / password values never reach the file. Cookie FILE paths are fine (they are paths).
_COOKIE_NAMES = (r"__Secure-\w+|__Host-\w+|SID|HSID|SSID|APISID|SAPISID|LOGIN_INFO|VISITOR_INFO1_LIVE|YSC|ct0|"
                 r"auth_token|sessionid|csrftoken|csrf_token|ds_user_id|rur")
_SENS = (r"(?:" + _COOKIE_NAMES + r"|[\w-]*(?:token|passwd|password|secret|api[_-]?key|bearer|signature|sessionid|session[_-]?id)[\w-]*)")
_HEADER = re.compile(r"(?i)\b(cookie|set-cookie|authorization|proxy-authorization|x-csrf-token)(\s*[:=]\s*)[^\r\n]*")
_KV = re.compile(r"(?i)(?<![\w-])(" + _SENS + r")(\s*[=:]\s*)(?!\*\*\*)([^\s&\"'`;,<>]+)")
_NETSCAPE = re.compile(r"(?m)^(#HttpOnly_)?\.?[\w.-]+\t(?:TRUE|FALSE)\t\S+\t(?:TRUE|FALSE)\t\d+\t[^\t\n]+\t[^\n]*$")
_SECRET_FLAGS = {"--password", "-p", "--video-password", "--twofactor", "-2", "--ap-password", "--netrc-cmd", "--proxy-password"}
_HEADER_FLAGS = {"--add-header", "--header", "-H", "--headers"}


_JSON_SECRET = re.compile(r'(?i)("[\w-]*(?:cookie|authorization|token|passwd|password|secret|api[_-]?key|sessionid|session_id|signature)[\w-]*"\s*:\s*)"[^"]*"')


def redact(text):
    """text with cookie / token / password / auth-header values replaced by ***."""
    if not text:
        return text
    text = _JSON_SECRET.sub(r'\1"***"', str(text))        # JSON: "authorization": "Bearer x"
    text = _NETSCAPE.sub("[cookie-file line removed]", text)
    text = _HEADER.sub(lambda m: m.group(1) + m.group(2) + "***", text)
    return _KV.sub(lambda m: m.group(1) + m.group(2) + "***", text)


def _trunc(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n] + f"...[+{len(s) - n} chars]"


def redact_argv(argv):
    """argv as a list of redacted strings: the value after a password / header flag, and secret-looking pieces."""
    out, hide, header = [], False, False
    for a in argv:
        a = str(a)
        if hide:
            out.append("***"); hide = False; continue
        if header:
            out.append(re.sub(r"^([^:=]+[:=]).*$", r"\1***", a) if re.match(r"(?i)\s*[\w-]*(cookie|authorization|token)", a) else redact(a))
            header = False; continue
        if a in _SECRET_FLAGS:
            out.append(a); hide = True; continue
        if a in _HEADER_FLAGS:
            out.append(a); header = True; continue
        out.append(redact(a))
    return out


def fmt_argv(argv):
    """The exact command line, shell-quoted, redacted, each piece capped."""
    return " ".join(shlex.quote(_trunc(a, 400)) for a in redact_argv(argv))


# --------------------------------------------------------------------------------------------- the handler
class _Shared(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler safe for several processes: flock around [size check, rotate, append]."""

    def __init__(self, path, max_bytes, backups):
        super().__init__(path, mode="a", maxBytes=max_bytes, backupCount=backups, encoding="utf-8", delay=True)
        self._lockpath = path + ".lock"

    def emit(self, record):
        fd = None
        try:
            fd = os.open(self._lockpath, os.O_RDWR | os.O_CREAT, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            fd = None
        try:
            super().emit(record)
            if self.stream is not None:        # reopened for the next line: a rotation by another process is then seen
                try:
                    self.stream.close()
                finally:
                    self.stream = None
        except Exception:
            pass
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


_LV = {"WARNING": "WARN", "CRITICAL": "CRIT"}


class _Format(logging.Formatter):
    def format(self, record):
        record.area = record.name[len(ROOT) + 1:] if record.name.startswith(ROOT + ".") else record.name
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.message = _trunc(redact(msg), 6000)
        record.asctime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)) + ".%03d" % int(record.msecs)
        s = f"{record.asctime} {_LV.get(record.levelname, record.levelname):<5} [{record.process}] {record.area}: {record.message}"
        if record.exc_info:
            s += "\n" + redact(self.formatException(record.exc_info))
        return s


_lock = threading.Lock()
_ready = False


def _setup():
    global _ready
    with _lock:
        if _ready:
            return
        _ready = True
        root = logging.getLogger(ROOT)
        root.propagate = False
        root.setLevel(_LEVELS.get((os.environ.get("JOWN_LOG") or "debug").strip().lower(), logging.DEBUG))
        try:
            os.makedirs(log_dir(), exist_ok=True)
            try:
                mx = int(os.environ.get("JOWN_LOG_MAX_BYTES") or DEFAULT_MAX)
            except ValueError:
                mx = DEFAULT_MAX
            h = _Shared(log_path(), mx, BACKUPS)
        except Exception:
            h = logging.NullHandler()
        h.setFormatter(_Format())
        root.addHandler(h)


def get(area):
    """The logger for one area ('host', 'engine', 'spotify', ...), all writing the same file."""
    _setup()
    return logging.getLogger(f"{ROOT}.{area}")


def install_hooks(log):
    """Uncaught exceptions (main thread and worker threads) land in the log with their traceback."""
    def hook(et, ev, tb):
        log.critical("uncaught exception", exc_info=(et, ev, tb))
        sys.__excepthook__(et, ev, tb)

    def thook(args):
        log.error("uncaught exception in thread %s", getattr(args.thread, "name", "?"),
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    sys.excepthook = hook
    threading.excepthook = thook


# ------------------------------------------------------------------------------------------------ subprocesses
def _lines(text, n):
    ls = [l.rstrip() for l in str(text or "").splitlines() if l.strip()]
    return ls[-n:], max(0, len(ls) - n)


def tail_block(log, level, label, text, n, width=1000):
    ls, omitted = _lines(text, n)
    if omitted:
        log.log(level, "  %s: (%d earlier lines omitted)", label, omitted)
    for l in ls:
        log.log(level, "  %s| %s", label, _trunc(l, width))


def exec_start(log, cmd, cwd=None, timeout=None):
    log.debug("exec  %s  [cwd=%s timeout=%s]", fmt_argv(cmd), cwd or os.getcwd(), timeout)
    return time.monotonic()


def exec_done(log, cmd, rc, t0, out="", err="", note="", ok_tail=5):
    """One result line + output tail: last 30 lines of stderr and stdout on failure, last 5 on success."""
    dur = time.monotonic() - t0
    name = os.path.basename(str(cmd[0])) if cmd else "?"
    if rc == 0:
        log.info("exec ok   rc=0 %.2fs %s%s", dur, name, f" ({note})" if note else "")
        n, lvl = ok_tail, logging.DEBUG
    else:
        log.warning("exec FAIL rc=%s %.2fs %s%s", rc, dur, name, f" ({note})" if note else "")
        n, lvl = 30, logging.WARNING
    if n:
        tail_block(log, lvl, "stderr", err, n, 1000 if rc else 300)
        tail_block(log, lvl, "stdout", out, n, 1000 if rc else 300)


# ------------------------------------------------------------------------------------------- jobs / cancel
# One mechanism for Stop: a fetch runs as a "job" (its request id). Every subprocess it starts (yt-dlp, gallery-dl,
# ffmpeg, the analyzer ...) is launched by popen() below in its OWN session, so a cancel can signal the whole process
# group, children included, and is registered on the job. A cancelled job starts nothing new: popen() refuses, and the
# engine's retry loops ask cancelled(). Partial files are removed by the job's cleanup callbacks (see engine.py).
class Stopped(Exception):
    """popen() refused to start a process: the current job was cancelled."""


class Job:
    def __init__(self, jid):
        self.id = jid
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.procs = set()
        self.cleanups = []      # callables () -> [removed paths]; run once the job's processes are gone
        self.stems = []         # output-name stems this job writes (so another job's sweep leaves them alone)
        self.keep = set()       # finished files, never swept
        self.t0 = time.time()


JOBS = {}
JOBS_LOCK = threading.Lock()
_tls = threading.local()
KILL_GRACE = 3.0                # seconds between SIGTERM and SIGKILL of a cancelled job's process group


def register_job(jid):
    """The job for this request id (created on first call). The host registers it in its reader thread, before the
    worker thread starts, so a cancel that follows at once always finds it."""
    with JOBS_LOCK:
        j = JOBS.get(jid)
        if j is None:
            j = JOBS[jid] = Job(jid)
        return j


@contextlib.contextmanager
def job(jid):
    """Bind the current thread to the job `jid` (None = an unregistered job nobody can cancel)."""
    j = register_job(jid) if jid is not None else Job(None)
    _tls.job = j
    try:
        yield j
    finally:
        _tls.job = None
        with JOBS_LOCK:
            if jid is not None and JOBS.get(jid) is j:
                del JOBS[jid]


def current_job():
    return getattr(_tls, "job", None)


def cancelled():
    j = current_job()
    return bool(j and j.cancelled.is_set())


def on_cleanup(fn):
    j = current_job()
    if j:
        j.cleanups.append(fn)


def keep_file(path):
    """A finished file of the current job: a cancel never sweeps it."""
    j = current_job()
    if j:
        j.keep.add(os.path.abspath(str(path)))


def other_stems(j):
    with JOBS_LOCK:
        return [st for o in JOBS.values() if o is not j for st in o.stems]


def run_cleanups(j):
    """Remove the job's partial files (after its processes are gone). Returns the paths removed."""
    removed = []
    for fn in list(j.cleanups):
        try:
            removed += fn() or []
        except Exception:
            logging.getLogger("jownloader.jobs").exception("cleanup for job %s raised", j.id)
    return removed


def _signal_group(pid, sig):
    try:
        os.killpg(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _escalate(pid):
    time.sleep(KILL_GRACE)
    _signal_group(pid, signal.SIGKILL)      # stragglers of the group that ignored SIGTERM (no-op if it is empty)


def kill_proc(p, grace=KILL_GRACE):
    """SIGTERM p's whole process group; SIGKILL whatever is left after `grace` s. True when a signal was sent."""
    if p.poll() is not None and not _signal_group(p.pid, 0):
        return False
    sent = _signal_group(p.pid, signal.SIGTERM)
    threading.Thread(target=_escalate, args=(p.pid,), daemon=True).start()
    return sent


def cancel_job(jid, log=None):
    """Mark job `jid` cancelled and kill its processes. Returns (found, [pids signalled])."""
    with JOBS_LOCK:
        j = JOBS.get(jid)
    if j is None:
        return False, []
    j.cancelled.set()
    with j.lock:
        procs = list(j.procs)
    pids = []
    for p in procs:
        if kill_proc(p):
            pids.append(p.pid)
            if log:
                log.info("cancel job=%s: killed pid %d (process group, SIGKILL in %gs if still alive)", jid, p.pid, KILL_GRACE)
    return True, pids


def running_jobs():
    with JOBS_LOCK:
        return list(JOBS)


def popen(cmd, **kw):
    """subprocess.Popen in its own session, registered on the current job. Raises Stopped when the job was cancelled."""
    j = current_job()
    if j and j.cancelled.is_set():
        raise Stopped()
    p = subprocess.Popen(cmd, start_new_session=True, **kw)
    if j:
        with j.lock:
            j.procs.add(p)
            late = j.cancelled.is_set()
        if late:                    # the cancel landed between the check and the registration
            kill_proc(p)
    return p


def release(p):
    j = current_job()
    if j:
        with j.lock:
            j.procs.discard(p)


def run(cmd, log, timeout=None, cwd=None, input=None, env=None, ok_tail=5):
    """subprocess.run(capture_output, text) with the full log. Returns (returncode, stdout, stderr); never raises:
    a command that cannot start or times out is rc 1 with the reason in stderr. ok_tail = how many output lines of a
    successful run are logged (a failure always logs 30). A cancelled job's command is not started (rc 1, "stopped")
    and one already running is killed with its children (see cancel_job)."""
    t0 = exec_start(log, cmd, cwd, timeout)
    try:
        p = popen(cmd, stdin=subprocess.PIPE if input is not None else None, stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE, text=True, cwd=cwd, env=env)
    except Stopped:
        exec_done(log, cmd, 1, t0, "", "stopped", "not started: the job was stopped")
        return 1, "", "stopped"
    except (OSError, subprocess.SubprocessError) as e:
        exec_done(log, cmd, 1, t0, "", str(e), "could not start")
        return 1, "", str(e)
    try:
        try:
            out, err = p.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            _signal_group(p.pid, signal.SIGKILL)
            out, err = p.communicate()
            out, err = out or "", err or ""
            exec_done(log, cmd, 1, t0, out, err, f"TIMED OUT after {timeout}s")
            return 1, out, (err + "\n" if err else "") + f"timed out after {timeout}s"
        except (OSError, subprocess.SubprocessError) as e:
            _signal_group(p.pid, signal.SIGKILL)
            exec_done(log, cmd, 1, t0, "", str(e), "could not run")
            return 1, "", str(e)
    finally:
        release(p)
    if cancelled():
        _signal_group(p.pid, signal.SIGKILL)        # a cancelled job leaves no group member behind
    exec_done(log, cmd, p.returncode, t0, out, err, "stopped" if cancelled() else "", ok_tail=ok_tail)
    return p.returncode, out, err


def _short(v, n=200):
    return _trunc(repr(v) if not isinstance(v, str) else v, n)


def summarize(msg):
    """One line describing a native message or its reply: scalars as they are, lists as counts, no file data."""
    parts = []
    for k, v in (msg or {}).items():
        if k in ("data", "entries"):
            parts.append(f"{k}=<{len(v)}{' b64 chars' if k == 'data' else ' entries'}>")
        elif isinstance(v, str):
            parts.append(f"{k}={_trunc(v, 300)!r}")
        elif isinstance(v, (bool, int, float)) or v is None:
            parts.append(f"{k}={v}")
        elif isinstance(v, list):
            small = all(isinstance(x, (str, int)) for x in v) and len(v) <= 3
            parts.append(f"{k}={_trunc(repr(v), 200) if small else '[%d]' % len(v)}")
        elif isinstance(v, dict):
            if k == "meta":
                parts.append("meta=" + _trunc(repr({a: v.get(a) for a in ("title", "artists", "number", "total", "folder") if a in v}), 250))
            else:
                parts.append(f"{k}={{{len(v)} keys}}")
    return " ".join(parts)


# ----------------------------------------------------------------------------------------- extension side
def write_ext(entries):
    """Entries sent by the extension ({t: ms epoch, l: level, a: area, m: message, d: data}) go into the same file
    with their own timestamps, logger 'ext.<area>'. Returns how many were written."""
    _setup()
    n = 0
    for e in entries or []:
        try:
            area = re.sub(r"[^\w.-]+", "_", str(e.get("a") or "ext"))[:24]
            lg = logging.getLogger(f"{ROOT}.ext.{area}")
            lvl = _LEVELS.get(str(e.get("l") or "info").lower(), logging.INFO)
            if not lg.isEnabledFor(lvl):
                continue
            text = str(e.get("m") or "")
            d = e.get("d")
            if d not in (None, "", {}, []):
                text += "  " + (d if isinstance(d, str) else json.dumps(d, ensure_ascii=False, default=str))
            rec = logging.LogRecord(lg.name, lvl, "", 0, text, (), None)
            ms = int(float(e.get("t") or time.time() * 1000))        # epoch milliseconds, kept exact
            rec.created, rec.msecs = ms / 1000, ms % 1000
            lg.handle(rec)
            n += 1
        except Exception:
            continue
    return n


def tail_file(n=200):
    """The last n lines of the log (spilling into jownloader.log.1 when the live file is short), as one string."""
    n = max(1, min(int(n or 200), 5000))
    out = []
    for p in (log_path(), log_path() + ".1"):
        try:
            with open(p, "rb") as f:
                f.seek(0, os.SEEK_END)
                size, buf, pos = f.tell(), b"", f.tell()
                while pos > 0 and buf.count(b"\n") <= n:
                    step = min(65536, pos)
                    pos -= step
                    f.seek(pos)
                    buf = f.read(step) + buf
                lines = buf.decode("utf-8", "replace").splitlines()
                if pos > 0 and lines:
                    lines = lines[1:]          # first line is cut mid-way
                out = lines + out
        except OSError:
            continue
        if len(out) >= n:
            break
    return "\n".join(out[-n:])
