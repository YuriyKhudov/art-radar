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

CLOUDFLARE_MODEL = "@cf/google/gemma-4-26b-a4b-it"
CLOUDFLARE_KEY_ENV = "ART_RADAR_CLOUDFLARE_API_TOKEN"
CLOUDFLARE_ACCOUNT_ENV = "ART_RADAR_CLOUDFLARE_ACCOUNT_ID"
# The account's Workers Free limit is 10,000 Neurons/day. Leave room for
# other runs and other account usage even when request accounting is unavailable.
CLOUDFLARE_RUN_NEURONS = 1000


class CloudflareEditor:
    """Use the editor completion interface with one Workers Free model."""

    def __init__(self, key=None, account=None):
        self._key = (os.getenv(CLOUDFLARE_KEY_ENV, "") if key is None else key).strip()
        self._account = (os.getenv(CLOUDFLARE_ACCOUNT_ENV, "") if account is None else account).strip()
        self.http = urllib.request.build_opener(Redirects([]))
        self.stop_reason = None if self._key and re.fullmatch(r"[a-fA-F0-9]{32}", self._account) else "credentials-missing"
        self.last_error = self.stop_reason
        self.calls = 0
        self.neurons = 0.0

    @staticmethod
    def charge(input_tokens, output_tokens):
        return (input_tokens * 9091 + output_tokens * 27273) / 1_000_000

    def complete(self, system, payload, max_tokens, schema):
        self.last_error = self.stop_reason
        if self.stop_reason:
            return None
        body = {"model": CLOUDFLARE_MODEL, "messages": [{"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                "chat_template_kwargs": {"enable_thinking": False}, "temperature": 0.2,
                "max_completion_tokens": max_tokens,
                "response_format": {"type": "json_schema", "json_schema": {"name": "article_edit", "strict": True,
                                                                           "schema": schema}}}
        data = json.dumps(body, ensure_ascii=False).encode()
        estimate = self.charge(len(data) + 64, max_tokens)
        if self.neurons + estimate > CLOUDFLARE_RUN_NEURONS:
            self.stop_reason = self.last_error = "run-budget"
            return None
        self.neurons += estimate
        url = f"https://api.cloudflare.com/client/v4/accounts/{self._account}/ai/v1/chat/completions"
        request = urllib.request.Request(url, data=data,
                                         headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                                                  "Accept": "application/json"})
        self.calls += 1
        try:
            with self.http.open(request, timeout=45) as response:
                value = json.loads(response.read(500_001))
            usage = value.get("usage", {})
            counts = [usage.get(key) for key in ("prompt_tokens", "completion_tokens")] if isinstance(usage, dict) else []
            if len(counts) == 2 and all(type(count) is int and count >= 0 for count in counts):
                self.neurons += self.charge(*counts) - estimate
            choice = value["choices"][0]
            if choice.get("finish_reason") != "stop":
                self.last_error = "incomplete-response"
                return None
            content = choice["message"]["content"]
            result = json.loads(content) if isinstance(content, str) else content
            if isinstance(result, dict):
                return result
        except urllib.error.HTTPError as error:
            self.last_error = f"http-{error.code}"
            if error.code == 429:
                try:
                    codes = {item["code"] for item in json.loads(error.read(8_192)).get("errors", [])}
                except (AttributeError, KeyError, TypeError, ValueError):
                    codes = set()
                self.last_error = "capacity" if 3040 in codes and 3036 not in codes else "quota-exhausted"
            if error.code in {401, 403, 404, 410} or self.last_error == "quota-exhausted":
                self.stop_reason = self.last_error
            return None
        except (AttributeError, OSError, ValueError, KeyError, TypeError, IndexError):
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
