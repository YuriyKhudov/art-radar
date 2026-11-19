"""Completion clients for Art Radar's article editor."""

import atexit
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from zipfile import ZipFile

from radar.editorial_sources import Redirects


VERSION = "b6817"
MODEL = "Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
MODEL_URL = ("https://huggingface.co/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/"
             "resolve/ae44f08e1392f39c0e474af10c3ff8355c8b6688/" + MODEL)
MODEL_SHA = "2fde00ce69dd4899c70d020845e2638353015bba0fdf161b3eb965f2bca4464e"
BUILDS = {
    "win32": ("win-cpu-x64", "a1d6cabeb420c8aa8ce7e7ae8cf76a531cc59470dafacb4f99d42c2613aee826"),
    "linux": ("ubuntu-x64", "b4408b30b6e87ad951629f674dc69fbc199a7ab4b6ccbd54eb8b4c5b4fd7da81"),
}

GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_KEY_ENV = "ART_RADAR_GROQ_API_KEY"


class GroqEditor:
    """Use the same completion interface with a single Groq Free project."""

    def __init__(self, key=None):
        self._key = (os.getenv(GROQ_KEY_ENV, "") if key is None else key).strip()
        self.http = urllib.request.build_opener(Redirects(["api.groq.com"]))
        self.stop_reason = None if self._key else "key-missing"
        self.last_error = self.stop_reason
        self.calls = 0
        self.remaining_tokens = None
        self.reset_tokens_at = 0.0

    def complete(self, system, payload, max_tokens, schema):
        self.last_error = self.stop_reason
        if self.stop_reason:
            return None
        content = json.dumps(payload, ensure_ascii=False)
        completion_limit = max_tokens + 550
        # Estimate conservatively and use the provider's current remaining-token header between calls.
        estimate = (len(system.encode()) + len(content.encode())) // 2 + completion_limit
        if self.remaining_tokens is not None and estimate > self.remaining_tokens:
            remaining = self.reset_tokens_at - time.monotonic()
            if 0 < remaining <= 60:
                time.sleep(remaining + .1)
        body = {"model": GROQ_MODEL, "messages": [{"role": "system", "content": system},
                    {"role": "user", "content": content}], "reasoning_effort": "low", "reasoning_format": "hidden",
                "max_completion_tokens": completion_limit,
                "response_format": {"type": "json_schema", "json_schema": {"name": "article_edit", "strict": True,
                                                                           "schema": schema}}}
        request = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
        self.calls += 1
        try:
            with self.http.open(request, timeout=60) as response:
                tokens = response.headers.get("x-ratelimit-remaining-tokens", "")
                reset = response.headers.get("x-ratelimit-reset-tokens", "")
                match = re.fullmatch(r"(?:(\d+)m)?(\d+(?:\.\d+)?)s", reset)
                if tokens.isdecimal() and match:
                    self.remaining_tokens = int(tokens)
                    self.reset_tokens_at = time.monotonic() + 60 * int(match[1] or 0) + float(match[2])
                value = json.loads(response.read(500_001))
            choice = value["choices"][0]
            if choice.get("finish_reason") != "stop":
                self.last_error = "incomplete-response"
                return None
            result = json.loads(choice["message"]["content"])
            if isinstance(result, dict):
                return result
        except urllib.error.HTTPError as error:
            self.last_error = "quota-exhausted" if error.code == 429 else f"http-{error.code}"
            if error.code in {400, 401, 403, 404, 429}:
                self.stop_reason = self.last_error
            return None
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            pass
        self.last_error = "invalid-response"
        return None


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(path, url, expected):
    if path.is_file() and sha256(path) == expected:
        return
    partial = path.with_suffix(path.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=45) as incoming, partial.open("wb") as target:
            shutil.copyfileobj(incoming, target, length=1024 * 1024)
        if sha256(partial) != expected:
            raise ValueError("Контрольная сумма редакционной модели или рантайма не совпала.")
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


class LocalEditor:
    def __init__(self):
        self.process = None
        self.url = None
        self.failed = False
        self.last_error = None
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def start(self):
        if os.getenv("ART_RADAR_RUN_LOCAL_EDITOR") != "1" or sys.platform not in BUILDS:
            raise ValueError("Локальный редактор не запущен.")
        root = Path(os.getenv("ART_RADAR_EDITOR_CACHE", ".cache/journal-editor")).resolve()
        root.mkdir(parents=True, exist_ok=True)
        platform, checksum = BUILDS[sys.platform]
        archive = f"llama-{VERSION}-bin-{platform}.zip"
        download(root / archive, f"https://github.com/ggml-org/llama.cpp/releases/download/{VERSION}/{archive}", checksum)
        download(root / MODEL, MODEL_URL, MODEL_SHA)
        with ZipFile(root / archive) as files:
            files.extractall(root / VERSION)
            for member in files.infolist():
                permissions = (member.external_attr >> 16) & 0o777
                if permissions:
                    (root / VERSION / member.filename).chmod(permissions)
        binary = next((root / VERSION).rglob("llama-server.exe" if sys.platform == "win32" else "llama-server"))
        binary.chmod(binary.stat().st_mode | 0o100)
        with socket.socket() as port:
            port.bind(("127.0.0.1", 0))
            listen = port.getsockname()[1]
        self.process = subprocess.Popen([str(binary), "-m", MODEL, "--host", "127.0.0.1",
                                         "--port", str(listen), "-c", "12288", "-np", "1", "-t", "4", "-ngl", "0"],
                                        cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        atexit.register(self.close)
        self.url = f"http://127.0.0.1:{listen}"
        for _ in range(45):
            if self.process.poll() is not None:
                break
            try:
                with self.http.open(self.url + "/health", timeout=2):
                    return
            except OSError:
                time.sleep(1)
        self.close()
        raise ValueError("Локальная модель не запустилась.")

    def complete(self, system, payload, max_tokens, schema):
        self.last_error = None
        if self.failed:
            self.last_error = "startup"
            return None
        try:
            if self.process is None:
                self.start()
        except (OSError, ValueError, StopIteration):
            self.failed = True
            self.last_error = "startup"
            return None
        try:
            request = {"messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                       "response_format": {"type": "json_object", "schema": schema}, "temperature": 0, "seed": 21,
                       "max_tokens": max_tokens, "stream": True}
            call = urllib.request.Request(self.url + "/v1/chat/completions",
                                          data=json.dumps(request, ensure_ascii=False).encode("utf-8"),
                                          headers={"Content-Type": "application/json"})
            deadline = time.monotonic() + 165
            completion = ""
            with self.http.open(call, timeout=70) as response:
                for line in response:
                    if time.monotonic() >= deadline:
                        self.last_error = "deadline"
                        return None
                    if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
                        continue
                    choice = next(iter(json.loads(line[6:]).get("choices", [])), {})
                    content = choice.get("delta", {}).get("content") or ""
                    completion += content
                    if content and completion.rstrip().endswith("}"):
                        try:
                            return json.loads(completion)
                        except json.JSONDecodeError:
                            pass
            self.last_error = "incomplete-json"
        except (TimeoutError, OSError) as error:
            self.last_error = "timeout" if isinstance(error, TimeoutError) else "connection"
            if self.process and self.process.poll() is not None:
                self.process = None
        except (ValueError, KeyError, TypeError):
            self.last_error = "invalid-stream"
            return None
        return None
