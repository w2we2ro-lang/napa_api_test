"""
NAPA Voyage Optimization API GUI Tester

One Tkinter app for testing the NAPA Voyage Optimization API:
- Swagger-driven endpoint tabs
- x-api-key authentication
- Editable request JSON or form fields
- Response logging and async Location follow-up
- Simple coordinate map preview

Run:
    python napa_api_gui.py
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

try:
    import requests
except ImportError:  # pragma: no cover - exercised only before dependencies are installed.
    requests = None  # type: ignore[assignment]


DEFAULT_BASE_URL = "https://api.fleetintelligence.napa.fi/vo"
DEFAULT_SWAGGER_URL = "https://api.fleetintelligence.napa.fi/vo/v1/swagger.json"
DEFAULT_TIMEOUT_SECONDS = 60

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
ASYNC_DONE_STATES = {"completed", "complete", "done", "finished", "failed", "failure", "error", "ready"}
ASYNC_WAIT_STATES = {"accepted", "queued", "pending", "running", "processing", "inprogress", "in_progress"}

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

    endpoints.sort(key=lambda item: (tag_rank.get(item.tag, 999), item.tag, item.path, item.method))
    return endpoints


def fallback_endpoints() -> List[EndpointSpec]:
    examples: List[EndpointSpec] = [
        EndpointSpec(
            tag="Forecast",
            method="POST",
            path="/v2/interpolate-conditions",
            summary="Interpolates conditions for given times and coordinates.",
            content_type="application/json",
            example={
                "coordinates": [
                    {"latitude": 60.1533167, "longitude": 24.9489667},
                    {"latitude": 58.549169, "longitude": 21.042663},
                    {"latitude": 56.468645, "longitude": 17.524141},
                ],
                "timestamps": [
                    "2018-10-30T00:00:00+00:00",
                    "2018-10-30T11:18:46+00:00",
                    "2018-10-30T23:44:34+00:00",
                ],
            },
        ),
        EndpointSpec(
            tag="Forecast",
            method="POST",
            path="/v1/forecast-metadata/status",
            summary="Get information about latest forecast updates.",
        ),
        EndpointSpec(
            tag="Performance",
            method="POST",
            path="/v1/performance/calculate-for-condition",
            summary="Calculates performance for a given condition.",
            content_type="application/json",
            example={
                "imoNumber": 9629457,
                "draft": 14,
                "operationMethod": {"speedOverGround": 7},
                "courseOverGround": 37,
                "windSpeed": 13.5,
                "windDirection": 266,
                "windWavesSignificantHeight": 3.2,
                "windWavesZeroCrossingPeriod": 6.9,
                "windWavesDirection": 262,
                "swellSignificantHeight": 1,
                "swellZeroCrossingPeriod": 6.3,
                "swellDirection": 199,
                "seaCurrentSpeed": 0.1,
                "seaCurrentDirection": 158,
                "waterDepth": 82,
            },
        ),
        EndpointSpec(
            tag="PerformanceModel",
            method="POST",
            path="/v1/performance-models/create",
            summary="Creates a generic performance model from ship particulars.",
            content_type="application/json",
            example={
                "imoNumber": 9629457,
                "shipType": "Container",
                "lengthOverAll": 250,
                "breadth": 40,
                "designDraft": 10,
                "engineBrakePower": 30000000,
                "serviceSpeed": 10,
            },
        ),
        EndpointSpec(
            tag="PerformanceModel",
            method="GET",
            path="/v1/performance-models/try-get-performance-model",
            summary="Returns performance model calculation status.",
            query_params={"guid": PARAMETER_DEFAULTS["guid"]},
        ),
        EndpointSpec(
            tag="Route",
            method="POST",
            path="/v1/find-shortest-route",
            summary="Returns the shortest route.",
            content_type="application/json",
            example={
                "start": {"latitude": 60.1533167, "longitude": 24.9489667},
                "destination": {"latitude": 53.96666718, "longitude": 10.9},
            },
        ),
        EndpointSpec(
            tag="RouteNetwork",
            method="GET",
            path="/v1/get-version",
            summary="Returns the version of the route network currently in use.",
        ),
        EndpointSpec(
            tag="Voyage",
            method="POST",
            path="/v1/calculate-voyage",
            summary="Returns the voyage for a given route.",
            content_type="application/json",
            example={
                "imoNumber": 9629457,
                "draft": 14,
                "operationMethod": {"speedOverGround": 7},
                "route": [
                    {"latitude": 60.1533167, "longitude": 24.9489667},
                    {"latitude": 53.96666718, "longitude": 10.9},
                ],
                "startTime": "2018-10-30T00:00:00+00:00",
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
        return location
    parsed = urlparse(normalize_base_url(base_url))
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if location.startswith("/"):
        return urljoin(origin, location)
    return urljoin(normalize_base_url(base_url) + "/", location)


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
        controls.columnconfigure(1, weight=1)

        self.summary_var = tk.StringVar(value="")
        ttk.Label(controls, textvariable=self.summary_var, wraplength=980, foreground="#444").grid(
            row=1, column=0, columnspan=6, sticky="ew", pady=(6, 0)
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
        started = time.time()
        try:
            response = require_requests().request(**kwargs)
            elapsed = time.time() - started
            self._handle_response(response, elapsed, base_url)
        except Exception as exc:
            self.log(f"ERROR: {exc}")

    def _handle_response(self, response: requests.Response, elapsed: float, base_url: str) -> None:
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

    def get_last_location_once(self) -> None:
        self._request_location(poll=False)

    def poll_last_location(self) -> None:
        self._request_location(poll=True)

    def _request_location(self, poll: bool) -> None:
        location = self.location_var.get().strip() or self.latest_location
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
        self.log(("Polling " if poll else "GET ") + location)
        threading.Thread(target=self._location_worker, args=(kwargs, poll), daemon=True).start()

    def _location_worker(self, kwargs: Dict[str, Any], poll: bool) -> None:
        base_url = kwargs.pop("_base_url")
        max_attempts = 20 if poll else 1
        interval = 3
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
            if not poll or response.status_code >= 400 or self._is_async_done(parsed):
                return
            time.sleep(interval)

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


class MapCanvas(tk.Canvas):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, background="#eef4fb", highlightthickness=0)
        self.points: List[Dict[str, Any]] = []
        self.lines: List[List[Dict[str, float]]] = []
        self.bind("<Configure>", lambda _event: self.redraw())

    def render(self, points: List[Dict[str, Any]], lines: List[List[Dict[str, float]]]) -> None:
        self.points = points
        self.lines = lines
        self.redraw()

    def clear_map(self) -> None:
        self.points = []
        self.lines = []
        self.redraw()

    def _project(self, lat: float, lon: float) -> Tuple[float, float]:
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        x = (lon + 180.0) / 360.0 * width
        y = (90.0 - lat) / 180.0 * height
        return x, y

    def redraw(self) -> None:
        self.delete("all")
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        self.create_rectangle(0, 0, width, height, fill="#eef4fb", outline="")
        for lon in range(-180, 181, 30):
            x, _ = self._project(0, lon)
            self.create_line(x, 0, x, height, fill="#d4dde8")
        for lat in range(-60, 61, 30):
            _, y = self._project(lat, 0)
            self.create_line(0, y, width, y, fill="#d4dde8")

        for line in self.lines:
            coords: List[float] = []
            for point in line:
                x, y = self._project(point["lat"], point["lon"])
                coords.extend([x, y])
            if len(coords) >= 4:
                self.create_line(*coords, fill="#1d6fd1", width=2, smooth=True)

        for index, point in enumerate(self.points, start=1):
            x, y = self._project(point["lat"], point["lon"])
            self.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#d11d43", outline="#7d1027")
            label = str(point.get("label") or index)
            self.create_text(x + 7, y - 7, text=label[:20], anchor="w", fill="#20242a")


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

        canvas_frame = ttk.LabelFrame(map_frame, text="Coordinate Preview")
        canvas_frame.pack(fill=tk.BOTH, expand=True)
        self.map_canvas = MapCanvas(canvas_frame)
        self.map_canvas.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

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
        self.source_data = data
        self._set_source(json_preview(data), "Loaded active response JSON.")

    def clear(self) -> None:
        self.source_data = None
        self.source_text.delete("1.0", tk.END)
        self.result_text.delete("1.0", tk.END)
        self.map_canvas.clear_map()

    def render_map(self) -> None:
        try:
            data = self.source_data if self.source_data is not None else json.loads(self.source_text.get("1.0", tk.END))
            points, lines = self._extract_map_data(data)
            if not points and not lines:
                raise ValueError("No latitude/longitude or GeoJSON coordinates were found.")
            self.map_canvas.render(points, lines)
            self._log(f"Points: {len(points)}")
            self._log(f"Lines: {len(lines)}")
        except Exception as exc:
            self._log(f"ERROR: {exc}")
            messagebox.showerror("Map Preview Error", str(exc))

    def _set_source(self, raw: str, message: str) -> None:
        self.source_text.delete("1.0", tk.END)
        self.source_text.insert("1.0", raw)
        self._log(message)

    def _log(self, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.result_text.insert(tk.END, f"[{ts}] {message}\n")
        self.result_text.see(tk.END)

    def _extract_map_data(self, data: Any) -> Tuple[List[Dict[str, Any]], List[List[Dict[str, float]]]]:
        points: List[Dict[str, Any]] = []
        lines: List[List[Dict[str, float]]] = []
        seen_points = set()
        seen_lines = set()

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

        def visit(obj: Any, label: str = "Point") -> None:
            if isinstance(obj, dict):
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
                    if key in {"geometry", "coordinates"}:
                        continue
                    visit(value, str(key))
            elif isinstance(obj, list):
                if not add_line(obj):
                    point = coord_pair(obj)
                    if point:
                        add_point(point, label)
                    for item in obj:
                        visit(item, label)

        visit(data)
        return points, lines


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
