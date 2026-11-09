#!/usr/bin/env python3
"""
gitls - browse a folder in your browser with git status colors.

Usage:
    python3 gitls.py [DIRECTORY] [-p PORT]

Then open http://localhost:8777/ . Reload the page to refresh git status.
Click a status badge to see that file's diff.
"""

import argparse
import functools
import os
import subprocess
import sys
import time
import webbrowser
from html import escape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse, parse_qs

# ---------------------------------------------------------------- git helpers


def git(root, *args, timeout=15, allow_fail=False):
    """Run a git command, return stdout or None on failure."""
    try:
        # git emits UTF-8 regardless of the OS code page; decoding with the
        # locale default (cp1252 on most Windows setups) fails on names like
        # "Ásvány.txt" and silently drops every status in the repo.
        proc = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 and not allow_fail:
        return None
    return proc.stdout


def repo_root(path):
    out = git(path, "rev-parse", "--show-toplevel")
    return out.strip() if out else None


def head_info(root):
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    sha = git(root, "rev-parse", "--short", "HEAD")
    subject = git(root, "log", "-1", "--pretty=%s")
    return (
        branch.strip() if branch else "?",
        sha.strip() if sha else "no commits",
        subject.strip() if subject else "",
    )


def status_map(root):
    """{path relative to repo root: 'XY'} for every changed/untracked file."""
    out = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if out is None:
        return {}
    fields = out.split("\0")
    result = {}
    i = 0
    while i < len(fields):
        entry = fields[i]
        if len(entry) < 4:
            i += 1
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in ("R", "C"):  # rename/copy: the next field is the old path
            i += 1
            old = fields[i] if i < len(fields) else ""
            if old:
                result[old] = code
        result[path] = code
        i += 1
    return result


# ------------------------------------------------------------- status styling

# kind -> (css class, human label)
KINDS = {
    "untracked": ("s-new", "untracked"),
    "worktree": ("s-work", "modified, not staged"),
    "staged": ("s-index", "staged"),
    "both": ("s-both", "staged + more edits since"),
    "deleted": ("s-gone", "deleted"),
    "conflict": ("s-conflict", "conflicted"),
}


def classify(code):
    """Map a porcelain XY code to one of the kinds above."""
    if code == "??":
        return "untracked"
    x, y = code[0], code[1]
    if "U" in code or code in ("AA", "DD"):
        return "conflict"
    if x == "D" or y == "D":
        return "deleted"
    if x != " " and y != " ":
        return "both"
    if x != " ":
        return "staged"
    return "worktree"


def badge(code):
    """Two-cell badge: left = index column, right = worktree column."""
    kind = classify(code)
    cls, label = KINDS[kind]
    x, y = (code[0], code[1]) if len(code) == 2 else (" ", " ")
    cell = lambda ch: f'<i>{escape(ch) if ch.strip() else "&middot;"}</i>'
    return f'<span class="badge {cls}" title="{escape(code)} - {label}">{cell(x)}{cell(y)}</span>'


def human_size(n):
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


# --------------------------------------------------------------------- markup

