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

import csv
import json
import math
import os
import queue
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse
import xml.etree.ElementTree as ET

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
LOCAL_API_KEY_FILES = ("napa_api_key.txt", ".napa_api_key", ".env")
API_KEY_ENV_NAMES = ("NAPA_API_KEY", "API_KEY", "X_API_KEY")
GLOBE_MIN_ZOOM = 0.18
GLOBE_MAX_ZOOM = 10.0
GLOBE_ZOOM_FACTOR = 1.22
BATCH_RETRY_ATTEMPTS = 3
BATCH_RETRY_DELAY_SECONDS = 15
AUTO_PROFILE_REFRESH_MS = 30_000
PROFILE_SERIES_COLORS = [
    "#38bdf8",
    "#f59e0b",
    "#22c55e",
    "#ef4444",
    "#14b8a6",
    "#eab308",
]
BATCH_ENDPOINTS = {
    "Find shortest voyage": "/v1/find-shortest-voyage",
    "Find optimal voyage": "/v1/find-optimal-voyage",
    "Calculate voyage plan": "/v2/calculate-voyage-plan",
}
BATCH_OUTPUT_KINDS = {
    "Find shortest voyage": "FindShortestVoyage",
    "Find optimal voyage": "FindOptimalVoyage",
    "Calculate voyage plan": "CalculateVoyagePlan",
}
CALCULATE_VOYAGE_BATCH_ENDPOINT = "Calculate voyage plan"
CALCULATE_VOYAGE_OPERATION_PROFILE = "OptimalSpeed"
CALCULATE_VOYAGE_MAX_INTERVAL_DISTANCE_METERS = 50 * 1852
DEFAULT_BATCH_ROOT = Path.home() / "Downloads"
DEFAULT_BATCH_OUTPUT_DIR = DEFAULT_BATCH_ROOT / "napa_batch_output"
RTZ_FILE_NAME_RE = re.compile(
    r"^(?P<imo>\d+)_SAS_(?P<kind>.+)_(?P<date>\d{8})_(?P<time>\d{6})\.rtz$",
    re.IGNORECASE,
)

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
ASYNC_DONE_STATES = {"completed", "complete", "done", "finished", "failed", "failure", "error", "ready"}
ASYNC_WAIT_STATES = {"accepted", "queued", "pending", "running", "processing", "inprogress", "in_progress"}

