"""Model providers behind one interface, plus API key storage.

Anthropic, OpenAI and Gemini are supported, each for direct requests and for
their batch APIs. Stage instructions are sent as a cacheable system prompt.

Typical usage example:

  provider = make_provider("anthropic", model="claude-opus-5-5")
  response = provider.generate(system, user, image_path, schema)
"""

import base64
import io
import json
import os
import random
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

KEYRING_SERVICE = "gtvh-multimodal-analyzer"

PROVIDERS = {
    "anthropic": {"label": "Anthropic (Claude)", "env": ["ANTHROPIC_API_KEY"], "default_model": "claude-opus-5-5",
                  "efforts": ["low", "medium", "high", "xhigh", "max"]},
    "openai": {"label": "OpenAI (GPT)", "env": ["OPENAI_API_KEY"], "default_model": None,
               "efforts": ["minimal", "low", "medium", "high"]},
    "gemini": {"label": "Google (Gemini)", "env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"], "default_model": None,
               "efforts": []},
}
MAX_IMAGE_BYTES = 4_500_000
_UNSUPPORTED = {"$schema", "$id", "title", "minLength", "maxLength", "pattern", "minimum", "maximum",
                "minItems", "maxItems", "format"}


class ProviderError(Exception):
    pass


class SchemaRejected(ProviderError):
    pass


class StopRun(ProviderError):
    pass


class _Transient(ProviderError):
    pass


@dataclass
class Response:
    text: str
    usage: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


_ENV_FILE_VARS = {}


def load_env_file(start=None):
    folder = Path(start or Path.cwd()).resolve()
    for d in (folder, *folder.parents):
        f = d / ".env"
        if f.is_file():
            for line in f.read_text(encoding="utf-8-sig").splitlines():
                key, sep, value = line.strip().removeprefix("export ").partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if sep and key and value and not line.lstrip().startswith("#") and key not in os.environ:
                    os.environ[key] = value
                    _ENV_FILE_VARS[key] = f
            return f
    return None


def get_key(provider):
    for var in PROVIDERS[provider]["env"]:
        if os.environ.get(var):
            where = _ENV_FILE_VARS.get(var)
            return os.environ[var], f"{where} ({var})" if where else f"environment variable {var}"
    try:
        import keyring
        k = keyring.get_password(KEYRING_SERVICE, provider)
        if k:
            return k, "credential store"
    except Exception:
        pass
    return None, None


def set_key(provider, key):
    import keyring
    keyring.set_password(KEYRING_SERVICE, provider, key.strip())


def delete_key(provider):
    import keyring
    try:
        keyring.delete_password(KEYRING_SERVICE, provider)
    except keyring.errors.PasswordDeleteError:
        pass


def transport_schema(node):
    if isinstance(node, dict):
        out = {k: transport_schema(v) for k, v in node.items() if k not in _UNSUPPORTED}
        if "const" in out:
            out["enum"] = [out.pop("const")]
        return out
    if isinstance(node, list):
        return [transport_schema(x) for x in node]
    return node


def load_image(path):
    path = Path(path)
    data = path.read_bytes()
    ext = path.suffix.lower().lstrip(".")
    media = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
             "webp": "image/webp"}[ext]
    if len(data) <= MAX_IMAGE_BYTES:
        return data, media, None
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGB")
    quality, scale = 92, 1.0
    while True:
        im = img if scale == 1.0 else img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        if buf.tell() <= MAX_IMAGE_BYTES or scale < 0.3:
            return buf.getvalue(), "image/jpeg", f"re-encoded as JPEG {im.width}x{im.height} (q{quality})"
        if quality > 80:
            quality -= 6
        else:
            scale *= 0.85