CSS = """
:root {
  --bg:#101215; --panel:#161a1f; --line:#232a33; --ink:#d8dee7; --dim:#7c8896;
  --new:#5fd68a; --work:#e0a54a; --index:#5fb3d9; --both:#b98ae0;
  --gone:#e06c6c; --conflict:#e0508a;
}
* { box-sizing:border-box; }
body {
  margin:0; padding:28px 32px 60px; background:var(--bg); color:var(--ink);
  font:14px/1.5 ui-monospace, "JetBrains Mono", "DejaVu Sans Mono", monospace;
}
a { color:inherit; text-decoration:none; }
a:hover { text-decoration:underline; }
:focus-visible { outline:2px solid var(--index); outline-offset:2px; }
header { border-bottom:1px solid var(--line); padding-bottom:14px; margin-bottom:18px; }
.crumb { font-size:17px; letter-spacing:-.2px; word-break:break-all; }
.crumb .sep { color:var(--dim); }
.crumb a:hover { color:#fff; }
.head { margin-top:8px; color:var(--dim); font-size:12.5px; }
.head b { color:var(--ink); font-weight:600; }
.head .sha { color:var(--index); }
table { border-collapse:collapse; width:100%; max-width:1100px; }
th {
  text-align:left; font-weight:600; font-size:11px; letter-spacing:.14em;
  text-transform:uppercase; color:var(--dim); padding:0 14px 8px 0;
  border-bottom:1px solid var(--line);
}
td { padding:3px 14px 3px 0; white-space:nowrap; vertical-align:middle; }
tbody tr:hover { background:var(--panel); }
td.name { width:100%; white-space:normal; word-break:break-all; }
td.size, td.date { color:var(--dim); font-size:12.5px; text-align:right; }
td.date { text-align:left; }
.dir > a::after { content:"/"; color:var(--dim); }
.badge {
  display:inline-flex; gap:1px; font-size:11px; line-height:1;
}
.badge i {
  font-style:normal; width:15px; height:17px; display:grid; place-items:center;
  background:var(--panel); border:1px solid var(--line); border-radius:2px;
}
.badge i:first-child { color:var(--index); }
.s-new i, .s-new i:first-child { color:var(--new); border-color:#2a4a36; }
.s-work i:last-child { color:var(--work); border-color:#4a3c22; }
.s-both i:first-child { color:var(--index); }
.s-both i:last-child { color:var(--both); border-color:#3d2a4d; }
.s-gone i { color:var(--gone); border-color:#4a2626; }
.s-conflict i { color:var(--conflict); border-color:#4d2436; }
.n-new { color:var(--new); }
.n-work { color:var(--work); }
.n-index { color:var(--index); }
.n-both { color:var(--both); }
.n-gone { color:var(--gone); text-decoration:line-through; text-decoration-color:#6b3232; }
.n-conflict { color:var(--conflict); }
.count {
  color:var(--work); font-size:11px; background:var(--panel);
  border:1px solid var(--line); border-radius:9px; padding:1px 7px;
}
.clean { color:var(--dim); }
.legend { margin-top:26px; color:var(--dim); font-size:12px; display:flex;
          flex-wrap:wrap; gap:16px; max-width:1100px; }
.legend span::before { content:"\\25a0  "; }
.l-new::before { color:var(--new); } .l-work::before { color:var(--work); }
.l-index::before { color:var(--index); } .l-both::before { color:var(--both); }
.l-gone::before { color:var(--gone); }
pre.diff { background:var(--panel); border:1px solid var(--line); border-radius:4px;
           padding:14px 16px; overflow-x:auto; font-size:13px; max-width:1100px; }
pre.diff .add { color:var(--new); } pre.diff .del { color:var(--gone); }
pre.diff .hunk { color:var(--index); } pre.diff .meta { color:var(--dim); }
.back { display:inline-block; margin-bottom:16px; color:var(--dim); }
@media (max-width:640px) { body { padding:18px 16px 50px; } td.date { display:none; } }
"""


def page(title, body):
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title><style>{CSS}</style>{body}"
    )


def breadcrumb(rel_url):
    """rel_url: '/a/b/' style path already URL-decoded."""
    parts = [p for p in rel_url.strip("/").split("/") if p]
    out = ["<a href='/'>&#8962; root</a>"]
    acc = ""
    for p in parts:
        acc += "/" + quote(p)
        out.append(f"<a href='{acc}/'>{escape(p)}</a>")
    return "<span class='sep'> / </span>".join(out)


# -------------------------------------------------------------------- handler


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