ENABLED_ENDPOINT_PATHS = (
    "/v1/performance-models/create",
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

PROJECT_PERFORMANCE_CREATE_EXAMPLE = {
    "imoNumber": 9935208,
    "shipType": "Container",
    "lengthOverAll": 366,
    "breadth": 51,
    "designDraft": 14,
    "engineBrakePower": 30000000,
    "serviceSpeed": 10,
}
PROJECT_ENDPOINT_EXAMPLE_OVERRIDES = {
    "/v1/performance-models/create": PROJECT_PERFORMANCE_CREATE_EXAMPLE,
}


def _example_for_endpoint(path: str, example: Any) -> Any:
    override = PROJECT_ENDPOINT_EXAMPLE_OVERRIDES.get(path)
    if override is not None:
        return json.loads(json.dumps(override))
    return example


def _load_local_defaults() -> Dict[str, str]:
    path = Path(__file__).with_name("napa_gui_defaults.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {key: str(value) for key, value in data.items() if value is not None}


def _strip_optional_quotes(value: str) -> str:
    value = value.strip().strip(";")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def _read_api_key_file(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except Exception:
        return ""

    fallback = ""
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" in line:
            key, value = line.split("=", 1)
            normalized_key = key.strip().upper().replace("-", "_")
            if normalized_key in API_KEY_ENV_NAMES or normalized_key == "API_KEY":
                return _strip_optional_quotes(value)
            continue
        if not fallback:
            fallback = _strip_optional_quotes(line)
    return fallback


def _load_local_api_key(defaults: Optional[Dict[str, str]] = None) -> str:
    for env_name in API_KEY_ENV_NAMES:
        value = os.getenv(env_name, "").strip()
        if value:
            return value

    for filename in LOCAL_API_KEY_FILES:
        value = _read_api_key_file(Path(__file__).with_name(filename))
        if value:
            return value

    defaults = defaults or {}
    return defaults.get("api_key", "").strip()


def _utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _future_or_existing_start_time(value: Any = None) -> str:
    parsed = _parse_utc(value)
    if parsed is not None:
        return _utc_z(parsed)
    return _utc_z(datetime.now(timezone.utc))


def _use_next_waypoint_speed_rpm(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    original_values: List[Dict[str, float]] = []
    for point in points:
        props = point.get("properties") if isinstance(point.get("properties"), dict) else {}
        values = {}
        for key in ("speed", "rpm"):
            if isinstance(props.get(key), (int, float)):
                values[key] = float(props[key])
        original_values.append(values)

    for index, point in enumerate(points):
        props = point.setdefault("properties", {})
        if not isinstance(props, dict):
            continue
        props.pop("speed", None)
        props.pop("rpm", None)
        if index + 1 < len(original_values):
            props.update(original_values[index + 1])
    return points


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
                    example=_example_for_endpoint(path, example),
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
            path="/v1/performance-models/create",
            summary="Creates a generic performance model for IMO 9935208 using ABB project vessel particulars.",
            content_type="application/json",
            example=PROJECT_PERFORMANCE_CREATE_EXAMPLE,
        ),
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
                "operationProfile": CALCULATE_VOYAGE_OPERATION_PROFILE,
                "maxCalculationIntervalDistance": CALCULATE_VOYAGE_MAX_INTERVAL_DISTANCE_METERS,
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
        api_key = root.current_api_key()
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
        api_key = root.current_api_key()
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
        self.bind("<Button-4>", lambda _event: self._zoom_by(GLOBE_ZOOM_FACTOR))
        self.bind("<Button-5>", lambda _event: self._zoom_by(1 / GLOBE_ZOOM_FACTOR))
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
        self._radius = max(18.0, min(width, height) * 0.43 * self.zoom)

        self.create_rectangle(0, 0, width, height, fill="#07111f", outline="")
        self._draw_satellite_globe(width, height)
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
        delta = int(event.delta)
        if delta == 0:
            return
        steps = int(delta / 120) if delta else 0
        if steps == 0:
            steps = 1 if delta > 0 else -1
        steps = max(-6, min(6, steps))
        self._zoom_by(GLOBE_ZOOM_FACTOR ** steps)

    def _zoom_by(self, factor: float) -> None:
        self.zoom = max(GLOBE_MIN_ZOOM, min(GLOBE_MAX_ZOOM, self.zoom * factor))
        self.redraw()

    def _draw_satellite_globe(self, width: int, height: int) -> None:
        if Image is None or ImageTk is None:
            self._draw_texture_fallback("Install Pillow to render the satellite globe.")
            return
        if self._earth_texture is None:
            self._draw_texture_fallback(self._earth_texture_status)
            return

        globe = self._render_textured_globe(width, height)
        self._globe_photo = ImageTk.PhotoImage(globe)
        self.create_image(0, 0, anchor="nw", image=self._globe_photo)
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

    def _render_textured_globe(self, width: int, height: int) -> Any:
        texture = self._earth_texture
        radius = max(self._radius, 1.0)
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        pixels = image.load()
        texture_pixels = texture.load()
        tex_w, tex_h = texture.size
        cos_yaw = math.cos(self.yaw)
        sin_yaw = math.sin(self.yaw)
        cos_pitch = math.cos(self.pitch)
        sin_pitch = math.sin(self.pitch)

        left = max(0, int(math.floor(self._cx - radius)))
        right = min(width, int(math.ceil(self._cx + radius)))
        top = max(0, int(math.floor(self._cy - radius)))
        bottom = min(height, int(math.ceil(self._cy + radius)))

        for py in range(top, bottom):
            ny = (self._cy - py - 0.5) / radius
            for px in range(left, right):
                nx = (px + 0.5 - self._cx) / radius
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
            f"Routes {len(self.routes)}  Nodes {route_nodes}  Zoom {self.zoom:.2f}x"
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


class RtzBatchFrame(ttk.Frame, LogMixin):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.batch_stop_event = threading.Event()
        self.batch_running = False
        self._init_log_queue()
        self._build_widgets()
        self.after(100, self._flush_log_queue)

    def _build_widgets(self) -> None:
        batch_frame = ttk.LabelFrame(self, text="Continuous RTZ Batch", padding=(8, 8, 8, 4))
        batch_frame.pack(fill=tk.X, padx=8, pady=(8, 4))

        self.batch_planned_path_var = tk.StringVar()
        self.batch_optimal_dir_var = tk.StringVar(value=str(DEFAULT_BATCH_ROOT))
        self.batch_output_dir_var = tk.StringVar(value=str(DEFAULT_BATCH_OUTPUT_DIR))
        self.batch_endpoint_var = tk.StringVar(value="Find optimal voyage")
        self.batch_limit_var = tk.StringVar(value="0")

        ttk.Label(batch_frame, text="Planned RTZ").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(batch_frame, textvariable=self.batch_planned_path_var).grid(row=0, column=1, sticky="we", padx=5, pady=3)
        ttk.Button(batch_frame, text="Browse", command=self.browse_batch_planned_file).grid(row=0, column=2, padx=5, pady=3)

        ttk.Label(batch_frame, text="Reference optimal folder").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(batch_frame, textvariable=self.batch_optimal_dir_var).grid(row=1, column=1, sticky="we", padx=5, pady=3)
        ttk.Button(batch_frame, text="Browse", command=self.browse_batch_optimal_dir).grid(row=1, column=2, padx=5, pady=3)

        ttk.Label(batch_frame, text="Output folder").grid(row=2, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(batch_frame, textvariable=self.batch_output_dir_var).grid(row=2, column=1, sticky="we", padx=5, pady=3)
        ttk.Button(batch_frame, text="Browse", command=self.browse_batch_output_dir).grid(row=2, column=2, padx=5, pady=3)

        ttk.Label(batch_frame, text="Batch request").grid(row=3, column=0, sticky="w", padx=5, pady=3)
        endpoint_box = ttk.Combobox(
            batch_frame,
            textvariable=self.batch_endpoint_var,
            values=list(BATCH_ENDPOINTS.keys()),
            state="readonly",
            width=28,
        )
        endpoint_box.grid(row=3, column=1, sticky="w", padx=5, pady=3)
        endpoint_box.bind("<<ComboboxSelected>>", lambda _event: self.load_batch_sample())

        ttk.Label(batch_frame, text="Max files (0 = all)").grid(row=4, column=0, sticky="w", padx=5, pady=3)
        ttk.Spinbox(batch_frame, textvariable=self.batch_limit_var, from_=0, to=10000, increment=1, width=10).grid(
            row=4, column=1, sticky="w", padx=5, pady=3
        )
        ttk.Button(batch_frame, text="Load Sample", command=self.load_batch_sample).grid(row=4, column=1, sticky="e", padx=5, pady=3)
        ttk.Button(batch_frame, text="Start Batch", command=self.start_rtz_batch).grid(row=4, column=2, padx=5, pady=3)
        ttk.Button(batch_frame, text="Stop", command=self.stop_rtz_batch).grid(row=4, column=3, padx=5, pady=3)
        batch_frame.columnconfigure(1, weight=1)

        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        request_frame = ttk.LabelFrame(main, text="Base Request JSON")
        log_frame = ttk.LabelFrame(main, text="Batch Log")
        main.add(request_frame, weight=2)
        main.add(log_frame, weight=3)

        self.request_text = scrolledtext.ScrolledText(request_frame, wrap=tk.NONE)
        self.request_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.NONE)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.load_batch_sample()

    def _root_app(self) -> "NapaApiGui":
        return self.winfo_toplevel()  # type: ignore[return-value]

    def browse_batch_planned_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select planned RTZ",
            initialdir=str(DEFAULT_BATCH_ROOT),
            filetypes=[("RTZ files", "*.rtz"), ("XML files", "*.xml"), ("All files", "*.*")],
        )
        if path:
            self.batch_planned_path_var.set(path)

    def browse_batch_optimal_dir(self) -> None:
        path = filedialog.askdirectory(title="Select reference optimal RTZ folder", initialdir=str(DEFAULT_BATCH_ROOT))
        if path:
            self.batch_optimal_dir_var.set(path)

    def browse_batch_output_dir(self) -> None:
        path = filedialog.askdirectory(title="Select NAPA output RTZ folder", initialdir=str(DEFAULT_BATCH_ROOT))
        if path:
            self.batch_output_dir_var.set(path)

    def load_batch_sample(self) -> None:
        endpoint_path = BATCH_ENDPOINTS.get(self.batch_endpoint_var.get(), "/v1/find-optimal-voyage")
        sample: Dict[str, Any] = {}
        for endpoint in fallback_endpoints():
            if endpoint.path == endpoint_path and isinstance(endpoint.example, dict):
                sample = json.loads(json.dumps(endpoint.example))
                break
        self.request_text.delete("1.0", tk.END)
        self.request_text.insert("1.0", json_preview(sample or {}))

    def start_rtz_batch(self) -> None:
        if self.batch_running:
            messagebox.showinfo("RTZ Batch", "A batch is already running.")
            return
        try:
            settings = self._batch_settings()
        except Exception as exc:
            messagebox.showerror("RTZ Batch Error", str(exc))
            return
        self.batch_stop_event.clear()
        threading.Thread(target=self._run_rtz_batch, args=(settings,), daemon=True).start()

    def stop_rtz_batch(self) -> None:
        self.batch_stop_event.set()
        self.log("RTZ batch stop requested.")

    def _batch_settings(self) -> Dict[str, Any]:
        root = self._root_app()
        endpoint_name = self.batch_endpoint_var.get().strip()
        if endpoint_name not in BATCH_ENDPOINTS:
            raise ValueError(f"Unsupported batch request type: {endpoint_name}")
        raw_request = self.request_text.get("1.0", tk.END).strip()
        template = json.loads(raw_request) if raw_request else {}
        if not isinstance(template, dict):
            raise ValueError("Base Request JSON must be an object.")
        return {
            "endpoint_name": endpoint_name,
            "endpoint_path": BATCH_ENDPOINTS[endpoint_name],
            "base_url": root.base_url_var.get().strip() or DEFAULT_BASE_URL,
            "api_key": root.current_api_key(),
            "timeout": root.timeout_seconds(),
            "planned_path": self.batch_planned_path_var.get().strip(),
            "optimal_dir": self.batch_optimal_dir_var.get().strip(),
            "output_dir": self.batch_output_dir_var.get().strip(),
            "limit": self._batch_limit(),
            "template": template,
        }

    def _run_rtz_batch(self, settings: Dict[str, Any]) -> None:
        self.batch_running = True
        try:
            endpoint_name = settings["endpoint_name"]
            planned_path = Path(settings["planned_path"])
            optimal_dir = Path(settings["optimal_dir"])
            output_dir = Path(settings["output_dir"])
            if not planned_path.exists():
                raise ValueError(f"Planned RTZ not found: {planned_path}")
            if not optimal_dir.exists():
                raise ValueError(f"Reference optimal RTZ folder not found: {optimal_dir}")
            output_dir.mkdir(parents=True, exist_ok=True)

            planned_points, planned_schedule = self._parse_rtz(planned_path)
            if len(planned_points) < 2:
                raise ValueError("Planned RTZ must contain at least two waypoints.")
            if planned_schedule.get("eta"):
                self.log(f"Batch planned route ETA: {planned_schedule['eta']}")

            optimal_files = self._optimal_rtz_files(optimal_dir)
            limit = int(settings.get("limit") or 0)
            if limit:
                optimal_files = optimal_files[:limit]
            if not optimal_files:
                raise ValueError(f"No RTZ files found in reference optimal folder: {optimal_dir}")

            entries = []
            for sequence, optimal_path in enumerate(optimal_files, start=1):
                optimal_points, optimal_schedule = self._parse_rtz(optimal_path)
                if not optimal_points:
                    raise ValueError(f"Reference optimal RTZ has no waypoint: {optimal_path}")
                remaining_points = self._remaining_planned_points(planned_points, optimal_points[0])
                if len(remaining_points) < 2:
                    raise ValueError(f"Could not build at least two waypoints for: {optimal_path}")
                schedule = dict(optimal_schedule)
                if planned_schedule.get("eta"):
                    schedule["eta"] = planned_schedule["eta"]
                metadata = self._rtz_file_metadata(optimal_path)
                if metadata.get("timestamp_iso"):
                    schedule["etd"] = metadata["timestamp_iso"]
                entries.append({"source": optimal_path, "points": remaining_points, "schedule": schedule, "metadata": metadata, "sequence": sequence})

            self.log(f"RTZ batch prepared: {len(entries)} {endpoint_name} requests for {len(optimal_files)} reference files.")
            for index, entry in enumerate(entries, start=1):
                if self.batch_stop_event.is_set():
                    self.log("RTZ batch stopped.")
                    break
                source_path = entry["source"]
                metadata = entry["metadata"]
                output_name = self._batch_output_name(source_path, endpoint_name)
                output_path = output_dir / output_name
                request_path = output_path.with_suffix(".request.json")
                accepted_path = output_path.with_suffix(".accepted.json")
                response_path = output_path.with_suffix(".response.json")

                if self._resume_batch_output_if_possible(output_path, response_path):
                    continue

                payload = self._build_batch_payload(endpoint_name, entry["points"], entry["schedule"], metadata, settings["template"])
                self._write_batch_json(request_path, payload)
                self._run_batch_request_with_retries(
                    settings,
                    index,
                    len(entries),
                    source_path,
                    output_path,
                    output_name,
                    payload,
                    accepted_path,
                    response_path,
                )
            self.log("RTZ batch finished.")
        except Exception as exc:
            message = str(exc)
            self.log(f"ERROR: {message}")
            self.after(0, lambda: messagebox.showerror("RTZ Batch Error", message))
        finally:
            self.batch_running = False

    def _batch_limit(self) -> int:
        try:
            value = int(self.batch_limit_var.get())
        except ValueError:
            return 0
        return max(0, value)

    def _parse_rtz(self, path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        root = ET.parse(path).getroot()
        namespace = {"rtz": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
        waypoint_path = ".//rtz:waypoint" if namespace else ".//waypoint"
        position_path = "rtz:position" if namespace else "position"
        schedule_paths = (
            [".//rtz:scheduleElement", ".//rtz:sheduleElement"] if namespace else [".//scheduleElement", ".//sheduleElement"]
        )
        vo_path = ".//rtz:VOElement" if namespace else ".//VOElement"

        schedule_by_waypoint: Dict[str, Dict[str, Any]] = {}
        speeds = []
        etd = None
        eta = None
        for schedule_path in schedule_paths:
            for item in root.findall(schedule_path, namespace):
                waypoint_id = item.attrib.get("waypointId")
                waypoint_schedule: Dict[str, Any] = {}
                if item.attrib.get("speed"):
                    speed = float(item.attrib["speed"])
                    speeds.append(speed)
                    waypoint_schedule["speed"] = speed
                if item.attrib.get("rpm"):
                    waypoint_schedule["rpm"] = float(item.attrib["rpm"])
                if item.attrib.get("etd"):
                    waypoint_schedule["etd"] = item.attrib["etd"]
                if item.attrib.get("eta"):
                    waypoint_schedule["eta"] = item.attrib["eta"]
                if waypoint_id:
                    schedule_by_waypoint[waypoint_id] = waypoint_schedule
                etd = etd or item.attrib.get("etd")
                eta = item.attrib.get("eta") or eta

        for item in root.findall(vo_path, namespace):
            waypoint_id = item.attrib.get("waypointId")
            if waypoint_id:
                waypoint_schedule = schedule_by_waypoint.setdefault(waypoint_id, {})
                if item.attrib.get("speed") and "speed" not in waypoint_schedule:
                    speed = float(item.attrib["speed"])
                    waypoint_schedule["speed"] = speed
                    speeds.append(speed)
                if item.attrib.get("rpm") and "rpm" not in waypoint_schedule:
                    waypoint_schedule["rpm"] = float(item.attrib["rpm"])

        points = []
        for waypoint in root.findall(waypoint_path, namespace):
            position = waypoint.find(position_path, namespace)
            if position is None:
                continue
            lat = float(position.attrib["lat"])
            lon = float(position.attrib["lon"])
            waypoint_id = waypoint.attrib.get("id")
            name = waypoint.attrib.get("name") or f"WP {waypoint_id or len(points)}"
            geometry_type = waypoint.find("rtz:leg", namespace) if namespace else waypoint.find("leg")
            force_rhumb_line = geometry_type is not None and geometry_type.attrib.get("geometryType") == "Loxodrome"
            properties = {"name": name, "forceRhumbLine": force_rhumb_line}
            if waypoint_id in schedule_by_waypoint:
                properties.update(schedule_by_waypoint[waypoint_id])
            points.append(
                {
                    "type": "Feature",
                    "properties": properties,
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                }
            )

        _use_next_waypoint_speed_rpm(points)
        return points, {"speeds": speeds, "etd": etd, "eta": eta}

    def _build_batch_payload(
        self,
        endpoint_name: str,
        points: List[Dict[str, Any]],
        schedule: Dict[str, Any],
        metadata: Dict[str, Any],
        template: Dict[str, Any],
    ) -> Dict[str, Any]:
        payload = json.loads(json.dumps(template))
        if endpoint_name == CALCULATE_VOYAGE_BATCH_ENDPOINT:
            return self._build_calculate_voyage_payload(points, schedule, metadata, payload)

        start_lat, start_lon = self._feature_lat_lng(points[0])
        end_lat, end_lon = self._feature_lat_lng(points[-1])
        payload["fromCoordinates"] = {"latitude": start_lat, "longitude": start_lon}
        payload["toCoordinates"] = {"latitude": end_lat, "longitude": end_lon}
        payload["startTime"] = _future_or_existing_start_time(
            metadata.get("timestamp_iso") or schedule.get("etd") or payload.get("startTime")
        )

        speeds = [float(value) for value in schedule.get("speeds", []) if isinstance(value, (int, float))]
        if speeds:
            operation_method = payload.setdefault("operationMethod", {})
            if isinstance(operation_method, dict):
                operation_method["speedOverGround"] = round(sum(speeds) / len(speeds), 1)

        if metadata.get("imo") and not payload.get("imoNumber"):
            payload["imoNumber"] = int(metadata["imo"])
        return payload

    def _build_calculate_voyage_payload(
        self,
        points: List[Dict[str, Any]],
        schedule: Dict[str, Any],
        metadata: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        payload.pop("fromCoordinates", None)
        payload.pop("toCoordinates", None)
        payload.pop("startTime", None)
        payload.pop("operationMethod", None)
        payload["coordinates"] = [self._coordinate_payload(point) for point in points]
        payload["timestamps"] = self._batch_timestamps(points, schedule, metadata, payload)
        payload["operationProfile"] = CALCULATE_VOYAGE_OPERATION_PROFILE
        payload["maxCalculationIntervalDistance"] = CALCULATE_VOYAGE_MAX_INTERVAL_DISTANCE_METERS
        if metadata.get("imo") and not payload.get("imoNumber"):
            payload["imoNumber"] = int(metadata["imo"])
        return payload

    def _coordinate_payload(self, point: Dict[str, Any]) -> Dict[str, float]:
        lat, lon = self._feature_lat_lng(point)
        return {"latitude": lat, "longitude": lon}

    def _batch_timestamps(
        self,
        points: List[Dict[str, Any]],
        schedule: Dict[str, Any],
        metadata: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> List[str]:
        explicit = []
        for point in points:
            props = point.get("properties") if isinstance(point.get("properties"), dict) else {}
            value = props.get("etd") or props.get("eta") or props.get("time") or props.get("timestamp")
            explicit.append(_parse_utc(value))
        if all(item is not None for item in explicit):
            return [_utc_z(item) for item in explicit if item is not None]

        existing = payload.get("timestamps")
        if isinstance(existing, list) and len(existing) == len(points):
            parsed_existing = [_parse_utc(item) for item in existing]
            if all(item is not None for item in parsed_existing):
                return [_utc_z(item) for item in parsed_existing if item is not None]

        start_value = metadata.get("timestamp_iso") or schedule.get("etd")
        if not start_value and isinstance(existing, list) and existing:
            start_value = existing[0]
        current = _parse_utc(start_value) or datetime.now(timezone.utc)
        timestamps = [_utc_z(current)]
        default_speed = self._default_batch_speed(points, schedule, payload)
        for start, end in zip(points, points[1:]):
            speed = self._point_speed(end) or self._point_speed(start) or default_speed
            speed = max(1.0, float(speed))
            hours = self._distance_nm(start, end) / speed
            current = datetime.fromtimestamp(current.timestamp() + hours * 3600, tz=timezone.utc)
            timestamps.append(_utc_z(current))
        return timestamps

    def _point_speed(self, point: Dict[str, Any]) -> Optional[float]:
        props = point.get("properties") if isinstance(point.get("properties"), dict) else {}
        value = props.get("speed") if isinstance(props, dict) else None
        return float(value) if isinstance(value, (int, float)) else None

    def _default_batch_speed(self, points: List[Dict[str, Any]], schedule: Dict[str, Any], payload: Dict[str, Any]) -> float:
        speeds = [float(value) for value in schedule.get("speeds", []) if isinstance(value, (int, float))]
        if not speeds:
            speeds = [speed for point in points if (speed := self._point_speed(point)) is not None]
        if speeds:
            return max(1.0, sum(speeds) / len(speeds))
        operation_method = payload.get("operationMethod")
        if isinstance(operation_method, dict) and isinstance(operation_method.get("speedOverGround"), (int, float)):
            return max(1.0, float(operation_method["speedOverGround"]))
        return 10.0

    def _run_batch_request_with_retries(
        self,
        settings: Dict[str, Any],
        index: int,
        total: int,
        source_path: Path,
        output_path: Path,
        output_name: str,
        payload: Dict[str, Any],
        accepted_path: Path,
        response_path: Path,
    ) -> None:
        for attempt in range(1, BATCH_RETRY_ATTEMPTS + 1):
            if self.batch_stop_event.is_set():
                self.log("RTZ batch stopped.")
                return
            try:
                if self._resume_batch_output_if_possible(output_path, response_path):
                    return
                retry_text = "" if attempt == 1 else f" (retry {attempt}/{BATCH_RETRY_ATTEMPTS})"
                self.log(f"[{index}/{total}] Requesting {settings['endpoint_name']} from {source_path.name} -> {output_name}{retry_text}")
                accepted_data, route_data = self._send_napa_request_sync(settings, payload)
                self._write_batch_json(accepted_path, accepted_data)
                self._write_batch_json(response_path, route_data)
                self.log(f"Saved JSON: {output_path.with_suffix('.request.json').name}, {accepted_path.name}, {response_path.name}")
                self._save_batch_route(output_path, route_data)
                return
            except Exception as exc:
                if not self._is_connection_reset_10054(exc) or attempt >= BATCH_RETRY_ATTEMPTS:
                    raise
                self.log(
                    f"Connection reset during batch request. Restarting current item after "
                    f"{BATCH_RETRY_DELAY_SECONDS}s ({attempt}/{BATCH_RETRY_ATTEMPTS})."
                )
                if self.batch_stop_event.wait(BATCH_RETRY_DELAY_SECONDS):
                    self.log("RTZ batch stopped.")
                    return

    def _send_napa_request_sync(self, settings: Dict[str, Any], payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Any]:
        url = build_url(settings["base_url"], settings["endpoint_path"], {})
        headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "napa-api-gui-test/1.0"}
        if settings.get("api_key"):
            headers["x-api-key"] = settings["api_key"]

        started = time.time()
        response = require_requests().post(url, headers=headers, json=payload, timeout=settings["timeout"])
        accepted_data = {"requestUrl": url, "initial": self._response_snapshot(response, time.time() - started), "polls": []}
        self._raise_for_http_response(response, "Batch POST")
        location = response.headers.get("Location", "")
        if location:
            location = absolute_location(settings["base_url"], location)
            accepted_data["location"] = location

        parsed, _body_text = _response_body_text(response)
        if response.status_code == 202 and location:
            route_data = self._poll_batch_location(location, headers, settings["timeout"], accepted_data)
        elif parsed is not None:
            route_data = parsed
        else:
            route_data = accepted_data["initial"]
        return accepted_data, route_data

    def _poll_batch_location(
        self,
        location: str,
        headers: Dict[str, str],
        timeout: int,
        accepted_data: Dict[str, Any],
    ) -> Any:
        for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
            if self.batch_stop_event.is_set():
                raise RuntimeError("RTZ batch stopped during polling.")
            started = time.time()
            response = require_requests().get(location, headers=headers, timeout=timeout)
            snapshot = self._response_snapshot(response, time.time() - started)
            snapshot["attempt"] = attempt
            accepted_data["polls"].append(snapshot)
            self.log(f"Location attempt {attempt}/{POLL_MAX_ATTEMPTS}: HTTP {response.status_code}")
            self._raise_for_http_response(response, "Batch Location poll")
            parsed, _body_text = _response_body_text(response)
            if parsed is not None and self._is_async_done(parsed):
                return parsed
            if parsed is None and response.status_code != 202:
                return snapshot
            if attempt < POLL_MAX_ATTEMPTS:
                time.sleep(POLL_INTERVAL_SECONDS)
        raise TimeoutError(f"Location polling did not complete after {POLL_MAX_ATTEMPTS} attempts: {location}")

    def _response_snapshot(self, response: requests.Response, elapsed: float) -> Dict[str, Any]:
        parsed, body_text = _response_body_text(response)
        snapshot: Dict[str, Any] = {
            "statusCode": response.status_code,
            "elapsedSeconds": round(elapsed, 3),
            "headers": {
                key: value
                for key, value in response.headers.items()
                if key.lower() in {"content-type", "location", "retry-after"}
            },
        }
        if parsed is not None:
            snapshot["body"] = parsed
        elif body_text:
            snapshot["bodyText"] = body_text
        return snapshot

    def _raise_for_http_response(self, response: requests.Response, context: str) -> None:
        if response.status_code < 400:
            return
        _parsed, body_text = _response_body_text(response)
        detail = body_text[:2000] if body_text else response.text[:2000]
        raise RuntimeError(f"{context} failed: HTTP {response.status_code} {detail}")

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

    def _save_batch_route(self, output_path: Path, route_data: Any) -> bool:
        route_points = self._extract_route_points_for_rtz(route_data)
        if len(route_points) < 2:
            self.log(f"No route geometry found. Response saved: {output_path.with_suffix('.response.json')}")
            return False
        self._write_rtz(output_path, route_points, route_name=output_path.stem)
        self.log(f"Saved RTZ: {output_path} ({len(route_points)} waypoints)")
        return True

    def _resume_batch_output_if_possible(self, output_path: Path, response_path: Path) -> bool:
        if output_path.exists() and output_path.stat().st_size > 0:
            self.log(f"Resume: existing RTZ found. Skipping API request: {output_path.name}")
            return True
        if not response_path.exists() or response_path.stat().st_size <= 0:
            return False
        try:
            route_data = self._read_batch_json(response_path)
        except Exception as exc:
            self.log(f"Resume: existing response JSON could not be read. Request will be retried: {response_path.name} ({exc})")
            return False
        route_points = self._extract_route_points_for_rtz(route_data)
        if len(route_points) < 2:
            self.log(f"Resume: existing JSON has no route geometry. Request will be retried: {response_path.name}")
            return False
        self._write_rtz(output_path, route_points, route_name=output_path.stem)
        self.log(f"Resume: rebuilt RTZ from existing JSON: {output_path} ({len(route_points)} waypoints)")
        return True

    def _optimal_rtz_files(self, optimal_dir: Path) -> List[Path]:
        return sorted(optimal_dir.glob("*.rtz"), key=lambda path: (self._rtz_file_metadata(path).get("timestamp_sort") or path.name, path.name))

    def _rtz_file_metadata(self, path: Path) -> Dict[str, Any]:
        match = RTZ_FILE_NAME_RE.match(path.name)
        if not match:
            return {"kind": "", "timestamp_label": "", "timestamp_iso": "", "timestamp_sort": path.name}
        label = f"{match.group('date')}_{match.group('time')}"
        dt = datetime.strptime(match.group("date") + match.group("time"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return {
            "imo": match.group("imo"),
            "kind": match.group("kind"),
            "timestamp_label": label,
            "timestamp_iso": _utc_z(dt),
            "timestamp_sort": dt,
        }

    def _batch_output_name(self, source_path: Path, endpoint_name: str) -> str:
        metadata = self._rtz_file_metadata(source_path)
        output_kind = BATCH_OUTPUT_KINDS.get(endpoint_name, self._batch_endpoint_slug(endpoint_name))
        if metadata.get("imo") and metadata.get("timestamp_label"):
            return f"{metadata['imo']}_SAS_{output_kind}_{metadata['timestamp_label']}.rtz"
        return f"{source_path.stem}_{output_kind}.rtz"

    def _batch_endpoint_slug(self, endpoint_name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", endpoint_name.lower()).strip("-") or "batch"

    def _write_batch_json(self, path: Path, data: Any) -> None:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _read_batch_json(self, path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    def _remaining_planned_points(self, planned_points: List[Dict[str, Any]], current_point: Dict[str, Any]) -> List[Dict[str, Any]]:
        if len(planned_points) < 2:
            return planned_points
        current_lat, current_lon = self._feature_lat_lng(current_point)
        best_index = 0
        best_score = float("inf")
        for index in range(len(planned_points) - 1):
            start_lat, start_lon = self._feature_lat_lng(planned_points[index])
            end_lat, end_lon = self._feature_lat_lng(planned_points[index + 1])
            score = self._point_segment_score(current_lat, current_lon, start_lat, start_lon, end_lat, end_lon)
            if score < best_score:
                best_score = score
                best_index = index
        cut_index = min(best_index + 1, len(planned_points) - 1)
        if self._distance_nm(current_point, planned_points[cut_index]) < 0.2 and cut_index + 1 < len(planned_points):
            cut_index += 1
        current_feature = json.loads(json.dumps(current_point))
        current_feature.setdefault("properties", {})
        if isinstance(current_feature["properties"], dict):
            current_feature["properties"]["name"] = current_feature["properties"].get("name") or "Current position"
        return [current_feature] + json.loads(json.dumps(planned_points[cut_index:]))

    def _feature_lat_lng(self, feature: Dict[str, Any]) -> Tuple[float, float]:
        coordinates = feature["geometry"]["coordinates"]
        return float(coordinates[1]), float(coordinates[0])

    def _point_segment_score(
        self,
        point_lat: float,
        point_lon: float,
        start_lat: float,
        start_lon: float,
        end_lat: float,
        end_lon: float,
    ) -> float:
        scale = math.cos(math.radians((point_lat + start_lat + end_lat) / 3))
        end_lon = start_lon + ((end_lon - start_lon + 180) % 360) - 180
        point_lon = start_lon + ((point_lon - start_lon + 180) % 360) - 180
        px, py = point_lon * scale, point_lat
        sx, sy = start_lon * scale, start_lat
        ex, ey = end_lon * scale, end_lat
        dx, dy = ex - sx, ey - sy
        if dx == 0 and dy == 0:
            return (px - sx) ** 2 + (py - sy) ** 2
        fraction = max(0.0, min(1.0, ((px - sx) * dx + (py - sy) * dy) / (dx * dx + dy * dy)))
        closest_x = sx + fraction * dx
        closest_y = sy + fraction * dy
        return (px - closest_x) ** 2 + (py - closest_y) ** 2

    def _distance_nm(self, first: Dict[str, Any], second: Dict[str, Any]) -> float:
        lat1, lon1 = self._feature_lat_lng(first)
        lat2, lon2 = self._feature_lat_lng(second)
        radius_nm = 3440.065
        d_lat = math.radians(lat2 - lat1)
        d_lon = math.radians(((lon2 - lon1 + 180) % 360) - 180)
        a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
        return radius_nm * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))

    def _extract_route_points_for_rtz(self, data: Any) -> List[Dict[str, Any]]:
        candidates: List[List[Dict[str, Any]]] = []

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

        def coord_pair(value: Any) -> Optional[Tuple[float, float]]:
            if isinstance(value, list) and len(value) >= 2:
                first = number(value[0])
                second = number(value[1])
                if first is None or second is None:
                    return None
                if -180 <= first <= 180 and -90 <= second <= 90:
                    return second, first
                if -90 <= first <= 90 and -180 <= second <= 180:
                    return first, second
            return None

        def named_pair(obj: Any) -> Optional[Tuple[float, float]]:
            if not isinstance(obj, dict):
                return None
            lat = number(obj.get("latitude", obj.get("lat")))
            lon = number(obj.get("longitude", obj.get("lon", obj.get("lng"))))
            if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
            return None

        def numeric_value(obj: Any, keys: Tuple[str, ...]) -> Optional[float]:
            if not isinstance(obj, dict):
                return None
            for key in keys:
                value = number(obj.get(key))
                if value is not None:
                    return value
            for nested_key in ("properties", "operationMethod", "operation_method"):
                nested = obj.get(nested_key)
                nested_value = numeric_value(nested, keys) if isinstance(nested, dict) else None
                if nested_value is not None:
                    return nested_value
            return None

        def inherited_route_properties(obj: Any, inherited: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            properties = dict(inherited or {})
            speed = numeric_value(obj, ("speed", "speedOverGround", "speedKnots", "sog", "plannedSpeed"))
            if speed is not None:
                properties["speed"] = speed
            rpm = numeric_value(obj, ("rpm", "engineRpm", "shaftRpm"))
            if rpm is not None:
                properties["rpm"] = rpm
            return properties

        def feature_from_point(
            obj: Any,
            index: int,
            inherited: Optional[Dict[str, Any]] = None,
        ) -> Optional[Dict[str, Any]]:
            if not isinstance(obj, dict):
                return None
            geometry = obj.get("geometry") if isinstance(obj.get("geometry"), dict) else {}
            pair = coord_pair(geometry.get("coordinates")) or named_pair(obj)
            if pair is None:
                return None
            lat, lon = pair
            props = obj.get("properties") if isinstance(obj.get("properties"), dict) else {}
            properties = inherited_route_properties(obj, inherited)
            properties["name"] = str(props.get("name") or obj.get("name") or obj.get("label") or f"WP {index}")
            speed = numeric_value(obj, ("speed", "speedOverGround", "speedKnots", "sog", "plannedSpeed"))
            if speed is not None:
                properties["speed"] = speed
            rpm = numeric_value(obj, ("rpm", "engineRpm", "shaftRpm"))
            if rpm is not None:
                properties["rpm"] = rpm
            for key in ("eta", "etd", "time", "timestamp"):
                if props.get(key):
                    properties[key] = props[key]
                elif obj.get(key):
                    properties[key] = obj[key]
            return {"type": "Feature", "properties": properties, "geometry": {"type": "Point", "coordinates": [lon, lat]}}

        def add_line(coords: Any, inherited: Optional[Dict[str, Any]] = None) -> None:
            if not isinstance(coords, list):
                return
            route_points = []
            for index, item in enumerate(coords, start=1):
                pair = coord_pair(item)
                if pair:
                    lat, lon = pair
                    properties = dict(inherited or {})
                    properties["name"] = f"WP {index}"
                    route_points.append({"type": "Feature", "properties": properties, "geometry": {"type": "Point", "coordinates": [lon, lat]}})
            if len(route_points) >= 2:
                candidates.append(route_points)

        def visit(obj: Any, inherited: Optional[Dict[str, Any]] = None) -> None:
            if isinstance(obj, dict):
                local_inherited = inherited_route_properties(obj, inherited)
                geometry = obj.get("geometry") if isinstance(obj.get("geometry"), dict) else {}
                coords = geometry.get("coordinates")
                if geometry.get("type") == "LineString":
                    add_line(coords, local_inherited)
                points = obj.get("points")
                if isinstance(points, list):
                    route_points = [
                        point
                        for index, item in enumerate(points, start=1)
                        if (point := feature_from_point(item, index, local_inherited))
                    ]
                    if len(route_points) >= 2:
                        candidates.append(route_points)
                coordinates = obj.get("coordinates")
                if isinstance(coordinates, list):
                    named_points = [
                        point
                        for index, item in enumerate(coordinates, start=1)
                        if (point := feature_from_point(item, index, local_inherited))
                    ]
                    if len(named_points) >= 2:
                        candidates.append(named_points)
                    else:
                        add_line(coordinates, local_inherited)
                for value in obj.values():
                    visit(value, local_inherited)
            elif isinstance(obj, list):
                route_points = [
                    point
                    for index, item in enumerate(obj, start=1)
                    if (point := feature_from_point(item, index, inherited))
                ]
                if len(route_points) >= 2:
                    candidates.append(route_points)
                else:
                    add_line(obj, inherited)
                    for item in obj:
                        visit(item, inherited)

        def route_candidate_score(points: List[Dict[str, Any]]) -> Tuple[int, int, int]:
            rpm_count = 0
            speed_count = 0
            for point in points:
                props = point.get("properties") if isinstance(point.get("properties"), dict) else {}
                if isinstance(props.get("rpm"), (int, float)):
                    rpm_count += 1
                if isinstance(props.get("speed"), (int, float)):
                    speed_count += 1
            return rpm_count, speed_count, len(points)

        visit(data)
        return max(candidates, key=route_candidate_score) if candidates else []

    def _write_rtz(self, output_path: Path, route_points: List[Dict[str, Any]], route_name: str) -> None:
        namespace = "http://www.cirm.org/RTZ/1/0"
        ET.register_namespace("", namespace)
        route = ET.Element(f"{{{namespace}}}route", {"version": "1.0"})
        ET.SubElement(route, f"{{{namespace}}}routeInfo", {"routeName": route_name})
        waypoints = ET.SubElement(route, f"{{{namespace}}}waypoints")
        has_schedule = False
        speeds_by_index: Dict[int, float] = {}
        rpms_by_index: Dict[int, float] = {}
        times_by_index: Dict[int, Dict[str, str]] = {}

        for index, point in enumerate(route_points):
            lat, lon = self._feature_lat_lng(point)
            props = point.get("properties") if isinstance(point.get("properties"), dict) else {}
            waypoint = ET.SubElement(waypoints, f"{{{namespace}}}waypoint", {"id": str(index), "name": str(props.get("name") or ""), "radius": "0.50"})
            ET.SubElement(waypoint, f"{{{namespace}}}position", {"lat": self._fmt_coord(lat), "lon": self._fmt_coord(lon)})
            geometry_type = "Loxodrome" if props.get("forceRhumbLine") else "Orthodrome"
            ET.SubElement(waypoint, f"{{{namespace}}}leg", {"starboardXTD": "0.03", "portsideXTD": "0.03", "geometryType": geometry_type})
            if isinstance(props.get("speed"), (int, float)):
                speeds_by_index[index] = float(props["speed"])
                has_schedule = True
            if isinstance(props.get("rpm"), (int, float)):
                rpms_by_index[index] = float(props["rpm"])
            time_attrs = {key: str(props[key]) for key in ("etd", "eta") if props.get(key)}
            if time_attrs:
                times_by_index[index] = time_attrs
                has_schedule = True

        if has_schedule:
            schedules = ET.SubElement(route, f"{{{namespace}}}schedules")
            schedule = ET.SubElement(schedules, f"{{{namespace}}}schedule", {"id": "0"})
            calculated = ET.SubElement(schedule, f"{{{namespace}}}calculated")
            for index in range(len(route_points)):
                attrs = {"waypointId": str(index)}
                if index in speeds_by_index:
                    attrs["speed"] = self._fmt_float(speeds_by_index[index])
                attrs.update(times_by_index.get(index, {}))
                ET.SubElement(calculated, f"{{{namespace}}}scheduleElement", attrs)

        if speeds_by_index or rpms_by_index:
            extensions = ET.SubElement(route, f"{{{namespace}}}extensions")
            extension = ET.SubElement(extensions, f"{{{namespace}}}extension")
            vo = ET.SubElement(extension, f"{{{namespace}}}VoyageOptimization")
            for index in sorted(set(speeds_by_index) | set(rpms_by_index)):
                attrs = {"waypointId": str(index), "usingspeed": "0"}
                if index in speeds_by_index:
                    attrs["speed"] = self._fmt_float(speeds_by_index[index])
                if index in rpms_by_index:
                    attrs["rpm"] = self._fmt_float(rpms_by_index[index])
                ET.SubElement(vo, f"{{{namespace}}}VOElement", attrs)

        ET.indent(route, space="    ")
        ET.ElementTree(route).write(output_path, encoding="UTF-8", xml_declaration=True)

    def _fmt_coord(self, value: float) -> str:
        return f"{value:.8f}".rstrip("0").rstrip(".")

    def _fmt_float(self, value: float) -> str:
        return f"{value:.6f}".rstrip("0").rstrip(".")

    def _is_connection_reset_10054(self, exc: BaseException) -> bool:
        if isinstance(exc, ConnectionResetError):
            return True
        if getattr(exc, "errno", None) == 10054 or getattr(exc, "winerror", None) == 10054:
            return True
        text = str(exc)
        return "10054" in text or "connection reset" in text.lower()


class ProfileCanvas(tk.Canvas):
    def __init__(self, master: tk.Misc, title: str, y_label: str, color: str) -> None:
        super().__init__(master, background="#0f172a", highlightthickness=0, height=260)
        self.title = title
        self.y_label = y_label
        self.color = color
        self.intervals: List[Dict[str, Any]] = []
        self.series: List[Dict[str, Any]] = []
        self.bind("<Configure>", lambda _event: self.redraw())

    def set_series(self, series: List[Dict[str, Any]]) -> None:
        normalized = []
        for index, item in enumerate(series, start=1):
            intervals = [
                interval
                for interval in item.get("intervals", [])
                if isinstance(interval.get("start"), datetime)
                and isinstance(interval.get("end"), datetime)
                and isinstance(interval.get("value"), (int, float))
            ]
            if not intervals:
                continue
            normalized.append(
                {
                    "label": str(item.get("label") or f"Series {index}"),
                    "color": str(item.get("color") or self.color),
                    "intervals": sorted(intervals, key=lambda interval: interval["start"]),
                }
            )
        self.series = normalized
        self.intervals = [interval for item in normalized for interval in item["intervals"]]
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 1)
        height = max(self.winfo_height(), 1)
        self.create_rectangle(0, 0, width, height, fill="#0f172a", outline="")
        self.create_text(14, 12, anchor="nw", text=self.title, fill="#e2e8f0", font=("Segoe UI", 10, "bold"))

        if not self.intervals:
            self.create_text(width / 2, height / 2, text="No profile data", fill="#94a3b8", font=("Segoe UI", 10))
            return

        left, right, top, bottom = 68, 18, 42, 42
        plot_w = max(1, width - left - right)
        plot_h = max(1, height - top - bottom)
        start_time = min(item["start"] for item in self.intervals)
        end_time = max(item["end"] for item in self.intervals)
        values = [float(item["value"]) for item in self.intervals]
        min_value = min(values)
        max_value = max(values)
        if min_value == max_value:
            min_value -= 1
            max_value += 1
        else:
            padding = (max_value - min_value) * 0.08
            min_value -= padding
            max_value += padding
        total_seconds = max(1.0, (end_time - start_time).total_seconds())

        def x_pos(value: datetime) -> float:
            return left + ((value - start_time).total_seconds() / total_seconds) * plot_w

        def y_pos(value: float) -> float:
            return top + (max_value - value) / (max_value - min_value) * plot_h

        self.create_line(left, top, left, top + plot_h, fill="#64748b")
        self.create_line(left, top + plot_h, left + plot_w, top + plot_h, fill="#64748b")

        for index in range(5):
            fraction = index / 4
            value = min_value + (max_value - min_value) * (1 - fraction)
            y = top + plot_h * fraction
            self.create_line(left, y, left + plot_w, y, fill="#1e293b")
            self.create_text(left - 8, y, anchor="e", text=f"{value:.1f}", fill="#cbd5e1", font=("Segoe UI", 8))

        for index in range(5):
            fraction = index / 4
            tick_time = start_time + timedelta(seconds=total_seconds * fraction)
            x = left + plot_w * fraction
            self.create_line(x, top, x, top + plot_h, fill="#1e293b")
            self.create_text(x, top + plot_h + 16, text=tick_time.strftime("%m-%d %H:%M"), fill="#cbd5e1", font=("Segoe UI", 8))

        for item in self.series:
            previous_x = None
            previous_y = None
            color = item["color"]
            for interval in item["intervals"]:
                x1 = x_pos(interval["start"])
                x2 = x_pos(interval["end"])
                y = y_pos(float(interval["value"]))
                if previous_x is not None and previous_y is not None:
                    self.create_line(x1, previous_y, x1, y, fill=color, width=2)
                self.create_line(x1, y, x2, y, fill=color, width=2)
                previous_x = x2
                previous_y = y

        self.create_text(16, top + plot_h / 2, text=self.y_label, fill="#cbd5e1", font=("Segoe UI", 8), angle=90)
        self._draw_legend(width)

    def _draw_legend(self, width: int) -> None:
        if not self.series:
            return
        max_items = 6
        y = 16
        x = width - 16
        for item in self.series[:max_items]:
            label = self._short_label(item["label"])
            text_id = self.create_text(x, y, anchor="ne", text=label, fill="#cbd5e1", font=("Segoe UI", 8))
            bbox = self.bbox(text_id)
            line_x = (bbox[0] - 22) if bbox else (x - 96)
            self.create_line(line_x, y, line_x + 14, y, fill=item["color"], width=3)
            y += 15
        if len(self.series) > max_items:
            self.create_text(x, y, anchor="ne", text=f"+{len(self.series) - max_items} more", fill="#94a3b8", font=("Segoe UI", 8))

    def _short_label(self, label: str) -> str:
        return label if len(label) <= 30 else f"{label[:27]}..."


class ResultPreviewFrame(ttk.Frame, LogMixin):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=10)
        self.speed_intervals: List[Dict[str, Any]] = []
        self.rpm_intervals: List[Dict[str, Any]] = []
        self.profile_series: List[Dict[str, Any]] = []
        self.profile_worker_running = False
        self.auto_profile_refresh_active = False
        self._init_log_queue()
        self._build_ui()
        self.after(100, self._flush_log_queue)
        self.after(AUTO_PROFILE_REFRESH_MS, self._auto_generate_profiles_while_batch_running)

    def _build_ui(self) -> None:
        controls = ttk.LabelFrame(self, text="Result RTZ Folders")
        controls.pack(fill=tk.X, pady=(0, 8))
        self.profile_folder_var = tk.StringVar(value=str(DEFAULT_BATCH_OUTPUT_DIR))
        self.profile_limit_var = tk.StringVar(value="0")
        ttk.Label(controls, text="Folders").grid(row=0, column=0, sticky="nw", padx=5, pady=5)
        self.profile_folder_list = tk.Listbox(controls, height=4, exportselection=False)
        self.profile_folder_list.grid(row=0, column=1, rowspan=3, sticky="we", padx=5, pady=5)
        self.profile_folder_list.insert(tk.END, str(DEFAULT_BATCH_OUTPUT_DIR))
        folder_buttons = ttk.Frame(controls)
        folder_buttons.grid(row=0, column=2, rowspan=3, sticky="n", padx=5, pady=5)
        ttk.Button(folder_buttons, text="Add Folder", command=self.browse_profile_folder).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(folder_buttons, text="Remove", command=self.remove_profile_folder).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(folder_buttons, text="Clear", command=self.clear_profile_folders).pack(fill=tk.X)
        ttk.Label(controls, text="Max files (0 = all)").grid(row=3, column=0, sticky="w", padx=5, pady=5)
        ttk.Spinbox(controls, textvariable=self.profile_limit_var, from_=0, to=10000, increment=1, width=10).grid(
            row=3, column=1, sticky="w", padx=5, pady=5
        )
        ttk.Button(controls, text="Generate Profiles", command=self.generate_profiles).grid(row=3, column=1, sticky="e", padx=5, pady=5)
        ttk.Button(controls, text="Save CSV", command=self.save_profiles_csv).grid(row=3, column=2, padx=5, pady=5)
        controls.columnconfigure(1, weight=1)

        charts = ttk.PanedWindow(self, orient=tk.VERTICAL)
        charts.pack(fill=tk.BOTH, expand=True)
        speed_frame = ttk.LabelFrame(charts, text="Speed Profile")
        rpm_frame = ttk.LabelFrame(charts, text="RPM Profile")
        charts.add(speed_frame, weight=1)
        charts.add(rpm_frame, weight=1)
        self.speed_canvas = ProfileCanvas(speed_frame, "Speed Profile", "Speed (kn)", "#38bdf8")
        self.speed_canvas.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.rpm_canvas = ProfileCanvas(rpm_frame, "RPM Profile", "RPM", "#f59e0b")
        self.rpm_canvas.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        log_frame = ttk.LabelFrame(self, text="Profile Log")
        log_frame.pack(fill=tk.BOTH, expand=False, pady=(8, 0))
        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, wrap=tk.NONE)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    def browse_profile_folder(self) -> None:
        path = filedialog.askdirectory(title="Select result RTZ folder", initialdir=str(DEFAULT_BATCH_ROOT))
        if path:
            self._add_profile_folder(path)

    def remove_profile_folder(self) -> None:
        for index in reversed(self.profile_folder_list.curselection()):
            self.profile_folder_list.delete(index)

    def clear_profile_folders(self) -> None:
        self.profile_folder_list.delete(0, tk.END)

    def _add_profile_folder(self, path: str) -> None:
        value = str(Path(path))
        existing = set(self.profile_folder_list.get(0, tk.END))
        if value not in existing:
            self.profile_folder_list.insert(tk.END, value)

    def _profile_folders(self) -> List[Path]:
        values = [str(value).strip() for value in self.profile_folder_list.get(0, tk.END) if str(value).strip()]
        if not values and self.profile_folder_var.get().strip():
            values = [self.profile_folder_var.get().strip()]
        folders = []
        seen = set()
        for value in values:
            folder = Path(value)
            key = str(folder).lower()
            if key in seen:
                continue
            seen.add(key)
            folders.append(folder)
        return folders

    def generate_profiles(self, auto: bool = False) -> None:
        if self.profile_worker_running:
            if not auto:
                self.log("Profile generation is already running.")
            return
        self.profile_worker_running = True
        threading.Thread(target=lambda: self._generate_profiles_worker(auto=auto), daemon=True).start()

    def _auto_generate_profiles_while_batch_running(self) -> None:
        try:
            root = self.winfo_toplevel()
            batch_tab = getattr(root, "rtz_batch_tab", None)
            batch_running = bool(getattr(batch_tab, "batch_running", False))
            if batch_running:
                if not self.auto_profile_refresh_active:
                    self.auto_profile_refresh_active = True
                    seconds = max(1, AUTO_PROFILE_REFRESH_MS // 1000)
                    self.log(f"Auto profile refresh active while batch is running ({seconds}s interval).")
                output_var = getattr(batch_tab, "batch_output_dir_var", None)
                output_dir = output_var.get().strip() if output_var is not None else ""
                if output_dir:
                    self._add_profile_folder(output_dir)
                if self._has_profile_input_files():
                    self.generate_profiles(auto=True)
            elif self.auto_profile_refresh_active:
                self.auto_profile_refresh_active = False
                self.log("Auto profile refresh stopped because batch is not running.")
        finally:
            self.after(AUTO_PROFILE_REFRESH_MS, self._auto_generate_profiles_while_batch_running)

    def _has_profile_input_files(self) -> bool:
        for folder in self._profile_folders():
            try:
                if folder.exists() and len(list(folder.glob("*.rtz"))) >= 2:
                    return True
            except OSError:
                continue
        return False

    def _generate_profiles_worker(self, auto: bool = False) -> None:
        try:
            folders = self._profile_folders()
            if not folders:
                raise ValueError("Add at least one result RTZ folder.")
            limit = self._profile_limit()
            used_labels: set = set()
            profile_series: List[Dict[str, Any]] = []
            for folder in folders:
                if not folder.exists():
                    raise ValueError(f"Folder not found: {folder}")
                files = self._optimal_rtz_files(folder)
                if limit:
                    files = files[:limit]
                if len(files) < 2:
                    self.log(f"Skipping folder with fewer than two RTZ files: {folder}")
                    continue

                records_by_kind: Dict[str, List[Dict[str, Any]]] = {}
                for path in files:
                    metadata = self._rtz_file_metadata(path)
                    timestamp = metadata.get("timestamp_dt")
                    if timestamp is None:
                        self.log(f"Skipping file without result timestamp pattern: {path.name}")
                        continue
                    points = self._parse_profile_rtz(path)
                    if not points:
                        self.log(f"Skipping RTZ without waypoints: {path.name}")
                        continue
                    kind = str(metadata.get("kind") or "Result")
                    records_by_kind.setdefault(kind, []).append({"path": path, "time": timestamp, "points": points, "kind": kind})

                usable_groups: List[Tuple[str, List[Dict[str, Any]]]] = []
                for kind, records in sorted(records_by_kind.items(), key=lambda item: item[0].lower()):
                    records.sort(key=lambda item: (item["time"], item["path"].name))
                    if len(records) < 2:
                        self.log(f"Skipping {kind} in {folder.name}: fewer than two timestamped RTZ files.")
                        continue
                    usable_groups.append((kind, records))
                if not usable_groups:
                    self.log(f"Skipping folder without usable timestamped result RTZ pairs: {folder}")
                    continue

                show_kind_in_label = len(usable_groups) > 1
                for kind, records in usable_groups:
                    label = self._profile_series_label(folder, used_labels, kind if show_kind_in_label else "")
                    color = PROFILE_SERIES_COLORS[len(profile_series) % len(PROFILE_SERIES_COLORS)]
                    speed_intervals: List[Dict[str, Any]] = []
                    rpm_intervals: List[Dict[str, Any]] = []
                    for index in range(len(records) - 1):
                        speed_items, rpm_items = self._profile_pair_intervals(records[index], records[index + 1])
                        for item in speed_items:
                            item["series"] = label
                            item["folder"] = str(folder)
                            item["kind"] = kind
                        for item in rpm_items:
                            item["series"] = label
                            item["folder"] = str(folder)
                            item["kind"] = kind
                        speed_intervals.extend(speed_items)
                        rpm_intervals.extend(rpm_items)

                    profile_series.append(
                        {
                            "label": label,
                            "folder": str(folder),
                            "kind": kind,
                            "color": color,
                            "records": records,
                            "speed_intervals": speed_intervals,
                            "rpm_intervals": rpm_intervals,
                        }
                    )

            if not profile_series:
                raise ValueError("No usable result RTZ folders found.")

            self.after(0, lambda: self._apply_profiles(profile_series))
        except Exception as exc:
            message = str(exc)
            self.log(f"ERROR: {message}")
            if not auto:
                self.after(0, lambda: messagebox.showerror("Result Preview Error", message))
        finally:
            self.profile_worker_running = False

    def _profile_series_label(self, folder: Path, used_labels: set, kind: str = "") -> str:
        base = folder.name or str(folder)
        if kind:
            base = f"{base} / {kind}"
        label = base
        if label in used_labels:
            parent_label = f"{folder.parent.name}\\{base}" if folder.parent.name else base
            label = parent_label
        suffix = 2
        while label in used_labels:
            label = f"{base} {suffix}"
            suffix += 1
        used_labels.add(label)
        return label

    def _apply_profiles(self, profile_series: List[Dict[str, Any]]) -> None:
        self.profile_series = profile_series
        self.speed_intervals = [item for series in profile_series for item in series["speed_intervals"]]
        self.rpm_intervals = [item for series in profile_series for item in series["rpm_intervals"]]
        self.speed_canvas.set_series(
            [
                {"label": series["label"], "color": series["color"], "intervals": series["speed_intervals"]}
                for series in profile_series
            ]
        )
        self.rpm_canvas.set_series(
            [
                {"label": series["label"], "color": series["color"], "intervals": series["rpm_intervals"]}
                for series in profile_series
            ]
        )
        total_records = sum(len(series["records"]) for series in profile_series)
        self.log(
            f"Generated profiles from {len(profile_series)} result series and {total_records} RTZ files: "
            f"{len(self.speed_intervals)} speed intervals, {len(self.rpm_intervals)} rpm intervals."
        )
        for series in profile_series:
            self.log(
                f"{series['label']}: {len(series['records'])} files, "
                f"{len(series['speed_intervals'])} speed intervals, {len(series['rpm_intervals'])} rpm intervals."
            )
        if self.speed_intervals:
            speeds = [float(item["value"]) for item in self.speed_intervals]
            self.log(f"Speed min/avg/max: {min(speeds):.2f}/{sum(speeds) / len(speeds):.2f}/{max(speeds):.2f} kn")
        if self.rpm_intervals:
            rpms = [float(item["value"]) for item in self.rpm_intervals]
            self.log(f"RPM min/avg/max: {min(rpms):.2f}/{sum(rpms) / len(rpms):.2f}/{max(rpms):.2f}")

    def save_profiles_csv(self) -> None:
        if not self.speed_intervals and not self.rpm_intervals:
            messagebox.showinfo("Result Preview", "Generate profiles before saving CSV.")
            return
        path = filedialog.asksaveasfilename(
            title="Save Profile CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["profile", "series", "kind", "folder", "start_utc", "end_utc", "value", "source_file", "leg_index", "distance_nm"])
            for series in self.profile_series:
                for profile_name, intervals in (("speed", series["speed_intervals"]), ("rpm", series["rpm_intervals"])):
                    for item in intervals:
                        writer.writerow(
                            [
                                profile_name,
                                item.get("series", series.get("label", "")),
                                item.get("kind", series.get("kind", "")),
                                item.get("folder", series.get("folder", "")),
                                _utc_z(item["start"]),
                                _utc_z(item["end"]),
                                self._fmt_float(float(item["value"])),
                                item.get("source", ""),
                                item.get("leg_index", ""),
                                self._fmt_float(float(item.get("distance_nm", 0))),
                            ]
                        )
        self.log(f"Profile CSV saved: {path}")

    def _profile_limit(self) -> int:
        try:
            value = int(self.profile_limit_var.get())
        except ValueError:
            return 0
        return max(0, value)

    def _optimal_rtz_files(self, folder: Path) -> List[Path]:
        return sorted(folder.glob("*.rtz"), key=lambda path: (self._rtz_file_metadata(path).get("timestamp_sort") or path.name, path.name))

    def _rtz_file_metadata(self, path: Path) -> Dict[str, Any]:
        match = RTZ_FILE_NAME_RE.match(path.name)
        if not match:
            return {"timestamp_sort": path.name, "timestamp_dt": None}
        dt = datetime.strptime(match.group("date") + match.group("time"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return {
            "kind": match.group("kind"),
            "timestamp_sort": dt,
            "timestamp_dt": dt,
            "timestamp_label": f"{match.group('date')}_{match.group('time')}",
        }

    def _parse_profile_rtz(self, path: Path) -> List[Dict[str, Any]]:
        root = ET.parse(path).getroot()
        namespace = {"rtz": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
        waypoint_path = ".//rtz:waypoint" if namespace else ".//waypoint"
        position_path = "rtz:position" if namespace else "position"
        schedule_paths = (
            [".//rtz:scheduleElement", ".//rtz:sheduleElement"] if namespace else [".//scheduleElement", ".//sheduleElement"]
        )
        vo_path = ".//rtz:VOElement" if namespace else ".//VOElement"
        data_by_waypoint: Dict[str, Dict[str, Any]] = {}

        for schedule_path in schedule_paths:
            for item in root.findall(schedule_path, namespace):
                waypoint_id = item.attrib.get("waypointId")
                if not waypoint_id:
                    continue
                waypoint_data = data_by_waypoint.setdefault(waypoint_id, {})
                for key in ("speed", "rpm"):
                    if item.attrib.get(key):
                        waypoint_data[key] = float(item.attrib[key])
                for key in ("etd", "eta"):
                    if item.attrib.get(key):
                        waypoint_data[key] = item.attrib[key]

        for item in root.findall(vo_path, namespace):
            waypoint_id = item.attrib.get("waypointId")
            if not waypoint_id:
                continue
            waypoint_data = data_by_waypoint.setdefault(waypoint_id, {})
            for key in ("speed", "rpm"):
                if item.attrib.get(key):
                    waypoint_data[key] = float(item.attrib[key])

        points = []
        for waypoint in root.findall(waypoint_path, namespace):
            position = waypoint.find(position_path, namespace)
            if position is None:
                continue
            waypoint_id = waypoint.attrib.get("id") or str(len(points))
            leg = waypoint.find("rtz:leg", namespace) if namespace else waypoint.find("leg")
            properties = {
                "name": waypoint.attrib.get("name") or f"WP {waypoint_id}",
                "forceRhumbLine": leg is not None and leg.attrib.get("geometryType") == "Loxodrome",
            }
            properties.update(data_by_waypoint.get(waypoint_id, {}))
            points.append(
                {
                    "type": "Feature",
                    "properties": properties,
                    "geometry": {"type": "Point", "coordinates": [float(position.attrib["lon"]), float(position.attrib["lat"])]},
                }
            )
        _use_next_waypoint_speed_rpm(points)
        return points

    def _profile_pair_intervals(
        self,
        current: Dict[str, Any],
        next_record: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        start_time: datetime = current["time"]
        end_time: datetime = next_record["time"]
        interval_seconds = max(1.0, (end_time - start_time).total_seconds())
        segments = self._used_segments_until(current["points"], next_record["points"][0])
        if not segments:
            first_props = current["points"][0].get("properties", {})
            segments = [
                {
                    "leg_index": 0,
                    "distance_nm": 0.0,
                    "speed": first_props.get("speed"),
                    "rpm": first_props.get("rpm"),
                    "source": current["path"].name,
                }
            ]

        fallback_speed = self._average_value(segments, "speed") or 10.0
        raw_durations = []
        for segment in segments:
            speed = segment.get("speed") if isinstance(segment.get("speed"), (int, float)) and segment.get("speed") > 0 else fallback_speed
            raw_durations.append(max(0.0, float(segment.get("distance_nm", 0))) / float(speed) * 3600)
        total_raw = sum(raw_durations)
        if total_raw <= 0:
            raw_durations = [interval_seconds / len(segments)] * len(segments)
            total_raw = interval_seconds
        scale = interval_seconds / total_raw

        speed_intervals: List[Dict[str, Any]] = []
        rpm_intervals: List[Dict[str, Any]] = []
        cursor = start_time
        for index, segment in enumerate(segments):
            if index == len(segments) - 1:
                segment_end = end_time
            else:
                segment_end = cursor + timedelta(seconds=raw_durations[index] * scale)
            if segment_end <= cursor:
                continue
            base = {
                "start": cursor,
                "end": segment_end,
                "source": current["path"].name,
                "leg_index": segment.get("leg_index", index),
                "distance_nm": segment.get("distance_nm", 0.0),
            }
            if isinstance(segment.get("speed"), (int, float)):
                speed_intervals.append({**base, "value": float(segment["speed"])})
            if isinstance(segment.get("rpm"), (int, float)):
                rpm_intervals.append({**base, "value": float(segment["rpm"])})
            cursor = segment_end
        return speed_intervals, rpm_intervals

    def _used_segments_until(self, points: List[Dict[str, Any]], target: Dict[str, Any]) -> List[Dict[str, Any]]:
        if len(points) < 2:
            return []
        target_lat, target_lon = self._feature_lat_lng(target)
        best_index = 0
        best_fraction = 0.0
        best_score = float("inf")
        for index in range(len(points) - 1):
            start_lat, start_lon = self._feature_lat_lng(points[index])
            end_lat, end_lon = self._feature_lat_lng(points[index + 1])
            score, fraction = self._point_segment_projection(target_lat, target_lon, start_lat, start_lon, end_lat, end_lon)
            if score < best_score:
                best_score = score
                best_index = index
                best_fraction = fraction

        segments = []
        for index in range(best_index):
            segments.append(self._segment_profile(points, index, 1.0))
        if best_fraction > 1e-6:
            segments.append(self._segment_profile(points, best_index, best_fraction))
        return [segment for segment in segments if segment["distance_nm"] > 1e-6 or segment.get("speed") or segment.get("rpm")]

    def _segment_profile(self, points: List[Dict[str, Any]], index: int, fraction: float) -> Dict[str, Any]:
        start = points[index]
        end = points[index + 1]
        start_props = start.get("properties", {}) if isinstance(start.get("properties"), dict) else {}
        end_props = end.get("properties", {}) if isinstance(end.get("properties"), dict) else {}
        return {
            "leg_index": index,
            "distance_nm": self._distance_nm(start, end) * max(0.0, min(1.0, fraction)),
            "speed": start_props.get("speed") if isinstance(start_props.get("speed"), (int, float)) else end_props.get("speed"),
            "rpm": start_props.get("rpm") if isinstance(start_props.get("rpm"), (int, float)) else end_props.get("rpm"),
        }

    def _point_segment_projection(
        self,
        point_lat: float,
        point_lon: float,
        start_lat: float,
        start_lon: float,
        end_lat: float,
        end_lon: float,
    ) -> Tuple[float, float]:
        scale = math.cos(math.radians((point_lat + start_lat + end_lat) / 3))
        end_lon = start_lon + ((end_lon - start_lon + 180) % 360) - 180
        point_lon = start_lon + ((point_lon - start_lon + 180) % 360) - 180
        px, py = point_lon * scale, point_lat
        sx, sy = start_lon * scale, start_lat
        ex, ey = end_lon * scale, end_lat
        dx, dy = ex - sx, ey - sy
        if dx == 0 and dy == 0:
            return (px - sx) ** 2 + (py - sy) ** 2, 0.0
        fraction = max(0.0, min(1.0, ((px - sx) * dx + (py - sy) * dy) / (dx * dx + dy * dy)))
        closest_x = sx + fraction * dx
        closest_y = sy + fraction * dy
        return (px - closest_x) ** 2 + (py - closest_y) ** 2, fraction

    def _feature_lat_lng(self, feature: Dict[str, Any]) -> Tuple[float, float]:
        coordinates = feature["geometry"]["coordinates"]
        return float(coordinates[1]), float(coordinates[0])

    def _distance_nm(self, first: Dict[str, Any], second: Dict[str, Any]) -> float:
        lat1, lon1 = self._feature_lat_lng(first)
        lat2, lon2 = self._feature_lat_lng(second)
        radius_nm = 3440.065
        d_lat = math.radians(lat2 - lat1)
        d_lon = math.radians(((lon2 - lon1 + 180) % 360) - 180)
        a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
        return radius_nm * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))

    def _average_value(self, items: List[Dict[str, Any]], key: str) -> Optional[float]:
        values = [float(item[key]) for item in items if isinstance(item.get(key), (int, float))]
        return sum(values) / len(values) if values else None

    def _fmt_float(self, value: float) -> str:
        return f"{value:.6f}".rstrip("0").rstrip(".")


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
        self.api_key_var = tk.StringVar(value=_load_local_api_key(LOCAL_DEFAULTS))
        self.timeout_var = tk.StringVar(
            value=os.getenv("NAPA_TIMEOUT", LOCAL_DEFAULTS.get("timeout", str(DEFAULT_TIMEOUT_SECONDS)))
        )
        self.status_var = tk.StringVar(value="Ready.")

        self._build_widgets()
        self.set_endpoints(fallback_endpoints(), source="built-in fallback")
        self.after(300, self.reload_swagger_async)

    def current_api_key(self) -> str:
        api_key = self.api_key_var.get().strip()
        if api_key:
            return api_key
        api_key = _load_local_api_key(LOCAL_DEFAULTS)
        if api_key:
            self.api_key_var.set(api_key)
        return api_key

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

        if not hasattr(self, "rtz_batch_tab"):
            self.rtz_batch_tab = RtzBatchFrame(self.tabs)
            self.tabs.add(self.rtz_batch_tab, text="RTZ Batch")
        elif str(self.rtz_batch_tab) not in self.tabs.tabs():
            self.tabs.add(self.rtz_batch_tab, text="RTZ Batch")

        if not hasattr(self, "result_preview_tab"):
            self.result_preview_tab = ResultPreviewFrame(self.tabs)
            self.tabs.add(self.result_preview_tab, text="Result Preview")
        elif str(self.result_preview_tab) not in self.tabs.tabs():
            self.tabs.add(self.result_preview_tab, text="Result Preview")

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