def extract_json(text):
    if not text:
        return None
    t = re.sub(r"^```(?:json)?\s*", "", text.strip())
    t = re.sub(r"\s*```\s*$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except json.JSONDecodeError:
            return None
    return None


def _field(obj, key):
    if obj is None:
        return None
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


class Provider:
    name = ""
    max_tokens = 16000
    batch_limit_bytes = 150_000_000
    retry_delays = (20, 40, 80, 160, 300)

    def __init__(self, model=None, effort=None, api_key=None, timeout=600, cancel=None):
        spec = PROVIDERS[self.name]
        self.model = model or spec["default_model"]
        if not self.model:
            raise ProviderError(f"Choose a model for {spec['label']} (list them with: gtvh models {self.name}).")
        self.effort = effort
        self.timeout = timeout
        self.cancel = cancel or threading.Event()
        if api_key is None:
            api_key, _ = get_key(self.name)
            if not api_key:
                raise StopRun(f"No API key for {spec['label']}. Add one with: gtvh keys set {self.name} "
                              f"(or in the app's Settings).")
        self.api_key = api_key
        self.client = self._client()

    def _client(self):
        raise NotImplementedError

    def list_models(self):
        raise NotImplementedError

    def _retrying(self, fn):
        delays = self.retry_delays
        for attempt in range(len(delays) + 1):
            try:
                return fn()
            except _Transient as e:
                if attempt == len(delays):
                    raise StopRun(f"{e} (still failing after {len(delays)} retries; run again later)") from e
                if self.cancel.wait(delays[attempt] * random.uniform(1, 1.25)):
                    raise StopRun("cancelled") from e

    def generate(self, system, user, image=None, schema=None, name="output"):
        schema = transport_schema(schema) if schema else None
        return self._retrying(lambda: self._call(system, user, image, schema, name))

    def batch_submit(self, requests):
        requests = [dict(q, schema=transport_schema(q["schema"]) if q.get("schema") else None) for q in requests]
        return self._retrying(lambda: self._batch_submit(requests))

    def batch_poll(self, job):
        return self._retrying(lambda: self._batch_poll(job))

    def batch_results(self, job):
        return self._retrying(lambda: self._batch_results(job))


class AnthropicProvider(Provider):
    name = "anthropic"
    batch_limit_bytes = 200_000_000

    def _client(self):
        import anthropic
        return anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout, max_retries=3)

    def _api(self, fn, schema=None):
        import anthropic
        try:
            return fn()
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise StopRun(f"Anthropic rejected the API key: {e.message}") from e
        except anthropic.NotFoundError as e:
            raise StopRun(f"Anthropic: model '{self.model}' or batch not found: {e.message}") from e
        except anthropic.RateLimitError as e:
            raise _Transient(f"Anthropic rate limit: {e.message}") from e
        except anthropic.BadRequestError as e:
            if schema and "schema" in str(e.message).lower():
                raise SchemaRejected(e.message) from e
            raise ProviderError(f"Anthropic bad request: {e.message}") from e
        except anthropic.APIStatusError as e:
            if e.status_code == 402 or "credit" in str(e.message).lower():
                raise StopRun(f"Anthropic billing problem: {e.message}") from e
            if e.status_code >= 500:
                raise _Transient(f"Anthropic server error {e.status_code}: {e.message}") from e
            raise ProviderError(f"Anthropic error {e.status_code}: {e.message}") from e
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            raise _Transient(f"Anthropic connection problem: {e}") from e

    def _params(self, system, user, image, schema, batch=False):
        content, note = [], None
        if image is not None:
            data, media, note = load_image(image)
            content.append({"type": "image", "source": {"type": "base64", "media_type": media,
                                                        "data": base64.standard_b64encode(data).decode()}})
        content.append({"type": "text", "text": user})
        cache = {"type": "ephemeral", "ttl": "1h"} if batch else {"type": "ephemeral"}
        kw = {"model": self.model, "max_tokens": self.max_tokens,
              "system": [{"type": "text", "text": system, "cache_control": cache}],
              "messages": [{"role": "user", "content": content}]}
        oc = {}
        if schema:
            oc["format"] = {"type": "json_schema", "schema": schema}
        if self.effort:
            oc["effort"] = self.effort
        if oc:
            kw["output_config"] = oc
        return kw, note

    def _response(self, r, note=None):
        text = next((b.text for b in r.content if b.type == "text"), "")
        u = r.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_read_tokens": getattr(u, "cache_read_input_tokens", None) or 0,
                 "cache_write_tokens": getattr(u, "cache_creation_input_tokens", None) or 0}
        meta = {"stop_reason": r.stop_reason, "model_served": r.model}
        if note:
            meta["image_note"] = note
        if r.stop_reason == "refusal":
            meta["refusal"] = str(getattr(r, "stop_details", None))
        return Response(text, usage, meta)

    def _call(self, system, user, image, schema, name):
        kw, note = self._params(system, user, image, schema)
        return self._response(self._api(lambda: self.client.messages.create(**kw), schema), note)

    def _batch_submit(self, requests):
        reqs = [{"custom_id": q["rid"], "params": self._params(q["system"], q["user"], q["image"], q["schema"],
                                                               batch=True)[0]} for q in requests]
        return self._api(lambda: self.client.messages.batches.create(requests=reqs)).id

    def _batch_poll(self, job):
        b = self._api(lambda: self.client.messages.batches.retrieve(job))
        c = b.request_counts
        detail = f"{c.succeeded + c.errored + c.canceled + c.expired} of {c.processing + c.succeeded + c.errored + c.canceled + c.expired} done"
        return ("done" if b.processing_status == "ended" else "running"), detail

    def _batch_results(self, job):
        out = {}
        for r in self._api(lambda: list(self.client.messages.batches.results(job))):
            if r.result.type == "succeeded":
                out[r.custom_id] = self._response(r.result.message)
            else:
                out[r.custom_id] = ProviderError(f"batch request {r.result.type}: {getattr(r.result, 'error', '')}")
        return out

    def list_models(self):
        return [m.id for m in self.client.models.list()]