class GitIndexHandler(SimpleHTTPRequestHandler):
    server_version = "gitls/1.0"

    def host_allowed(self):
        """Only answer requests addressed to a loopback name.

        Binding to 127.0.0.1 is not enough on its own: with DNS rebinding a web
        page can point its own hostname at 127.0.0.1 and read this server's
        responses. Such requests still carry the attacker's Host header.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        host = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
        return host in LOOPBACK_HOSTS

    def do_GET(self):
        if not self.host_allowed():
            return self.send_error(403, "gitls only answers requests for localhost")
        query = parse_qs(urlparse(self.path).query)
        if "diff" in query:
            return self.send_diff(query["diff"][0])  # parse_qs already decoded it
        return super().do_GET()

    def do_HEAD(self):
        if not self.host_allowed():
            return self.send_error(403, "gitls only answers requests for localhost")
        return super().do_HEAD()

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    # -- directory listing ------------------------------------------------
    def list_directory(self, path):
        try:
            names = os.listdir(path)
        except OSError:
            self.send_error(404, "Cannot list directory")
            return None

        root = self.server.repo
        statuses = status_map(root) if root else {}
        rel_dir = os.path.relpath(path, root) if root else ""
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")

        # files git still tracks but that are gone from disk won't be in listdir
        prefix = rel_dir + "/" if rel_dir else ""
        ghosts = sorted(
            p[len(prefix):]
            for p, c in statuses.items()
            if p.startswith(prefix)
            and "/" not in p[len(prefix):]
            and "D" in c
            and not os.path.exists(os.path.join(path, p[len(prefix):]))
        )
        names = [n for n in names if n not in ghosts] + ghosts

        names.sort(key=lambda n: (not os.path.isdir(os.path.join(path, n)), n.lower()))
        rows = []
        for name in names:
            if name == ".git":
                continue
            full = os.path.join(path, name)
            is_dir = os.path.isdir(full)
            key = f"{rel_dir}/{name}" if rel_dir else name

            if is_dir:
                inside = [c for p, c in statuses.items() if p.startswith(key + "/")]
                if inside:
                    mark = f"<span class='count'>{len(inside)}</span>"
                    ncls = "n-work"
                else:
                    mark = "<span class='clean'>&middot;</span>"
                    ncls = ""
            else:
                code = statuses.get(key)
                if code:
                    mark = badge(code)
                    ncls = "n-" + KINDS[classify(code)][0].split("-")[1]
                    if code[0] in ("R", "C"):
                        ncls = "n-index"
                else:
                    mark = "<span class='clean'>&middot;</span>"
                    ncls = ""

            try:
                st = os.stat(full)
                size = "" if is_dir else human_size(st.st_size)
                mtime = time.strftime("%b %d %H:%M", time.localtime(st.st_mtime))
            except OSError:
                size, mtime = "", ""

            href = quote(name) + ("/" if is_dir else "")
            link = f"<a href='{href}'>{escape(name)}</a>"
            if not is_dir and statuses.get(key):
                mark = f"<a href='?diff={quote(key)}'>{mark}</a>"

            rows.append(
                f"<tr><td>{mark}</td>"
                f"<td class='name {'dir' if is_dir else ''} {ncls}'>{link}</td>"
                f"<td class='size'>{size}</td><td class='date'>{mtime}</td></tr>"
            )

        if root:
            branch, sha, subject = head_info(root)
            changed = len(statuses)
            head = (
                f"<div class='head'>on <b>{escape(branch)}</b> &middot; "
                f"<span class='sha'>{escape(sha)}</span> {escape(subject[:70])} &middot; "
                f"<b>{changed}</b> changed file{'' if changed == 1 else 's'} "
                f"&middot; reload to refresh</div>"
            )
        else:
            head = "<div class='head'>not a git repository - showing plain listing</div>"

        rel_url = unquote(urlparse(self.path).path)
        body = (
            "<header>"
            f"<div class='crumb'>{breadcrumb(rel_url)}</div>{head}</header>"
            "<table><thead><tr><th>git</th><th>name</th><th style='text-align:right'>size</th>"
            "<th>modified</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
            "<div class='legend'>"
            "<span class='l-new'>untracked</span>"
            "<span class='l-work'>modified, not staged</span>"
            "<span class='l-index'>staged</span>"
            "<span class='l-both'>staged + edited since</span>"
            "<span class='l-gone'>deleted</span>"
            "<span>left cell = index, right cell = working tree</span>"
            "</div>"
        )
        return self.render(page(rel_url or "/", body))

    # -- diff view --------------------------------------------------------
    def send_diff(self, rel_path):
        root = self.server.repo
        if not root:
            self.send_error(404, "Not a git repository")
            return
        code = status_map(root).get(rel_path, "")
        if code == "??":
            text = git(
                root, "diff", "--no-index", "--", "/dev/null", rel_path, allow_fail=True
            ) or ""
            if not text:
                try:
                    with open(os.path.join(root, rel_path), encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError as exc:
                    text = str(exc)
        else:
            text = (git(root, "diff", "--", rel_path) or "") + (
                git(root, "diff", "--cached", "--", rel_path) or ""
            )
        text = text or "No textual diff (binary file, or nothing to show)."

        lines = []
        for line in text.splitlines():
            cls = ""
            if line[:3] in ("---", "+++"):
                cls = "meta"
            elif line.startswith("+"):
                cls = "add"
            elif line.startswith("-"):
                cls = "del"
            elif line.startswith("@@"):
                cls = "hunk"
            elif line[:3] in ("dif", "ind", "new", "del", "old"):
                cls = "meta"
            lines.append(f"<span class='{cls}'>{escape(line)}</span>" if cls else escape(line))

        parent = "/" + quote(os.path.dirname(rel_path)) + ("/" if os.path.dirname(rel_path) else "")
        body = (
            f"<a class='back' href='{parent}'>&larr; back</a>"
            f"<header><div class='crumb'>{escape(rel_path)}</div>"
            f"<div class='head'>{escape(code or '  ')} &middot; git diff</div></header>"
            "<pre class='diff'>" + "\n".join(lines) + "</pre>"
        )
        self.render(page(rel_path, body))

    def render(self, html_text):
        data = html_text.encode("utf-8", "replace")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
        return None


def make_server(directory, port=8777):
    """Build (but don't start) a server for `directory`; port 0 picks a free one."""
    # realpath, not abspath: git reports the resolved top-level path, so a
    # symlinked or 8.3-short served path would never line up with it.
    directory = os.path.realpath(directory)
    handler = functools.partial(GitIndexHandler, directory=directory)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.repo = repo_root(directory)
    return httpd


def main():
    ap = argparse.ArgumentParser(description="Browse a folder with git status colors.")
    ap.add_argument("directory", nargs="?", default=".", help="folder to serve (default: .)")
    ap.add_argument("-p", "--port", type=int, default=8777)
    ap.add_argument("--no-open", action="store_true", help="don't launch the browser")
    args = ap.parse_args()

    directory = os.path.realpath(args.directory)
    if not os.path.isdir(directory):
        sys.exit(f"Not a directory: {directory}")

    httpd = make_server(directory, args.port)

    url = f"http://localhost:{args.port}/"
    print(f"gitls  {directory}")
    print(f"repo   {httpd.repo or '(none found)'}")
    print(f"open   {url}   (ctrl-c to stop)")
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
