# napa_api_test

Tkinter GUI tester for the NAPA Voyage Optimization API.

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

Optional local defaults:

```powershell
Copy-Item napa_gui_defaults.example.json napa_gui_defaults.json
```

Then edit `napa_gui_defaults.json`. Keep real API keys out of git.
