# napa_api_test

Tkinter GUI tester for the NAPA Voyage Optimization API endpoints enabled for this project.

Enabled endpoints:

- `POST /v1/performance-models/tune`
- `POST /v1/performance-models/tune-relative`
- `POST /v1/find-shortest-voyage`
- `POST /v1/find-optimal-voyage`
- `GET /v1/try-get-voyage`
- `POST /v2/calculate-voyage-plan`

## Files

- `napa_api_gui.py` - GUI test program
- `requirements_napa_gui.txt` - Python dependency list
- `napa_gui_defaults.example.json` - optional local defaults template

## Run

```powershell
pip install -r requirements_napa_gui.txt
python napa_api_gui.py
```

The app uses `x-api-key` authentication. Enter the API key in the toolbar, or set `NAPA_API_KEY`.

For async voyage endpoints such as `find-shortest-voyage`, the app follows the `202 Accepted`
`Location` header automatically when `Auto poll 202` is enabled. Completed JSON responses can be
rendered in the `Map Preview` tab with `Show Map`.

Optional local defaults:

```powershell
Copy-Item napa_gui_defaults.example.json napa_gui_defaults.json
```

Then edit `napa_gui_defaults.json`. Keep real API keys out of git.