class OpenAIProvider(Provider):
    name = "openai"
    batch_limit_bytes = 180_000_000

    def _client(self):
        import openai
        return openai.OpenAI(api_key=self.api_key, timeout=self.timeout, max_retries=3)

    def _api(self, fn, schema=None):
        import openai
        try:
            return fn()
        except (openai.AuthenticationError, openai.PermissionDeniedError) as e:
            raise StopRun(f"OpenAI rejected the API key: {e.message}") from e
        except openai.NotFoundError as e:
            raise StopRun(f"OpenAI: model '{self.model}' or batch not found: {e.message}") from e
        except openai.RateLimitError as e:
            if getattr(e, "code", None) == "insufficient_quota" or "quota" in str(e.message).lower():
                raise StopRun(f"OpenAI quota exhausted: {e.message}") from e
            raise _Transient(f"OpenAI rate limit: {e.message}") from e
        except openai.BadRequestError as e:
            if schema and ("schema" in str(e.message).lower() or "response_format" in str(e.message).lower()):
                raise SchemaRejected(e.message) from e
            raise ProviderError(f"OpenAI bad request: {e.message}") from e
        except openai.APIStatusError as e:
            if e.status_code >= 500:
                raise _Transient(f"OpenAI server error {e.status_code}: {e.message}") from e
            raise ProviderError(f"OpenAI error {e.status_code}: {e.message}") from e
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            raise _Transient(f"OpenAI connection problem: {e}") from e

    def _params(self, system, user, image, schema, name):
        content, note = [], None
        if image is not None:
            data, media, note = load_image(image)
            content.append({"type": "input_image", "detail": "high",
                            "image_url": f"data:{media};base64,{base64.b64encode(data).decode()}"})
        content.append({"type": "input_text", "text": user})
        kw = {"model": self.model, "instructions": system, "max_output_tokens": self.max_tokens * 2,
              "input": [{"role": "user", "content": content}], "store": False,
              "prompt_cache_key": f"gtvh-{name}"}
        if schema:
            kw["text"] = {"format": {"type": "json_schema", "name": re.sub(r"\W", "_", name), "schema": schema,
                                     "strict": True}}
        if self.effort:
            kw["reasoning"] = {"effort": self.effort}
        return kw, note

    @staticmethod
    def _usage(u):
        return {"input_tokens": _field(u, "input_tokens"), "output_tokens": _field(u, "output_tokens"),
                "cache_read_tokens": _field(_field(u, "input_tokens_details"), "cached_tokens") or 0,
                "reasoning_tokens": _field(_field(u, "output_tokens_details"), "reasoning_tokens") or 0}

    def _call(self, system, user, image, schema, name):
        kw, note = self._params(system, user, image, schema, name)
        r = self._api(lambda: self.client.responses.create(**kw), schema)
        meta = {"status": r.status, "model_served": r.model}
        if r.status == "incomplete":
            meta["incomplete"] = str(getattr(r, "incomplete_details", ""))
        if note:
            meta["image_note"] = note
        return Response(r.output_text or "", self._usage(r.usage), meta)

    def _batch_submit(self, requests):
        lines = [json.dumps({"custom_id": q["rid"], "method": "POST", "url": "/v1/responses",
                             "body": self._params(q["system"], q["user"], q["image"], q["schema"], q["name"])[0]})
                 for q in requests]
        f = self._api(lambda: self.client.files.create(file=("gtvh-batch.jsonl", "\n".join(lines).encode("utf-8")),
                                                       purpose="batch"))
        return self._api(lambda: self.client.batches.create(input_file_id=f.id, endpoint="/v1/responses",
                                                            completion_window="24h")).id

    def _batch_poll(self, job):
        b = self._api(lambda: self.client.batches.retrieve(job))
        c = b.request_counts
        detail = f"{(c.completed + c.failed) if c else 0} of {c.total if c else '?'} done ({b.status})"
        if b.status in ("completed", "expired"):
            return "done", detail
        if b.status in ("failed", "cancelled"):
            return "failed", f"{b.status}: {b.errors}"
        return "running", detail

    def _batch_results(self, job):
        b = self._api(lambda: self.client.batches.retrieve(job))
        out = {}
        for fid in (b.output_file_id, b.error_file_id):
            if not fid:
                continue
            for line in self._api(lambda: self.client.files.content(fid).text).splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                resp = d.get("response") or {}
                body = resp.get("body") or {}
                if resp.get("status_code") == 200:
                    text = "".join(c.get("text", "") for item in body.get("output", []) if item.get("type") == "message"
                                   for c in item.get("content", []) if c.get("type") == "output_text")
                    out[d["custom_id"]] = Response(text, self._usage(body.get("usage")),
                                                   {"status": body.get("status"), "model_served": body.get("model")})
                else:
                    out[d["custom_id"]] = ProviderError(f"batch request failed: {d.get('error') or body.get('error')}")
        return out

    def list_models(self):
        ids = [m.id for m in self.client.models.list()]
        return sorted(i for i in ids if re.match(r"(gpt-|o\d|chatgpt)", i))


