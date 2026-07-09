# napa_api_test

Tkinter GUI tester for the NAPA Voyage Optimization API endpoints enabled for this project.

Enabled endpoints:

- `POST /v1/performance-models/create`
- `POST /v1/performance-models/tune`
- `POST /v1/performance-models/tune-relative`
- `POST /v1/find-shortest-voyage`
- `POST /v1/find-optimal-voyage`
- `GET /v1/try-get-voyage`
- `POST /v2/calculate-voyage`

## Files

- `napa_api_gui.py` - GUI test program
- `requirements_napa_gui.txt` - Python dependency list
- `napa_gui_defaults.example.json` - optional local defaults template

## Run

```powershell
pip install -r requirements_napa_gui.txt
python napa_api_gui.py
```

The app uses `x-api-key` authentication. Enter the API key in the toolbar, set `NAPA_API_KEY`,
or put it in a local gitignored file:

```powershell
Set-Content napa_api_key.txt "your-api-key"
```

You can also use `.env` with `NAPA_API_KEY=your-api-key`, or set `api_key` in
`napa_gui_defaults.json`. These local secret files are ignored by `.gitignore`.

For async voyage endpoints such as `find-shortest-voyage`, the app follows the `202 Accepted`
`Location` header automatically when `Auto poll 202` is enabled. Completed JSON responses can be
rendered on the ABB-style interactive `3D Globe` in the `Map Preview` tab with `Show Map`.
Drag to rotate the globe and use the mouse wheel to zoom from `0.18x` to `10x`.

The `performance-models/create` sample is preset for IMO `9935208` using ABB project vessel
particulars: container ship, LOA `366 m`, breadth `51 m`.

## RTZ Batch

Use the `RTZ Batch` tab to run ABB-style continuous RTZ batch tests for:

- `POST /v1/find-shortest-voyage`
- `POST /v1/find-optimal-voyage`
- `POST /v2/calculate-voyage`

Select a planned RTZ, a folder of reference optimal RTZ files, and an output folder. The app uses
each reference optimal RTZ's first waypoint as the current position, cuts the remaining planned
route from that point, then sends a NAPA voyage request using the first/last remaining waypoints as
`fromCoordinates` and `toCoordinates` for v1 requests, or all remaining waypoints as `coordinates`
for `calculate-voyage`. The calculate voyage batch request automatically sets
`operationProfile` to `OptimalSpeed` and `maxCalculationIntervalDistance` to `92600` meters
(`50 * 1852`). It saves `.request.json`, `.accepted.json`, `.response.json`, and a result `.rtz`
when route geometry is returned.

## Result Preview

Use the `Result Preview` tab to inspect RTZ batch outputs. Add one or more result RTZ folders,
then click `Generate Profiles` to render ABB-style speed and RPM profile charts from timestamped
RTZ files. The tab auto-adds the active RTZ Batch output folder while a batch is running and can
export the generated profile intervals to CSV.

Optional local defaults:

```powershell
Copy-Item napa_gui_defaults.example.json napa_gui_defaults.json
```

Then edit `napa_gui_defaults.json`. Keep real API keys out of git.
