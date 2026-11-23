# PyManager

PyManager is an independent, local dashboard for finding and managing Python installations on Windows, macOS, and Linux. It shows versions, interpreter paths, and installation sources, including uv, pyenv, Conda, and system installations. Install and uninstall options depend on the platform and how each interpreter was installed.

## Run locally

From the repository root, run `run.bat` on Windows or `sh run.sh` on macOS or Linux. These launchers can install uv and a managed Python runtime if needed. Open the URL printed in the terminal.

To use an existing Python interpreter and install only the app's dependencies, create a virtual environment and run the commands for your platform.

**Windows (PowerShell)**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

**macOS and Linux**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

The server listens on `127.0.0.1` and picks an available port starting at 5000. Set `PYMANAGER_PORT` to choose a specific port. Keep the app on loopback because it can change local Python installations.

Paths added from the dashboard are saved locally in `pymanager.config.json`, which Git ignores.

## Contributing and license

See the [contributor guide](CONTRIBUTING.md) for bug reports and pull requests. Project code is available under the [MIT License](LICENSE). [Third-party notices](THIRD_PARTY_NOTICES.md) cover external dependencies and services.
