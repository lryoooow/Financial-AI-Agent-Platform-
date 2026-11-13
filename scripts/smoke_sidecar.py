"""Smoke-test an onedir sidecar in an isolated profile."""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def smoke(executable: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="jingyuan-sidecar-") as root:
        profile = Path(root)
        env = {
            **os.environ, "FINRESEARCH_DATA_DIR": root, "FINRESEARCH_KNOWLEDGE_DB": str(profile / "knowledge.db"),
            "DATABASE_URL": "", "FIN_ACCESS_TOKEN": "", "FIN_WEB": "0", "FRONTEND_DIST": "",
            "LLM_API_KEY": "", "TUSHARE_TOKEN": "", "SERPER_API_KEY": "", "ENABLE_SERPER_SEARCH": "false",
        }
        process = subprocess.Popen([str(executable.resolve())], cwd=root, env=env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        origin = ""

        def request(path, body=None, headers=None):
            req = urllib.request.Request(origin + path, data=body, headers=headers or {})
            with urllib.request.urlopen(req, timeout=25) as response:
                return response.read()

        try:
            for _ in range(90):
                if process.poll() is not None:
                    break
                try:
                    origin = "http://127.0.0.1:" + (profile / "port.txt").read_text().strip()
                    health = json.loads(request("/api/health"))
                    if health.get("db") == "ok":
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.4)
            else:
                raise RuntimeError("Packaged sidecar health check timed out.")
            if process.poll() is not None:
                log = profile / "backend-sidecar.log"
                raise RuntimeError(log.read_text(encoding="utf-8", errors="replace")[-4000:] if log.exists() else "Packaged sidecar exited.")
            assert health["version"] == "2.0.0"
            assert b"/assets/" in request("/")
            assert json.loads(request("/api/watchlist"))["groups"][0]["name"] == "核心自选"
            boundary = "JingYuanSmokeMultipart"
            content = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"fixture.md\"\r\n"
                f"Content-Type: text/markdown\r\n\r\n经营现金流验证资料。\r\n--{boundary}--\r\n"
            ).encode()
            request("/api/knowledge/upload", content, {"Content-Type": "multipart/form-data; boundary=" + boundary})
            assert json.loads(request("/api/knowledge/documents"))["items"][0]["chunks"] > 0
            print("PASS packaged sidecar: SQLite, frontend, watchlist, isolated Markdown import")
        finally:
            if process.poll() is None:
                try:
                    token = (profile / "shutdown-token.txt").read_text().strip()
                    request("/api/shutdown", b"", {"X-Shutdown-Token": token})
                    process.wait(timeout=12)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    process.terminate()
                    process.wait(timeout=5)


if __name__ == "__main__":
    smoke(Path(sys.argv[1]))