class GeminiProvider(Provider):
    name = "gemini"
    max_tokens = 32000
    batch_limit_bytes = 1_500_000_000

    def _client(self):
        from google import genai
        from google.genai import types
        return genai.Client(api_key=self.api_key, http_options=types.HttpOptions(timeout=self.timeout * 1000))

    def _api(self, fn, schema=None):
        from google.genai import errors
        try:
            return fn()
        except errors.ClientError as e:
            msg = str(e)
            if e.code in (401, 403):
                raise StopRun(f"Gemini rejected the API key: {msg}") from e
            if e.code == 404:
                raise StopRun(f"Gemini: model '{self.model}' or batch not found: {msg}") from e
            if e.code == 429:
                raise _Transient(f"Gemini rate limit: {msg}") from e
            if e.code == 400 and schema and "schema" in msg.lower():
                raise SchemaRejected(msg) from e
            raise ProviderError(f"Gemini error {e.code}: {msg}") from e
        except errors.ServerError as e:
            raise _Transient(f"Gemini server error: {e}") from e
        except errors.APIError as e:
            raise ProviderError(f"Gemini error: {e}") from e
        except OSError as e:
            raise _Transient(f"Gemini connection problem: {e}") from e

    def _call(self, system, user, image, schema, name):
        from google.genai import types
        contents, note = [], None
        if image is not None:
            data, media, note = load_image(image)
            contents.append(types.Part.from_bytes(data=data, mime_type=media))
        contents.append(user)
        cfg = {"system_instruction": system, "max_output_tokens": self.max_tokens}
        if schema:
            cfg.update(response_mime_type="application/json", response_json_schema=schema)
        r = self._api(lambda: self.client.models.generate_content(model=self.model, contents=contents,
                                                                  config=types.GenerateContentConfig(**cfg)), schema)
        u = r.usage_metadata
        usage = {"input_tokens": getattr(u, "prompt_token_count", None),
                 "output_tokens": getattr(u, "candidates_token_count", None),
                 "cache_read_tokens": getattr(u, "cached_content_token_count", None) or 0,
                 "reasoning_tokens": getattr(u, "thoughts_token_count", None) or 0}
        cand = r.candidates[0] if r.candidates else None
        meta = {"finish_reason": str(getattr(cand, "finish_reason", None)), "model_served": r.model_version}
        if note:
            meta["image_note"] = note
        try:
            text = r.text or ""
        except ValueError:
            text = ""
        return Response(text, usage, meta)

    def _batch_request(self, q):
        parts = []
        if q["image"] is not None:
            data, media, _ = load_image(q["image"])
            parts.append({"inlineData": {"mimeType": media, "data": base64.b64encode(data).decode()}})
        parts.append({"text": q["user"]})
        gen = {"maxOutputTokens": self.max_tokens}
        if q["schema"]:
            gen.update(responseMimeType="application/json", responseJsonSchema=q["schema"])
        return {"contents": [{"role": "user", "parts": parts}],
                "systemInstruction": {"parts": [{"text": q["system"]}]}, "generationConfig": gen}

    def _batch_submit(self, requests):
        from google.genai import types
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            for q in requests:
                f.write(json.dumps({"key": q["rid"], "request": self._batch_request(q)}) + "\n")
        try:
            up = self._api(lambda: self.client.files.upload(
                file=f.name, config=types.UploadFileConfig(display_name="gtvh-batch", mime_type="jsonl")))
            return self._api(lambda: self.client.batches.create(model=self.model, src=up.name,
                                                                config={"display_name": "gtvh"})).name
        finally:
            os.unlink(f.name)

    def _batch_poll(self, job):
        j = self._api(lambda: self.client.batches.get(name=job))
        state = j.state.name if j.state else "UNKNOWN"
        if state in ("JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"):
            return "done", state
        if state in ("JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"):
            return "failed", f"{state}: {j.error}"
        return "running", state

    def _batch_results(self, job):
        j = self._api(lambda: self.client.batches.get(name=job))
        data = self._api(lambda: self.client.files.download(file=j.dest.file_name))
        out = {}
        for line in data.decode("utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            resp = d.get("response")
            if not resp:
                out[d.get("key")] = ProviderError(f"batch request failed: {d.get('error') or d.get('status')}")
                continue
            cand = (resp.get("candidates") or [{}])[0]
            text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
            u = resp.get("usageMetadata") or {}
            out[d.get("key")] = Response(text, {"input_tokens": u.get("promptTokenCount"),
                                                "output_tokens": u.get("candidatesTokenCount"),
                                                "cache_read_tokens": u.get("cachedContentTokenCount") or 0,
                                                "reasoning_tokens": u.get("thoughtsTokenCount") or 0},
                                         {"finish_reason": cand.get("finishReason"),
                                          "model_served": resp.get("modelVersion")})
        return out

    def list_models(self):
        out = []
        for m in self.client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if "generateContent" in actions and "gemini" in m.name:
                out.append(m.name.removeprefix("models/"))
        return sorted(out)


_CLASSES = {c.name: c for c in (AnthropicProvider, OpenAIProvider, GeminiProvider)}


def make_provider(name, **kw):
    if name not in _CLASSES:
        raise ProviderError(f"Unknown provider '{name}'. Choose one of: {', '.join(_CLASSES)}")
    return _CLASSES[name](**kw)
