# CDS

## Setup

Use Linux or Ubuntu in WSL2 on Windows. Run the commands below in a Linux/WSL terminal.

Install these prerequisites first:

- Git
- Python 3.11 or newer (CI uses 3.13)
- Node.js 22 and npm

1. Clone the repository and enter the project folder:

   ```bash
   git clone https://github.com/PLevey001/capstone-CDS.git
   cd capstone-CDS
   ```

2. Install Python's virtual environment support and The Sleuth Kit (Ubuntu/Debian):

   ```bash
   sudo apt-get update
   sudo apt-get install python3-venv sleuthkit
   ```

   The Sleuth Kit is required for disk-image analysis and the full test suite.

3. Create a virtual environment and install the Python dependencies:

   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements-dev.txt
   ```

4. Install and build the frontend:

   ```bash
   cd frontend
   npm ci
   npm run build
   cd ..
   ```

5. Start the app from the project folder:

   ```bash
   python3 scripts/run.py
   ```

   Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Stop the server with Ctrl+C.

## Try the sample data

With the app running, open a second terminal in the project folder:

```bash
python3 scripts/import_demo.py
```

Refresh the browser and open the sample case. It contains a disk image, a text file, a CSV file, and a JSON file with invented data. Wait for all four sources to finish, then open one to review its results. Each run creates another sample case.

To generate the sample files without importing them, run `python3 scripts/make_demo.py`. Files are saved in `demo-evidence/`.

## Development

For frontend changes, stop the launcher and run the backend from the project folder:

```bash
.venv/bin/python -m uvicorn cds.main:app --host 127.0.0.1 --port 8000
```

In a second terminal:

```bash
cd frontend
npm run dev
```

Open the URL Vite prints (usually [http://127.0.0.1:5173](http://127.0.0.1:5173)). Frontend changes refresh automatically; restart the backend after Python changes. Run `npm run build` in `frontend/` again before returning to the launcher.

The [API reference](http://127.0.0.1:8000/docs) is available while the backend is running. To try requests that change data, click **Authorize** and enter `local-ui`.

| Folder | Contents |
| --- | --- |
| `cds/` | API, database, analysis, and job processing |
| `frontend/src/` | React interface |
| `scripts/` | Launcher and sample data utilities |
| `tests/` | Backend and integration tests |

## Next features

- **File extraction and recovery:** export files from disk images and recover deleted files when their contents are still available.
- **Artifact parsers:** extract browser history, Windows Registry records, and Windows event logs, including from files inside disk images.
- **Case timeline:** combine timestamps across evidence sources, with date filters and links back to the original artifacts.
- **Broader image support:** add E01 support and test NTFS and ext filesystems alongside the existing FAT sample images.

## Checks before a pull request

From the project folder:

```bash
.venv/bin/python -m pytest -q
cd frontend
npm run format:check
npm run build
cd ..
```

Use `npm run format` in `frontend/` to fix frontend formatting. Disk-image tests are skipped if The Sleuth Kit is missing. GitHub Actions runs the tests and frontend build on pushes and pull requests.

Pull the latest `main`, create a branch for your change, and open a pull request when the checks pass. Describe what changed and how you tested it. Commit dependency files and lockfile changes when adding packages.

## Local data and troubleshooting

- Cases and imported evidence are stored in `data/`. This folder and `demo-evidence/` are excluded from Git. Everyone's local cases are separate.
- Keep the server on `127.0.0.1`; this version has no login system. Run only one backend per data folder.
- If the launcher says to build the interface, rerun `npm ci` and `npm run build` in `frontend/`.
- If image analysis reports missing tools, check that `mmls` and `fls` are installed in the same Linux/WSL environment as the backend.
- If the workspace is already in use, check the original server terminal. Stop that server before starting another; do not delete its lock file.
