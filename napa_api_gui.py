"""
NAPA Voyage Optimization API GUI Tester

One Tkinter app for testing the NAPA Voyage Optimization API:
- Swagger-driven endpoint tabs limited to the enabled project APIs
- x-api-key authentication
- Editable request JSON or form fields
- Response logging and async Location follow-up
- In-app 3D globe map preview

Run:
    python napa_api_gui.py
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

try:
    import requests
except ImportError:  # pragma: no cover - exercised only before dependencies are installed.
    requests = None  # type: ignore[assignment]

try:
    from PIL import Image, ImageTk
except ImportError:  # pragma: no cover - optional visual enhancement.
    Image = None  # type: ignore[assignment]
    ImageTk = None  # type: ignore[assignment]


DEFAULT_BASE_URL = "https://api.fleetintelligence.napa.fi/vo"
DEFAULT_SWAGGER_URL = "https://api.fleetintelligence.napa.fi/vo/v1/swagger.json"
DEFAULT_TIMEOUT_SECONDS = 60
POLL_INTERVAL_SECONDS = 3
POLL_MAX_ATTEMPTS = 40
EARTH_TEXTURE_URL = (
    "https://assets.science.nasa.gov/content/dam/science/esd/eo/images/bmng/"
    "bmng-base/january/world.200401.3x5400x2700.jpg"
)
EARTH_TEXTURE_CACHE_NAME = "blue_marble_200401_2048.jpg"
EARTH_TEXTURE_WIDTH = 2048
EARTH_TEXTURE_HEIGHT = 1024

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
ASYNC_DONE_STATES = {"completed", "complete", "done", "finished", "failed", "failure", "error", "ready"}
ASYNC_WAIT_STATES = {"accepted", "queued", "pending", "running", "processing", "inprogress", "in_progress"}

ENABLED_ENDPOINT_PATHS = (
    "/v1/performance-models/tune",
    "/v1/performance-models/tune-relative",
    "/v1/find-shortest-voyage",
    "/v1/find-optimal-voyage",
    "/v1/try-get-voyage",
    "/v2/calculate-voyage-plan",
)
ENABLED_ENDPOINT_RANK = {path: index for index, path in enumerate(ENABLED_ENDPOINT_PATHS)}

PARAMETER_DEFAULTS = {
    "guid": "00000000-0000-0000-0000-000000000000",
    "uid": "00000000-0000-0000-0000-000000000000",
    "id": "00000000-0000-0000-0000-000000000000",
    "imo": "9629457",
    "imoNumber": "9629457",
    "voyageId": "sample-voyage-id",
    "performanceModelId": "00000000-0000-0000-0000-000000000000",
    "routeNetworkVersion": "",
}


def _load_local_defaults() -> Dict[str, str]:
    path = Path(__file__).with_name("napa_gui_defaults.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {key: str(value) for key, value in data.items() if value is not None}


LOCAL_DEFAULTS = _load_local_defaults()


def require_requests() -> Any:
    if requests is None:
        raise RuntimeError("Missing dependency: run 'pip install -r requirements_napa_gui.txt' first.")
    return requests


@dataclass
class EndpointSpec:
    tag: str
    method: str
    path: str
    summary: str = ""
    content_type: str = ""
    example: Any = None
    path_params: Dict[str, str] = field(default_factory=dict)
    query_params: Dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.method.upper()} {self.path}"

    @property
    def display_summary(self) -> str:
        return self.summary.strip() or "No summary in Swagger."


def _default_for_parameter(name: str, schema: Optional[Dict[str, Any]] = None) -> str:
    if name in PARAMETER_DEFAULTS:
        return PARAMETER_DEFAULTS[name]
    schema = schema or {}
    if "default" in schema:
        return str(schema["default"])
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return str(enum_values[0])
    schema_type = schema.get("type")
    if schema_type == "integer":
        return "0"
    if schema_type == "number":
        return "0"
    if schema_type == "boolean":
        return "false"
    return ""


def _first_json_content(content: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    if "application/json" in content:
        return "application/json", content["application/json"]
    if not content:
        return "", {}
    content_type = next(iter(content))
    return content_type, content[content_type]


def parse_swagger(data: Dict[str, Any]) -> List[EndpointSpec]:
    endpoints: List[EndpointSpec] = []
    paths = data.get("paths") or {}
    tag_order = [item.get("name") for item in data.get("tags", []) if item.get("name")]
    tag_rank = {name: index for index, name in enumerate(tag_order)}

    for path, operations in paths.items():
        if path not in ENABLED_ENDPOINT_RANK:
            continue
        if not isinstance(operations, dict):
            continue
        for method in HTTP_METHODS:
            operation = operations.get(method)
            if not isinstance(operation, dict):
                continue

            tag = (operation.get("tags") or ["Other"])[0]
            summary = operation.get("summary") or operation.get("operationId") or ""
            body = operation.get("requestBody") or {}
            content_type, content = _first_json_content(body.get("content") or {})
            example = content.get("example") if isinstance(content, dict) else None
            if example is None and content_type in {"multipart/form-data", "application/x-www-form-urlencoded"}:
                schema = content.get("schema") if isinstance(content, dict) else {}
                properties = schema.get("properties") if isinstance(schema, dict) else {}
                if isinstance(properties, dict):
                    example = {
                        name: _default_for_parameter(name, prop if isinstance(prop, dict) else {})
                        for name, prop in properties.items()
                    }
            path_params: Dict[str, str] = {}
            query_params: Dict[str, str] = {}

            for parameter in operation.get("parameters") or []:
                name = parameter.get("name")
                location = parameter.get("in")
                if not name or location not in {"path", "query"}:
                    continue
                value = _default_for_parameter(name, parameter.get("schema") or {})
                if location == "path":
                    path_params[name] = value
                else:
                    query_params[name] = value

            endpoints.append(
                EndpointSpec(
                    tag=tag,
                    method=method.upper(),
                    path=path,
                    summary=summary,
                    content_type=content_type,
                    example=example,
                    path_params=path_params,
                    query_params=query_params,
                )
            )

    endpoints.sort(
        key=lambda item: (
            ENABLED_ENDPOINT_RANK.get(item.path, 999),
            tag_rank.get(item.tag, 999),
            item.tag,
            item.method,
        )
    )
    return endpoints


def fallback_endpoints() -> List[EndpointSpec]:
    examples: List[EndpointSpec] = [
        EndpointSpec(
            tag="PerformanceModel",
            method="POST",
            path="/v1/performance-models/tune",
            summary="Tunes a given base performance model to match fuel oil consumption in ideal conditions.",
            content_type="application/json",
            example={
                "imoNumber": 9629457,
                "tuningPoints": [
                    {
                        "draft": 10,
                        "speedOverGround": 6,
                        "dailyFuelConsumption": 25000,
                    }
                ],
            },
        ),
        EndpointSpec(
            tag="PerformanceModel",
            method="POST",
            path="/v1/performance-models/tune-relative",
            summary="Tunes a given base performance model by a relative fuel consumption factor.",
            content_type="application/json",
            example={
                "imoNumber": 9629457,
                "draft": 10,
                "speedOverGround": 6,
                "fuelConsumptionFactor": 1.15,
            },
        ),
        EndpointSpec(
            tag="Voyage",
            method="POST",
            path="/v1/find-shortest-voyage",
            summary="Returns the location for the voyage with the shortest route.",
            content_type="application/json",
            example={
                "imoNumber": 9629457,
                "fromCoordinates": {"latitude": 60.18333333, "longitude": 24.96666667},
                "toCoordinates": {"latitude": 53.96666718, "longitude": 10.9},
                "startTime": "2026-07-09T00:00:00Z",
                "draft": 14,
                "operationMethod": {"speedOverGround": 7},
                "fuels": {
                    "availableFuels": [
                        {"type": "LSFO", "price": 400, "lowerHeatValue": 41600},
                        {"type": "MGO", "price": 600, "lowerHeatValue": 42800},
                    ],
                    "outsideEca": "LSFO",
                    "insideEca": "MGO",
                },
                "constraints": {
                    "maximumWaveHeight": 7,
                    "propellerRpm": {"allowedRange": {"min": 20, "max": 80}},
                },
            },
        ),
        EndpointSpec(
            tag="Voyage",
            method="POST",
            path="/v1/find-optimal-voyage",
            summary="Returns the location for the weather-optimized voyage.",
            content_type="application/json",
            example={
                "imoNumber": 9629457,
                "fromCoordinates": {"latitude": 60.18333333, "longitude": 24.96666667},
                "toCoordinates": {"latitude": 53.96666718, "longitude": 10.9},
                "startTime": "2026-07-09T00:00:00Z",
                "draft": 14,
                "operationMethod": {"speedOverGround": 7},
                "fuels": {
                    "availableFuels": [
                        {"type": "LSFO", "price": 400, "lowerHeatValue": 41600},
                        {"type": "MGO", "price": 600, "lowerHeatValue": 42800},
                    ],
                    "outsideEca": "LSFO",
                    "insideEca": "MGO",
                },
                "constraints": {
                    "maximumWaveHeight": 7,
                    "propellerRpm": {"allowedRange": {"min": 20, "max": 80}},
                },
            },
        ),
        EndpointSpec(
            tag="Voyage",
            method="GET",
            path="/v1/try-get-voyage",
            summary="Returns the status of the voyage calculation for the provided GUID.",
            query_params={"guid": PARAMETER_DEFAULTS["guid"]},
        ),
        EndpointSpec(
            tag="Voyage",
            method="POST",
            path="/v2/calculate-voyage-plan",
            summary="Returns the voyage for a given voyage plan.",
            content_type="application/json",
            example={
                "imoNumber": 9629457,
                "coordinates": [
                    {"latitude": 60.1533167, "longitude": 24.9489667},
                    {"latitude": 58.549169, "longitude": 21.042663},
                    {"latitude": 56.468645, "longitude": 17.524141},
                    {"latitude": 55.792767, "longitude": 15.700957},
                    {"latitude": 54.651725, "longitude": 12.375373},
                    {"latitude": 53.96666718, "longitude": 10.9},
                ],
                "draft": 14,
                "timestamps": [
                    "2018-10-30T00:00:00+00:00",
                    "2018-10-30T11:18:46+00:00",
                    "2018-10-30T23:44:34+00:00",
                    "2018-10-31T05:08:32+00:00",
                    "2018-10-31T14:56:09+00:00",
                    "2018-10-31T19:48:06+00:00",
                ],
                "constraints": {
                    "maximumWaveHeight": 7,
                    "propellerRpm": {"allowedRange": {"min": 20, "max": 80}},
                },
            },
        ),
    ]
    return examples


def normalize_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/") or DEFAULT_BASE_URL


def build_url(base_url: str, path: str, path_params: Dict[str, str]) -> str:
    expanded = path
    for match in re.findall(r"{([^}]+)}", path):
        value = path_params.get(match, "").strip()
        if not value:
            raise ValueError(f"Path parameter '{match}' is empty.")
        expanded = expanded.replace("{" + match + "}", value)
    return normalize_base_url(base_url) + "/" + expanded.lstrip("/")


def absolute_location(base_url: str, location: str) -> str:
    location = location.strip()
    if not location:
        return ""
    if location.startswith("http://") or location.startswith("https://"):
        return normalize_async_location(location)
    parsed = urlparse(normalize_base_url(base_url))
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if location.startswith("/"):
        return normalize_async_location(urljoin(origin, location))
    return normalize_async_location(urljoin(normalize_base_url(base_url) + "/", location))


def normalize_async_location(location: str) -> str:
    parsed = urlparse(location)
    if parsed.path.endswith("/vo/try-get-voyage"):
        parsed = parsed._replace(path=parsed.path[: -len("/try-get-voyage")] + "/v1/try-get-voyage")
    return urlunparse(parsed)


def parse_key_values(raw: str, skip_empty: bool = True) -> Dict[str, str]:
    raw = raw.strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("Parameter JSON must be an object.")
        return {str(key): "" if value is None else str(value) for key, value in parsed.items()}

    result: Dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Expected key=value format: {line}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if skip_empty and value == "":
            continue
        result[key] = value
    return result


def format_key_values(values: Dict[str, str]) -> str:
    return "\n".join(f"{key}={value}" for key, value in values.items())


def json_preview(data: Any, limit: int = 60000) -> str:
    try:
        text = json.dumps(data, indent=2, ensure_ascii=False)
    except Exception:
        text = str(data)
    if len(text) > limit:
        return text[:limit] + "\n\n... truncated ..."
    return text


def _response_body_text(response: requests.Response) -> Tuple[Optional[Any], str]:
    if not response.content:
        return None, ""
    try:
        parsed = response.json()
        return parsed, json_preview(parsed)
    except Exception:
        return None, response.text[:60000]


class LogMixin:
    def _init_log_queue(self) -> None:
        self.log_queue: queue.Queue[str] = queue.Queue()

    def log(self, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {message}\n")

    def _flush_log_queue(self) -> None:
        while not self.log_queue.empty():
            self.log_text.insert(tk.END, self.log_queue.get())
            self.log_text.see(tk.END)
        self.after(100, self._flush_log_queue)


class NapaApiFrame(ttk.Frame, LogMixin):
    def __init__(self, master: tk.Misc, tag: str, endpoints: List[EndpointSpec]) -> None:
        super().__init__(master)
        self.tag = tag
        self.endpoints: List[EndpointSpec] = []
        self.endpoint_by_label: Dict[str, EndpointSpec] = {}
        self.latest_response_data: Any = None
        self.latest_response_text = ""
        self.latest_location = ""
        self.auto_poll_var = tk.BooleanVar(value=True)

        self._init_log_queue()
        self._build_widgets()
        self.set_endpoints(endpoints)
        self.after(100, self._flush_log_queue)

    def _build_widgets(self) -> None:
        controls = ttk.Frame(self, padding=(8, 8, 8, 4))
        controls.pack(fill=tk.X)

        ttk.Label(controls, text="Endpoint").grid(row=0, column=0, sticky="w")
        self.endpoint_var = tk.StringVar()
        self.endpoint_box = ttk.Combobox(controls, textvariable=self.endpoint_var, state="readonly", width=58)
        self.endpoint_box.grid(row=0, column=1, sticky="ew", padx=6)
        self.endpoint_box.bind("<<ComboboxSelected>>", lambda _event: self.load_selected_endpoint())

        ttk.Button(controls, text="Load Sample", command=self.load_selected_endpoint).grid(row=0, column=2, padx=3)
        ttk.Button(controls, text="Send Request", command=self.send_request).grid(row=0, column=3, padx=3)
        ttk.Button(controls, text="GET Location", command=self.get_last_location_once).grid(row=0, column=4, padx=3)
        ttk.Button(controls, text="Poll Location", command=self.poll_last_location).grid(row=0, column=5, padx=3)
        ttk.Checkbutton(controls, text="Auto poll 202", variable=self.auto_poll_var).grid(row=0, column=6, padx=3)
        controls.columnconfigure(1, weight=1)

        self.summary_var = tk.StringVar(value="")
        ttk.Label(controls, textvariable=self.summary_var, wraplength=980, foreground="#444").grid(
            row=1, column=0, columnspan=7, sticky="ew", pady=(6, 0)
        )

        details = ttk.Frame(self, padding=(8, 0, 8, 4))
        details.pack(fill=tk.X)
        self.method_var = tk.StringVar()
        self.path_var = tk.StringVar()
        self.content_type_var = tk.StringVar()
        ttk.Label(details, text="Method").grid(row=0, column=0, sticky="w")
        ttk.Entry(details, textvariable=self.method_var, width=8, state="readonly").grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(details, text="Path").grid(row=0, column=2, sticky="w", padx=(12, 0))
        ttk.Entry(details, textvariable=self.path_var, state="readonly").grid(row=0, column=3, sticky="ew", padx=4)
        ttk.Label(details, text="Content").grid(row=0, column=4, sticky="w", padx=(12, 0))
        ttk.Entry(details, textvariable=self.content_type_var, width=24, state="readonly").grid(
            row=0, column=5, sticky="w", padx=4
        )
        details.columnconfigure(3, weight=1)

        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        request_frame = ttk.Frame(main)
        response_frame = ttk.Frame(main)
        main.add(request_frame, weight=3)
        main.add(response_frame, weight=2)

        params_frame = ttk.Frame(request_frame)
        params_frame.pack(fill=tk.X)

        path_frame = ttk.LabelFrame(params_frame, text="Path Params (key=value)")
        path_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))
        self.path_params_text = scrolledtext.ScrolledText(path_frame, height=4, wrap=tk.NONE)
        self.path_params_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        query_frame = ttk.LabelFrame(params_frame, text="Query Params (key=value, blank values skipped)")
        query_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0))
        self.query_params_text = scrolledtext.ScrolledText(query_frame, height=4, wrap=tk.NONE)
        self.query_params_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        body_frame = ttk.LabelFrame(request_frame, text="Request Body")
        body_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.request_text = scrolledtext.ScrolledText(body_frame, wrap=tk.NONE)
        self.request_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        location_frame = ttk.Frame(response_frame)
        location_frame.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(location_frame, text="Last Location").pack(side=tk.LEFT)
        self.location_var = tk.StringVar()
        ttk.Entry(location_frame, textvariable=self.location_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(location_frame, text="Show Map", command=self.show_latest_response_on_map).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(location_frame, text="Clear Log", command=self.clear_log).pack(side=tk.LEFT)

        log_frame = ttk.LabelFrame(response_frame, text="Response / Log")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.NONE)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    def set_endpoints(self, endpoints: List[EndpointSpec]) -> None:
        self.endpoints = endpoints
        self.endpoint_by_label = {endpoint.label: endpoint for endpoint in endpoints}
        values = list(self.endpoint_by_label.keys())
        self.endpoint_box["values"] = values
        if values:
            current = self.endpoint_var.get()
            self.endpoint_var.set(current if current in self.endpoint_by_label else values[0])
            self.load_selected_endpoint()

    def selected_endpoint(self) -> EndpointSpec:
        label = self.endpoint_var.get()
        endpoint = self.endpoint_by_label.get(label)
        if endpoint is None:
            raise RuntimeError("No endpoint is selected.")
        return endpoint

    def load_selected_endpoint(self) -> None:
        try:
            endpoint = self.selected_endpoint()
        except Exception:
            return
        self.summary_var.set(endpoint.display_summary)
        self.method_var.set(endpoint.method)
        self.path_var.set(endpoint.path)
        self.content_type_var.set(endpoint.content_type or "none")
        self.path_params_text.delete("1.0", tk.END)
        self.path_params_text.insert("1.0", format_key_values(endpoint.path_params))
        self.query_params_text.delete("1.0", tk.END)
        self.query_params_text.insert("1.0", format_key_values(endpoint.query_params))
        self.request_text.delete("1.0", tk.END)

        if endpoint.example is not None:
            self.request_text.insert("1.0", json_preview(endpoint.example))
        elif endpoint.content_type == "application/json":
            self.request_text.insert("1.0", "{}")
        elif endpoint.content_type in {"multipart/form-data", "application/x-www-form-urlencoded"}:
            self.request_text.insert("1.0", "")

    def clear_log(self) -> None:
        self.log_text.delete("1.0", tk.END)

    def _root_app(self) -> "NapaApiGui":
        return self.winfo_toplevel()  # type: ignore[return-value]

    def _build_request(self) -> Dict[str, Any]:
        endpoint = self.selected_endpoint()
        root = self._root_app()
        base_url = root.base_url_var.get()
        api_key = root.api_key_var.get().strip()
        timeout = root.timeout_seconds()
        path_params = parse_key_values(self.path_params_text.get("1.0", tk.END), skip_empty=False)
        query_params = parse_key_values(self.query_params_text.get("1.0", tk.END), skip_empty=True)
        url = build_url(base_url, endpoint.path, path_params)
        headers = {"Accept": "application/json", "User-Agent": "napa-api-gui-test/1.0"}
        if api_key:
            headers["x-api-key"] = api_key

        kwargs: Dict[str, Any] = {
            "method": endpoint.method,
            "url": url,
            "headers": headers,
            "params": query_params,
            "timeout": timeout,
            "_base_url": base_url,
            "_auto_poll": bool(self.auto_poll_var.get()),
        }

        raw_body = self.request_text.get("1.0", tk.END).strip()
        if endpoint.method in {"POST", "PUT", "PATCH", "DELETE"} and raw_body:
            if endpoint.content_type == "application/json":
                kwargs["json"] = json.loads(raw_body)
            elif endpoint.content_type in {"multipart/form-data", "application/x-www-form-urlencoded"}:
                kwargs["data"] = parse_key_values(raw_body, skip_empty=True)
            else:
                kwargs["data"] = raw_body
                if endpoint.content_type:
                    headers["Content-Type"] = endpoint.content_type
        return kwargs

    def send_request(self) -> None:
        try:
            kwargs = self._build_request()
        except Exception as exc:
            messagebox.showerror("Request Error", str(exc))
            return

        self._root_app().last_api_tab = self
        self.log(f"{kwargs['method']} {kwargs['url']}")
        if kwargs.get("params"):
            self.log(f"Query: {json.dumps(kwargs['params'])}")
        threading.Thread(target=self._send_worker, args=(kwargs,), daemon=True).start()

    def _send_worker(self, kwargs: Dict[str, Any]) -> None:
        base_url = kwargs.pop("_base_url")
        auto_poll = bool(kwargs.pop("_auto_poll", False))
        started = time.time()
        try:
            response = require_requests().request(**kwargs)
            elapsed = time.time() - started
            self._handle_response(response, elapsed, base_url, auto_poll=auto_poll)
        except Exception as exc:
            self.log(f"ERROR: {exc}")

    def _handle_response(
        self,
        response: requests.Response,
        elapsed: float,
        base_url: str,
        auto_poll: bool = False,
    ) -> None:
        location = response.headers.get("Location", "")
        if location:
            location = absolute_location(base_url, location)
        parsed, body_text = _response_body_text(response)

        def store() -> None:
            self.latest_response_data = parsed
            self.latest_response_text = body_text
            if location:
                self.latest_location = location
                self.location_var.set(location)

        self.after(0, store)

        self.log(f"HTTP {response.status_code} in {elapsed:.2f}s")
        if location:
            self.log(f"Location: {location}")
        interesting_headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower()
            in {
                "content-type",
                "retry-after",
                "x-ratelimit-limit",
                "x-ratelimit-remaining",
                "x-ratelimit-reset",
            }
        }
        if interesting_headers:
            self.log(f"Headers: {json.dumps(interesting_headers, indent=2)}")
        if body_text:
            self.log(body_text)
        else:
            self.log("(empty response body)")
        if response.status_code == 202 and location:
            if auto_poll:
                self.log("202 Accepted with Location. Polling result automatically...")
                self.after(0, lambda: self._request_location(poll=True, location_override=location, quiet=True))
            else:
                self.log("202 Accepted. Click 'Poll Location' to fetch the voyage result.")
        elif parsed is not None and self._is_async_done(parsed):
            self.after(0, lambda: self.show_latest_response_on_map(auto=True))

    def get_last_location_once(self) -> None:
        self._request_location(poll=False)

    def poll_last_location(self) -> None:
        self._request_location(poll=True)

    def _request_location(self, poll: bool, location_override: str = "", quiet: bool = False) -> None:
        location = location_override.strip() or self.location_var.get().strip() or self.latest_location
        if not location:
            messagebox.showinfo("Location", "No Location header has been captured yet.")
            return
        root = self._root_app()
        api_key = root.api_key_var.get().strip()
        headers = {"Accept": "application/json", "User-Agent": "napa-api-gui-test/1.0"}
        if api_key:
            headers["x-api-key"] = api_key
        kwargs = {
            "method": "GET",
            "url": location,
            "headers": headers,
            "timeout": root.timeout_seconds(),
            "_base_url": root.base_url_var.get(),
        }
        if not quiet:
            self.log(("Polling " if poll else "GET ") + location)
        threading.Thread(target=self._location_worker, args=(kwargs, poll), daemon=True).start()

    def _location_worker(self, kwargs: Dict[str, Any], poll: bool) -> None:
        base_url = kwargs.pop("_base_url")
        max_attempts = POLL_MAX_ATTEMPTS if poll else 1
        for attempt in range(1, max_attempts + 1):
            started = time.time()
            try:
                response = require_requests().request(**kwargs)
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                return
            elapsed = time.time() - started
            self.log(f"Location attempt {attempt}/{max_attempts}")
            self._handle_response(response, elapsed, base_url)
            parsed, _body_text = _response_body_text(response)
            if not poll or response.status_code >= 400:
                return
            if parsed is not None and self._is_async_done(parsed):
                return
            if parsed is None and response.status_code != 202:
                return
            time.sleep(POLL_INTERVAL_SECONDS)

    def _is_async_done(self, data: Any) -> bool:
        if not isinstance(data, dict):
            return True
        for key in ("status", "state", "resultStatus", "calculationStatus"):
            value = data.get(key)
            if isinstance(value, str):
                normalized = value.strip().lower().replace(" ", "_")
                if normalized in ASYNC_DONE_STATES:
                    return True
                if normalized in ASYNC_WAIT_STATES:
                    return False
        if any(key in data for key in ("data", "result", "voyage", "route", "performanceModelId")):
            return True
        return False

    def show_latest_response_on_map(self, auto: bool = False) -> None:
        data = self.latest_response_data
        if data is None and self.latest_response_text:
            try:
                data = json.loads(self.latest_response_text)
            except Exception:
                data = None
        if data is None:
            if not auto:
                messagebox.showinfo("Map Preview", "No parsed JSON response is available yet.")
            return
        root = self._root_app()
        rendered = root.map_tab.load_json_data(data, "Loaded response JSON.", render=True, show_errors=not auto)
        if rendered:
            root.tabs.select(root.map_tab)
            self.log("Rendered latest response in Map Preview.")


class GlobeCanvas(tk.Canvas):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, background="#07111f", highlightthickness=0)
        self.points: List[Dict[str, Any]] = []
        self.lines: List[List[Dict[str, float]]] = []
        self.routes: List[Dict[str, Any]] = []
        self.yaw = math.radians(-25)
        self.pitch = math.radians(12)
        self.zoom = 1.0
        self._drag_start: Optional[Tuple[int, int, float, float]] = None
        self._cx = 0.0
        self._cy = 0.0
        self._radius = 1.0
        self._earth_texture: Optional[Any] = None
        self._earth_texture_status = "Loading NASA Blue Marble satellite texture..."
        self._earth_texture_loading = False
        self._globe_photo: Optional[Any] = None

        self.bind("<Configure>", lambda _event: self.redraw())
        self.bind("<ButtonPress-1>", self._start_drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Button-4>", lambda _event: self._zoom_by(1.12))
        self.bind("<Button-5>", lambda _event: self._zoom_by(1 / 1.12))
        self._load_earth_texture_async()

    def render_map_data(
        self,
        points: List[Dict[str, Any]],
        lines: List[List[Dict[str, float]]],
        routes: List[Dict[str, Any]],
    ) -> None:
        self.points = list(points)
        self.lines = list(lines)
        self.routes = list(routes)
        self.redraw()

    def clear_globe(self) -> None:
        self.points = []
        self.lines = []
        self.routes = []
        self.redraw()

    def reset_view(self) -> None:
        self.yaw = math.radians(-25)
        self.pitch = math.radians(12)
        self.zoom = 1.0
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 1)
        height = max(self.winfo_height(), 1)
        self._cx = width / 2
        self._cy = height / 2
        self._radius = max(80.0, min(420.0, min(width, height) * 0.43 * self.zoom))

        self.create_rectangle(0, 0, width, height, fill="#07111f", outline="")
        self._draw_satellite_globe()
        self._draw_lines()
        self._draw_routes()
        self._draw_points()
        self._draw_overlay(width, height)

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_start = (int(event.x), int(event.y), self.yaw, self.pitch)

    def _drag(self, event: tk.Event) -> None:
        if self._drag_start is None:
            return
        start_x, start_y, start_yaw, start_pitch = self._drag_start
        self.yaw = start_yaw + (int(event.x) - start_x) * 0.01
        self.pitch = max(math.radians(-75), min(math.radians(75), start_pitch + (int(event.y) - start_y) * 0.01))
        self.redraw()

    def _wheel(self, event: tk.Event) -> None:
        self._zoom_by(1.12 if int(event.delta) > 0 else 1 / 1.12)

    def _zoom_by(self, factor: float) -> None:
        self.zoom = max(0.55, min(2.4, self.zoom * factor))
        self.redraw()

    def _draw_satellite_globe(self) -> None:
        if Image is None or ImageTk is None:
            self._draw_texture_fallback("Install Pillow to render the satellite globe.")
            return
        if self._earth_texture is None:
            self._draw_texture_fallback(self._earth_texture_status)
            return

        diameter = max(120, int(self._radius * 2))
        globe = self._render_textured_globe(diameter)
        self._globe_photo = ImageTk.PhotoImage(globe)
        self.create_image(self._cx, self._cy, image=self._globe_photo)
        self.create_oval(
            self._cx - self._radius,
            self._cy - self._radius,
            self._cx + self._radius,
            self._cy + self._radius,
            outline="#93c5fd",
            width=1,
        )

    def _draw_texture_fallback(self, message: str) -> None:
        self.create_oval(
            self._cx - self._radius,
            self._cy - self._radius,
            self._cx + self._radius,
            self._cy + self._radius,
            fill="#0f2742",
            outline="#38bdf8",
            width=2,
        )
        self.create_text(
            self._cx,
            self._cy,
            text=message,
            fill="#cbd5e1",
            font=("Segoe UI", 9),
            width=max(180, int(self._radius * 1.5)),
        )

    def _render_textured_globe(self, diameter: int) -> Any:
        texture = self._earth_texture
        radius = diameter / 2
        image = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
        pixels = image.load()
        texture_pixels = texture.load()
        tex_w, tex_h = texture.size
        cos_yaw = math.cos(self.yaw)
        sin_yaw = math.sin(self.yaw)
        cos_pitch = math.cos(self.pitch)
        sin_pitch = math.sin(self.pitch)

        for py in range(diameter):
            ny = (radius - py - 0.5) / radius
            for px in range(diameter):
                nx = (px + 0.5 - radius) / radius
                distance_sq = nx * nx + ny * ny
                if distance_sq > 1:
                    continue
                nz = math.sqrt(max(0.0, 1.0 - distance_sq))

                world_y = ny * cos_pitch + nz * sin_pitch
                z1 = -ny * sin_pitch + nz * cos_pitch
                world_x = nx * cos_yaw - z1 * sin_yaw
                world_z = nx * sin_yaw + z1 * cos_yaw

                lat = math.asin(max(-1.0, min(1.0, world_y)))
                lng = math.atan2(world_x, world_z)
                tx = int(((lng + math.pi) / (2 * math.pi)) * tex_w) % tex_w
                ty = max(0, min(tex_h - 1, int(((math.pi / 2 - lat) / math.pi) * tex_h)))
                red, green, blue = texture_pixels[tx, ty][:3]
                shade = 0.58 + 0.42 * nz
                pixels[px, py] = (int(red * shade), int(green * shade), int(blue * shade), 255)
        return image

    def _load_earth_texture_async(self) -> None:
        if Image is None or self._earth_texture_loading:
            return
        self._earth_texture_loading = True

        def worker() -> None:
            try:
                texture = self._load_earth_texture()
                self.after(0, lambda: self._set_earth_texture(texture, "NASA Blue Marble satellite texture loaded."))
            except Exception as exc:
                message = str(exc)
                self.after(0, lambda: self._set_earth_texture_error(message))

        threading.Thread(target=worker, daemon=True).start()

    def _load_earth_texture(self) -> Any:
        cache_path = self._earth_texture_cache_path()
        if cache_path.exists():
            return Image.open(cache_path).convert("RGB")

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        response = require_requests().get(EARTH_TEXTURE_URL, timeout=60)
        response.raise_for_status()
        texture = Image.open(BytesIO(response.content)).convert("RGB")
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        texture = texture.resize((EARTH_TEXTURE_WIDTH, EARTH_TEXTURE_HEIGHT), resample)
        texture.save(cache_path, "JPEG", quality=90)
        return texture

    def _earth_texture_cache_path(self) -> Path:
        base = Path(os.getenv("LOCALAPPDATA") or Path.home())
        return base / "NAPAApiGui" / EARTH_TEXTURE_CACHE_NAME

    def _set_earth_texture(self, texture: Any, status: str) -> None:
        self._earth_texture = texture
        self._earth_texture_status = status
        self._earth_texture_loading = False
        self.redraw()

    def _set_earth_texture_error(self, message: str) -> None:
        self._earth_texture_status = f"Satellite texture unavailable: {message}"
        self._earth_texture_loading = False
        self.redraw()

    def _draw_lines(self) -> None:
        for line in self.lines:
            self._draw_geo_path(line, "#00d4ff", 2)

    def _draw_routes(self) -> None:
        for route_index, route in enumerate(self.routes, start=1):
            route_points = route.get("points", [])
            if len(route_points) < 2:
                continue
            has_speed = any(isinstance(point.get("speed"), (int, float)) for point in route_points)
            if has_speed:
                for start, end in zip(route_points, route_points[1:]):
                    speed = end.get("speed") if isinstance(end.get("speed"), (int, float)) else start.get("speed")
                    self._draw_geo_path([start, end], self._speed_color(speed), 3)
            else:
                self._draw_geo_path(route_points, "#00d4ff", 3)
            self._draw_route_nodes(route_index, route_points)

    def _draw_points(self) -> None:
        label_limit = 80
        for index, point in enumerate(self.points, start=1):
            label = str(point.get("label") or f"Point {index}")
            self._draw_marker(point, "#38bdf8", 5, label if index <= label_limit else "")

    def _draw_route_nodes(self, route_index: int, route_points: List[Dict[str, Any]]) -> None:
        max_nodes = 260
        step = max(1, len(route_points) // max_nodes)
        last_index = len(route_points) - 1
        for index, point in enumerate(route_points):
            endpoint = index in {0, last_index}
            if not endpoint and index % step != 0:
                continue
            if index == 0:
                label = f"R{route_index} START"
                color = "#f8fafc"
                radius = 6
            elif index == last_index:
                label = f"R{route_index} END"
                color = "#f8fafc"
                radius = 6
            else:
                label = f"WP {index + 1}" if len(route_points) <= 80 else ""
                color = "#94a3b8"
                radius = 3
            self._draw_marker(point, color, radius, label)

    def _draw_geo_path(
        self,
        path: List[Dict[str, Any]],
        color: str,
        width: int,
        samples_per_segment: Optional[int] = None,
        close_path: bool = False,
    ) -> None:
        path = self._prepared_path(path, close_path)
        if len(path) < 2:
            return
        if samples_per_segment is None:
            samples_per_segment = 24 if len(path) <= 10 else 1

        for start, end in zip(path, path[1:]):
            start_vec = self._latlon_to_vector(float(start["lat"]), self._point_lon(start))
            end_vec = self._latlon_to_vector(float(end["lat"]), self._point_lon(end))
            last_xy: Optional[Tuple[float, float]] = None
            for index in range(samples_per_segment + 1):
                point_vec = self._slerp(start_vec, end_vec, index / samples_per_segment)
                xy = self._project_vector(point_vec)
                if xy is None:
                    last_xy = None
                    continue
                if last_xy is not None:
                    self.create_line(last_xy[0], last_xy[1], xy[0], xy[1], fill=color, width=width, smooth=True)
                last_xy = xy

    def _prepared_path(self, path: List[Dict[str, Any]], close_path: bool) -> List[Dict[str, Any]]:
        prepared: List[Dict[str, Any]] = []
        for point in path:
            if prepared and self._same_geo_point(prepared[-1], point):
                continue
            prepared.append(point)
        if len(prepared) > 1 and self._same_geo_point(prepared[0], prepared[-1]):
            prepared = prepared[:-1]
        if close_path and len(prepared) > 2:
            prepared = prepared + [prepared[0]]
        return prepared

    def _same_geo_point(self, first: Dict[str, Any], second: Dict[str, Any]) -> bool:
        lat_delta = abs(float(first["lat"]) - float(second["lat"]))
        lon_delta = abs(((self._point_lon(first) - self._point_lon(second) + 180) % 360) - 180)
        return lat_delta < 1e-7 and lon_delta < 1e-7

    def _draw_marker(self, point: Dict[str, Any], color: str, radius: int, label: str = "") -> None:
        xy = self._project_latlon(float(point["lat"]), self._point_lon(point))
        if xy is None:
            return
        x, y = xy
        self.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color, outline="#0f172a", width=1)
        if label:
            self.create_text(
                x + radius + 4,
                y - radius - 2,
                anchor="w",
                text=label[:36],
                fill="#e2e8f0",
                font=("Segoe UI", 8),
            )

    def _draw_overlay(self, width: int, height: int) -> None:
        route_nodes = sum(len(route.get("points", [])) for route in self.routes)
        summary = (
            f"3D Satellite Globe  Points {len(self.points)}  Lines {len(self.lines)}  "
            f"Routes {len(self.routes)}  Nodes {route_nodes}"
        )
        self.create_text(14, 12, anchor="nw", text=summary, fill="#e2e8f0", font=("Segoe UI", 9, "bold"))
        legend = [
            ("< 8 kn", "#2563eb"),
            ("8-12 kn", "#16a34a"),
            ("12-16 kn", "#f59e0b"),
            (">= 16 kn", "#dc2626"),
            ("no speed", "#00d4ff"),
        ]
        x = 14
        y = max(42, height - 22)
        for label, color in legend:
            self.create_line(x, y, x + 22, y, fill=color, width=4)
            self.create_text(x + 28, y, anchor="w", text=label, fill="#cbd5e1", font=("Segoe UI", 8))
            x += 86

    def _project_latlon(self, lat: float, lon: float) -> Optional[Tuple[float, float]]:
        return self._project_vector(self._latlon_to_vector(lat, lon))

    def _project_vector(self, vector: Tuple[float, float, float]) -> Optional[Tuple[float, float]]:
        x, y, z = vector
        cos_yaw = math.cos(self.yaw)
        sin_yaw = math.sin(self.yaw)
        x1 = x * cos_yaw + z * sin_yaw
        z1 = -x * sin_yaw + z * cos_yaw

        cos_pitch = math.cos(self.pitch)
        sin_pitch = math.sin(self.pitch)
        y2 = y * cos_pitch - z1 * sin_pitch
        z2 = y * sin_pitch + z1 * cos_pitch
        if z2 <= 0:
            return None
        return self._cx + self._radius * x1, self._cy - self._radius * y2

    def _latlon_to_vector(self, lat: float, lon: float) -> Tuple[float, float, float]:
        lat_rad = math.radians(lat)
        lon_rad = math.radians(lon)
        cos_lat = math.cos(lat_rad)
        return (cos_lat * math.sin(lon_rad), math.sin(lat_rad), cos_lat * math.cos(lon_rad))

    def _slerp(
        self,
        start: Tuple[float, float, float],
        end: Tuple[float, float, float],
        fraction: float,
    ) -> Tuple[float, float, float]:
        dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(start, end))))
        if dot > 0.9995 or dot < -0.9995:
            return self._normalize(tuple(start[i] + (end[i] - start[i]) * fraction for i in range(3)))
        omega = math.acos(dot)
        sin_omega = math.sin(omega)
        if abs(sin_omega) < 1e-8:
            return start
        start_scale = math.sin((1 - fraction) * omega) / sin_omega
        end_scale = math.sin(fraction * omega) / sin_omega
        return tuple(start[i] * start_scale + end[i] * end_scale for i in range(3))

    def _normalize(self, vector: Tuple[float, float, float]) -> Tuple[float, float, float]:
        length = math.sqrt(sum(value * value for value in vector))
        if length <= 1e-9:
            return (0.0, 0.0, 1.0)
        return tuple(value / length for value in vector)

    def _point_lon(self, point: Dict[str, Any]) -> float:
        value = point.get("lon", point.get("lng"))
        return float(value)

    def _speed_color(self, speed: Any) -> str:
        if not isinstance(speed, (int, float)):
            return "#00d4ff"
        if speed < 8:
            return "#2563eb"
        if speed < 12:
            return "#16a34a"
        if speed < 16:
            return "#f59e0b"
        return "#dc2626"


class MapPreviewFrame(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.source_data: Any = None
        self._build_widgets()

    def _build_widgets(self) -> None:
        controls = ttk.Frame(self, padding=(8, 8, 8, 4))
        controls.pack(fill=tk.X)
        ttk.Button(controls, text="Load Active Request", command=self.load_active_request).pack(side=tk.LEFT)
        ttk.Button(controls, text="Load Active Response", command=self.load_active_response).pack(side=tk.LEFT, padx=5)
        ttk.Button(controls, text="Render Map", command=self.render_map).pack(side=tk.LEFT, padx=5)
        ttk.Button(controls, text="Clear", command=self.clear).pack(side=tk.LEFT, padx=5)

        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        source_frame = ttk.LabelFrame(main, text="Source JSON")
        map_frame = ttk.Frame(main)
        main.add(source_frame, weight=2)
        main.add(map_frame, weight=3)

        self.source_text = scrolledtext.ScrolledText(source_frame, wrap=tk.NONE)
        self.source_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        canvas_frame = ttk.LabelFrame(map_frame, text="3D Globe")
        canvas_frame.pack(fill=tk.BOTH, expand=True)
        self.globe_widget = GlobeCanvas(canvas_frame)
        self.globe_widget.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.result_text = scrolledtext.ScrolledText(map_frame, height=8, wrap=tk.NONE)
        self.result_text.pack(fill=tk.X, pady=(8, 0))

    def _root_app(self) -> "NapaApiGui":
        return self.winfo_toplevel()  # type: ignore[return-value]

    def load_active_request(self) -> None:
        frame = self._root_app().last_api_tab
        if frame is None:
            messagebox.showinfo("Map Preview", "No active API tab is available.")
            return
        raw = frame.request_text.get("1.0", tk.END).strip()
        self.source_data = None
        self._set_source(raw, "Loaded active request JSON.")

    def load_active_response(self) -> None:
        frame = self._root_app().last_api_tab
        if frame is None:
            messagebox.showinfo("Map Preview", "No active API tab is available.")
            return
        data = frame.latest_response_data
        if data is None and frame.latest_response_text:
            try:
                data = json.loads(frame.latest_response_text)
            except Exception:
                data = None
        if data is None:
            messagebox.showinfo("Map Preview", "No parsed JSON response is available yet.")
            return
        self.load_json_data(data, "Loaded active response JSON.", render=False)

    def load_json_data(
        self,
        data: Any,
        message: str,
        render: bool = False,
        show_errors: bool = True,
    ) -> bool:
        self.source_data = data
        self._set_source(json_preview(data), message)
        if render:
            return self.render_map(show_errors=show_errors)
        return True

    def clear(self) -> None:
        self.source_data = None
        self.source_text.delete("1.0", tk.END)
        self.result_text.delete("1.0", tk.END)
        self.globe_widget.clear_globe()

    def render_map(self, show_errors: bool = True) -> bool:
        try:
            data = self.source_data if self.source_data is not None else json.loads(self.source_text.get("1.0", tk.END))
            points, lines, routes = self._extract_map_data(data)
            if not points and not lines and not routes:
                raise ValueError("No latitude/longitude or GeoJSON coordinates were found.")
            self.globe_widget.render_map_data(points, lines, routes)
            self._log(f"Points: {len(points)}")
            self._log(f"Lines: {len(lines)}")
            route_nodes = sum(len(route.get("points", [])) for route in routes)
            self._log(f"Routes: {len(routes)} / route nodes: {route_nodes}")
            for route_index, route in enumerate(routes, start=1):
                self._log_speed_profile(route_index, str(route.get("label") or "Route"), route.get("points", []))
            return True
        except Exception as exc:
            self._log(f"ERROR: {exc}")
            if show_errors:
                messagebox.showerror("Map Preview Error", str(exc))
            return False

    def _set_source(self, raw: str, message: str) -> None:
        self.source_text.delete("1.0", tk.END)
        self.source_text.insert("1.0", raw)
        self._log(message)

    def _log(self, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.result_text.insert(tk.END, f"[{ts}] {message}\n")
        self.result_text.see(tk.END)

    def _extract_map_data(self, data: Any) -> Tuple[List[Dict[str, Any]], List[List[Dict[str, float]]], List[Dict[str, Any]]]:
        points: List[Dict[str, Any]] = []
        lines: List[List[Dict[str, float]]] = []
        routes: List[Dict[str, Any]] = []
        seen_points = set()
        seen_lines = set()
        seen_routes = set()

        def number(value: Any) -> Optional[float]:
            if isinstance(value, bool):
                return None
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    return None
            return None

        def named_point(obj: Dict[str, Any]) -> Optional[Dict[str, float]]:
            lat = number(obj.get("latitude", obj.get("lat")))
            lon = number(obj.get("longitude", obj.get("lon", obj.get("lng"))))
            if lat is None or lon is None:
                return None
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return {"lat": lat, "lon": lon}
            return None

        def numeric_from_obj(obj: Any, keys: Tuple[str, ...]) -> Optional[float]:
            if not isinstance(obj, dict):
                return None
            for key in keys:
                value = number(obj.get(key))
                if value is not None:
                    return value
            properties = obj.get("properties")
            if isinstance(properties, dict):
                nested = numeric_from_obj(properties, keys)
                if nested is not None:
                    return nested
            operation_method = obj.get("operationMethod")
            if isinstance(operation_method, dict):
                nested = numeric_from_obj(operation_method, keys)
                if nested is not None:
                    return nested
            return None

        def point_label(obj: Any, fallback: str) -> str:
            if not isinstance(obj, dict):
                return fallback
            properties = obj.get("properties")
            if isinstance(properties, dict):
                for key in ("name", "label", "port"):
                    if properties.get(key):
                        return str(properties[key])
            for key in ("name", "label", "id"):
                if obj.get(key):
                    return str(obj[key])
            return fallback

        def coord_pair(value: Any) -> Optional[Dict[str, float]]:
            if not isinstance(value, list) or len(value) < 2:
                return None
            first = number(value[0])
            second = number(value[1])
            if first is None or second is None:
                return None
            if -180 <= first <= 180 and -90 <= second <= 90:
                return {"lat": second, "lon": first}
            if -90 <= first <= 90 and -180 <= second <= 180:
                return {"lat": first, "lon": second}
            return None

        def add_point(point: Dict[str, float], label: str = "Point") -> None:
            key = (round(point["lat"], 7), round(point["lon"], 7), label)
            if key in seen_points:
                return
            seen_points.add(key)
            points.append({**point, "label": label})

        def add_line(items: Iterable[Any]) -> bool:
            line = []
            for item in items:
                point = named_point(item) if isinstance(item, dict) else coord_pair(item)
                if point:
                    line.append(point)
            if len(line) < 2:
                return False
            key = tuple((round(point["lat"], 7), round(point["lon"], 7)) for point in line)
            if key in seen_lines:
                return True
            seen_lines.add(key)
            lines.append(line)
            for index, point in enumerate(line, start=1):
                if len(line) <= 20:
                    add_point(point, f"WP {index}")
            return True

        def add_route(items: Any, label: str = "Route") -> bool:
            if not isinstance(items, list):
                return False
            route_points: List[Dict[str, Any]] = []
            for index, item in enumerate(items, start=1):
                point = named_point(item) if isinstance(item, dict) else coord_pair(item)
                if not point:
                    continue
                route_point: Dict[str, Any] = {
                    **point,
                    "label": point_label(item, f"WP {index}"),
                }
                speed = numeric_from_obj(item, ("speed", "speedOverGround", "speedKnots", "sog", "plannedSpeed"))
                if speed is not None:
                    route_point["speed"] = speed
                route_points.append(route_point)
            if len(route_points) < 2:
                return False
            key = tuple((round(point["lat"], 7), round(point["lon"], 7)) for point in route_points)
            if key in seen_routes:
                return True
            seen_routes.add(key)
            routes.append({"label": label, "points": route_points})
            if len(route_points) <= 10:
                for route_point in route_points:
                    add_point({"lat": route_point["lat"], "lon": route_point["lon"]}, str(route_point.get("label") or label))
            return True

        def visit(obj: Any, label: str = "Point") -> None:
            if isinstance(obj, dict):
                if isinstance(obj.get("fromCoordinates"), dict) and isinstance(obj.get("toCoordinates"), dict):
                    add_route([obj["fromCoordinates"], obj["toCoordinates"]], str(obj.get("name") or obj.get("id") or "Route"))

                point = named_point(obj)
                if point:
                    add_point(point, str(obj.get("name") or obj.get("label") or label))

                geometry = obj.get("geometry") if isinstance(obj.get("geometry"), dict) else obj
                geometry_type = geometry.get("type") if isinstance(geometry, dict) else None
                coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
                if geometry_type == "Point":
                    point = coord_pair(coords)
                    if point:
                        props = obj.get("properties") if isinstance(obj.get("properties"), dict) else {}
                        add_point(point, str(props.get("name") or label))
                elif geometry_type == "LineString" and isinstance(coords, list):
                    add_line(coords)
                elif geometry_type in {"MultiLineString", "Polygon"} and isinstance(coords, list):
                    for item in coords:
                        if isinstance(item, list):
                            add_line(item)
                elif geometry_type == "MultiPolygon" and isinstance(coords, list):
                    for polygon in coords:
                        if isinstance(polygon, list):
                            for item in polygon:
                                if isinstance(item, list):
                                    add_line(item)

                for key, value in obj.items():
                    if key in {"coordinates", "waypoints", "route", "points"} and add_route(value, str(obj.get("name") or obj.get("id") or key)):
                        continue
                    if key in {"geometry", "coordinates"}:
                        continue
                    visit(value, str(key))
            elif isinstance(obj, list):
                if add_route(obj, label):
                    return
                if not add_line(obj):
                    point = coord_pair(obj)
                    if point:
                        add_point(point, label)
                    for item in obj:
                        visit(item, label)

        visit(data)
        return points, lines, routes

    def _log_speed_profile(self, route_index: int, label: str, route_points: List[Dict[str, Any]]) -> None:
        speeds = [float(point["speed"]) for point in route_points if isinstance(point.get("speed"), (int, float))]
        if not speeds:
            self._log(f"Route {route_index} ({label}) speed profile: no speed values found.")
            return
        average = round(sum(speeds) / len(speeds), 2)
        self._log(
            f"Route {route_index} ({label}) speed profile: min {min(speeds):.2f} kn, "
            f"avg {average:.2f} kn, max {max(speeds):.2f} kn, samples {len(speeds)}"
        )


class NapaApiGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("NAPA Voyage Optimization API GUI Tester")
        self.geometry("1280x860")
        self.api_frames: Dict[str, NapaApiFrame] = {}
        self.last_api_tab: Optional[NapaApiFrame] = None

        self.base_url_var = tk.StringVar(
            value=os.getenv("NAPA_BASE_URL", LOCAL_DEFAULTS.get("base_url", DEFAULT_BASE_URL))
        )
        self.swagger_url_var = tk.StringVar(
            value=os.getenv("NAPA_SWAGGER_URL", LOCAL_DEFAULTS.get("swagger_url", DEFAULT_SWAGGER_URL))
        )
        self.api_key_var = tk.StringVar(value=os.getenv("NAPA_API_KEY", LOCAL_DEFAULTS.get("api_key", "")))
        self.timeout_var = tk.StringVar(
            value=os.getenv("NAPA_TIMEOUT", LOCAL_DEFAULTS.get("timeout", str(DEFAULT_TIMEOUT_SECONDS)))
        )
        self.status_var = tk.StringVar(value="Ready.")

        self._build_widgets()
        self.set_endpoints(fallback_endpoints(), source="built-in fallback")
        self.after(300, self.reload_swagger_async)

    def _build_widgets(self) -> None:
        toolbar = ttk.Frame(self, padding=(10, 8, 10, 4))
        toolbar.pack(fill=tk.X)

        ttk.Label(toolbar, text="Base URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(toolbar, textvariable=self.base_url_var).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Label(toolbar, text="API Key").grid(row=0, column=2, sticky="w", padx=(10, 0))
        ttk.Entry(toolbar, textvariable=self.api_key_var, show="*").grid(row=0, column=3, sticky="ew", padx=5)
        ttk.Label(toolbar, text="Timeout").grid(row=0, column=4, sticky="w", padx=(10, 0))
        ttk.Entry(toolbar, textvariable=self.timeout_var, width=6).grid(row=0, column=5, sticky="w", padx=5)

        ttk.Label(toolbar, text="Swagger").grid(row=1, column=0, sticky="w", pady=(5, 0))
        ttk.Entry(toolbar, textvariable=self.swagger_url_var).grid(row=1, column=1, columnspan=3, sticky="ew", padx=5, pady=(5, 0))
        ttk.Button(toolbar, text="Reload Swagger", command=self.reload_swagger_async).grid(
            row=1, column=4, columnspan=2, sticky="ew", padx=(10, 0), pady=(5, 0)
        )
        toolbar.columnconfigure(1, weight=3)
        toolbar.columnconfigure(3, weight=2)

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill=tk.BOTH, expand=True)
        self.tabs.bind("<<NotebookTabChanged>>", self._remember_active_api_tab)

        status = ttk.Frame(self, padding=(10, 4, 10, 8))
        status.pack(fill=tk.X)
        ttk.Label(status, textvariable=self.status_var).pack(side=tk.LEFT)

    def timeout_seconds(self) -> int:
        try:
            return max(1, int(self.timeout_var.get().strip()))
        except ValueError:
            return DEFAULT_TIMEOUT_SECONDS

    def set_endpoints(self, endpoints: List[EndpointSpec], source: str) -> None:
        grouped: Dict[str, List[EndpointSpec]] = {}
        for endpoint in endpoints:
            grouped.setdefault(endpoint.tag, []).append(endpoint)

        current_tags = set(self.api_frames)
        new_tags = set(grouped)
        for tag in sorted(current_tags - new_tags):
            frame = self.api_frames.pop(tag)
            self.tabs.forget(frame)

        for tag, tag_endpoints in grouped.items():
            frame = self.api_frames.get(tag)
            if frame is None:
                frame = NapaApiFrame(self.tabs, tag, tag_endpoints)
                self.api_frames[tag] = frame
                self.tabs.add(frame, text=tag)
            else:
                frame.set_endpoints(tag_endpoints)

        if not hasattr(self, "map_tab"):
            self.map_tab = MapPreviewFrame(self.tabs)
            self.tabs.add(self.map_tab, text="Map Preview")
        elif str(self.map_tab) not in self.tabs.tabs():
            self.tabs.add(self.map_tab, text="Map Preview")

        if self.last_api_tab is None and self.api_frames:
            self.last_api_tab = next(iter(self.api_frames.values()))

        self.status_var.set(f"Loaded {len(endpoints)} endpoints from {source}.")

    def reload_swagger_async(self) -> None:
        swagger_url = self.swagger_url_var.get().strip() or DEFAULT_SWAGGER_URL
        timeout = self.timeout_seconds()
        self.status_var.set(f"Loading Swagger from {swagger_url} ...")
        threading.Thread(target=self._reload_swagger_worker, args=(swagger_url, timeout), daemon=True).start()

    def _reload_swagger_worker(self, swagger_url: str, timeout: int) -> None:
        try:
            response = require_requests().get(swagger_url, timeout=timeout)
            response.raise_for_status()
            endpoints = parse_swagger(response.json())
            if not endpoints:
                raise RuntimeError("Swagger did not contain any endpoints.")
        except Exception as exc:
            self.after(0, lambda: self.status_var.set(f"Swagger load failed: {exc}. Using current endpoints."))
            return
        self.after(0, lambda: self.set_endpoints(endpoints, source=swagger_url))

    def _remember_active_api_tab(self, _event: tk.Event) -> None:
        selected = self.tabs.nametowidget(self.tabs.select())
        if isinstance(selected, NapaApiFrame):
            self.last_api_tab = selected


def main() -> None:
    app = NapaApiGui()
    app.mainloop()


if __name__ == "__main__":
    main()
