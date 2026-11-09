"""Tests for gitls. Run with:  python -m unittest -v"""

import http.client
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import quote

import gitls


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def rmtree(path):
    """shutil.rmtree that also removes git's read-only object files on Windows."""

    def retry(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


class PureFunctionTests(unittest.TestCase):
    def test_classify_porcelain_codes(self):
        expected = {
            "??": "untracked",
            " M": "worktree",
            "M ": "staged",
            "A ": "staged",
            "R ": "staged",
            "MM": "both",
            "AM": "both",
            " D": "deleted",
            "D ": "deleted",
            "UU": "conflict",
            "AA": "conflict",
            "DD": "conflict",
        }
        for code, kind in expected.items():
            with self.subTest(code=code):
                self.assertEqual(gitls.classify(code), kind)

    def test_human_size(self):
        self.assertEqual(gitls.human_size(512), "512 B")
        self.assertEqual(gitls.human_size(2048), "2.0 kB")
        self.assertEqual(gitls.human_size(5 * 1024**3), "5.0 GB")


class RepoTests(unittest.TestCase):
    """One scratch repo holding a file in every state gitls distinguishes."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="gitls-test-")
        root = cls.root = os.path.join(cls.tmp, "repo")
        os.makedirs(root)
        git(root, "init", "-q")
        git(root, "config", "user.email", "test@example.com")
        git(root, "config", "user.name", "gitls test")
        git(root, "config", "core.autocrlf", "false")
        for name in ("modified.txt", "staged.txt", "both.txt", "deleted.txt", "old-name.txt"):
            write(root, name, f"{name}\n")
        git(root, "add", ".")
        git(root, "commit", "-q", "-m", "initial")

        write(root, "modified.txt", "modified.txt\ntwo\n")
        write(root, "staged.txt", "staged.txt\ntwo\n")
        git(root, "add", "staged.txt")
        write(root, "both.txt", "both.txt\ntwo\n")
        git(root, "add", "both.txt")
        write(root, "both.txt", "both.txt\ntwo\nthree\n")
        os.remove(os.path.join(root, "deleted.txt"))
        git(root, "mv", "old-name.txt", "new-name.txt")
        write(root, "Ásvány.txt", "non-ASCII name\n")  # UTF-8 bytes cp1252 cannot decode
        write(root, "rate%20.txt", "percent in name\n")
        write(root, "notes/todo.md", "untracked inside a new folder\n")

    @classmethod
    def tearDownClass(cls):
        rmtree(cls.tmp)

    def setUp(self):
        self.httpd = gitls.make_server(self.root, port=0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, path, host=None, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, headers={"Host": host or f"localhost:{self.port}"})
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8")
        finally:
            conn.close()

    def row(self, body, name):
        for tr in body.split("<tr>")[1:]:
            if f">{name}</a>" in tr:
                return tr
        self.fail(f"no row for {name!r}")

    def test_status_map_covers_every_state(self):
        statuses = gitls.status_map(gitls.repo_root(self.root))
        self.assertEqual(statuses.get("modified.txt"), " M")
        self.assertEqual(statuses.get("staged.txt"), "M ")
        self.assertEqual(statuses.get("both.txt"), "MM")
        self.assertEqual(statuses.get("deleted.txt"), " D")
        self.assertEqual(statuses.get("new-name.txt", "")[:1], "R")
        self.assertEqual(statuses.get("old-name.txt", "")[:1], "R")
        self.assertEqual(statuses.get("Ásvány.txt"), "??")
        self.assertEqual(statuses.get("notes/todo.md"), "??")

    def test_listing_marks_each_file(self):
        status, body = self.request("/")
        self.assertEqual(status, 200)
        expected = {
            "modified.txt": "s-work",
            "staged.txt": "s-index",
            "both.txt": "s-both",
            "deleted.txt": "s-gone",  # gone from disk, still listed
            "new-name.txt": "s-index",
            "Ásvány.txt": "s-new",
        }
        for name, css in expected.items():
            with self.subTest(name=name):
                self.assertIn(css, self.row(body, name))
        self.assertIn("<span class='count'>1</span>", self.row(body, "notes"))

    def test_diff_of_tracked_file(self):
        status, body = self.request("/?diff=modified.txt")
        self.assertEqual(status, 200)
        self.assertIn("<span class='add'>+two</span>", body)

    def test_diff_of_untracked_files_with_awkward_names(self):
        for name, content in (("Ásvány.txt", "non-ASCII name"), ("rate%20.txt", "percent in name")):
            with self.subTest(name=name):
                status, body = self.request("/?diff=" + quote(name))
                self.assertEqual(status, 200)
                self.assertIn(content, body)

    def test_rejects_requests_for_other_hosts(self):
        # A DNS-rebinding page reaches 127.0.0.1 but keeps its own Host header.
        self.assertEqual(self.request("/", host="attacker.example")[0], 403)
        self.assertEqual(self.request("/", host=f"attacker.example:{self.port}")[0], 403)
        self.assertEqual(self.request("/", host="attacker.example", method="HEAD")[0], 403)

    def test_accepts_loopback_hosts(self):
        for host in ("localhost", f"localhost:{self.port}", f"127.0.0.1:{self.port}"):
            with self.subTest(host=host):
                self.assertEqual(self.request("/", host=host)[0], 200)


if __name__ == "__main__":
    unittest.main()
