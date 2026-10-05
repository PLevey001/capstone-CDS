# CDS

CDS is a local capstone prototype for collecting evidence into cases, hashing imported originals, and saving reproducible analysis runs. It inventories raw FAT, NTFS and ext4 images through The Sleuth Kit, extracts known files, and conditionally recovers deleted files whose metadata/content survive.

Supported content includes Chrome/Chromium and Firefox history snapshots, Windows Registry keys/values, and structured EVTX events. Inside raw images, allocated files at recognized profile/system paths are candidates for those same parsers. Matching browser WAL/SHM sidecars are included inside images; separately uploaded journals are preserved but **not** attached to standalone database snapshots. Text, CSV and JSON uploads also receive bounded basic metadata inspection.

The interface offers file inventory, saved records, coverage diagnostics, analysis history, downloads and CSV/JSON exports. One paginated UTC timeline combines filesystem MAC times, browser visits, Registry key last-write times and event creation times. Registry values do not receive invented timestamps. Reanalysis preserves historical results; downloads use only the current saved run. Coverage describes what was examined and any gaps, independently of whether a job finished.

The [combined acceptance case and numbered walkthrough](docs/acceptance.md) define the demonstrated scope and limits. **E01 is deliberately excluded:** the local TSK 4.12.1/libewf 20140816 combination segfaults on EWF input, so no EWF analysis path was added. Segmented uploads, unallocated carving, encryption, Registry log replay and rendered EVTX messages are not supported. This is not a certified forensic tool or a claim of unlimited image scale.

## Setup

Use Linux or Ubuntu in WSL2 on Windows. Run these commands in a Linux/WSL terminal. Install Git, Python, Node.js 22 and npm first. **CI tests Python 3.11 and 3.13**; it does not test every newer Python release. Use one of those versions for setup.

1. Clone the repository and enter the project folder:

   ```bash
   git clone https://github.com/PLevey001/capstone-CDS.git
   cd capstone-CDS
   ```

2. Install Python virtual environment support, The Sleuth Kit, and the fixture tools (Ubuntu/Debian):

   ```bash
   sudo apt-get update
   sudo apt-get install python3-venv sleuthkit e2fsprogs ntfs-3g build-essential
   ```

   Disk-image analysis needs `mmls`, `fls` and `icat`. The acceptance generator and full filesystem tests additionally use `mke2fs`, `debugfs`, `e2fsck`, `mkntfs`, a C compiler and the ntfs-3g shared library. Fixtures require no mounts or privileged device access.

3. Create a virtual environment and install the pinned Python dependencies:

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

   Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Stop with Ctrl+C. The launcher uses the project's virtual environment. Setup verification and the restrictions of this session are recorded in [acceptance verification limits](docs/acceptance.md#exclusions-and-verification-limits); a fresh network install was blocked here, not silently treated as verified.

## Try the combined case

With the app running, use a second terminal in the project folder:

```bash
.venv/bin/python scripts/import_demo.py --acceptance
```

Expect a new **Combined acceptance case** with **14 sources and 469 records**, plus a source-side manifest under `demo-evidence/acceptance-*/sources/`. The command waits for analysis and verifies hashes and counts. Follow the [numbered walkthrough](docs/acceptance.md#numbered-walkthrough) for timeline ranges, saved-record provenance, extraction, qualified recovery, exports, history and cleanup. Failures are deliberately included in the case.

The original small USB demo remains available with `python3 scripts/import_demo.py`. It imports a FAT image, text, CSV and JSON. Generate those files alone with `python3 scripts/make_demo.py`. All samples are invented; no personal browser profiles or Windows logs are used.

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

Open the URL Vite prints (usually [http://127.0.0.1:5173](http://127.0.0.1:5173)). Frontend changes refresh automatically; restart the backend after Python changes. Run `npm run build` in `frontend/` before returning to the launcher.

The [API reference](http://127.0.0.1:8000/docs) is available while the backend runs. To try requests that change data, click **Authorize** and enter `local-ui`. This header is a local-browser guard, not a login credential.

| Folder | Contents |
| --- | --- |
| `cds/` | API, database, bounded parsers and workers |
| `frontend/src/` | React interface |
| `scripts/` | Launcher, existing demo builders/importer and performance check |
| `tests/` | Backend/integration tests and independent known-answer builders |
| `docs/` | Acceptance walkthrough, scope and measured performance |

## Checks before a pull request

```bash
.venv/bin/python -m pytest -q --require-image-tests
cd frontend
npm run format:check
npm run build
cd ..
```

`--require-image-tests` checks required image tools/fixtures and rejects skips in the image and combined-acceptance modules. Without that flag, image tests may skip when their native tools are unavailable. The `image-tests` CI job runs the entire backend suite with this flag on Python 3.11 and 3.13. The separate `validate` job retains the backend suite, frontend formatting/build and Playwright browser tests on Python 3.13 and Node 22.

For browser tests, install Playwright's pinned browser builds and run:

```bash
cd frontend
npx playwright install --with-deps chromium firefox
npm run test:e2e
cd ..
```

Playwright starts an isolated synthetic workspace. Use `npm run format` to fix frontend formatting. The [performance check](docs/acceptance.md#performance) records actual local timings rather than enforcing a hardware-sensitive latency threshold on shared CI runners.

## Local data and troubleshooting

- Cases and imported originals live in `data/`. This folder and `demo-evidence/` are ignored by Git. Set `CDS_DATA_DIR` to a separate directory for disposable demonstrations. Back up code and data together before an upgrade.
- Keep the server on `127.0.0.1`; there is no login system. Run one backend per data directory.
- If the launcher asks for a frontend build, run `npm ci` and `npm run build` in `frontend/`.
- If image analysis reports missing tools, ensure `mmls`, `fls` and `icat` are installed in the same Linux/WSL environment as the backend. Acceptance generation also names any missing fixture tools.
- If a workspace is already in use, stop its original server before restarting. Do not delete the lock file. Restart requeues interrupted analyses and removes their disposable content working sets while preserving saved results.
- Deleting a case removes that case's saved data and owned source/work files; it does not delete the separately generated demo fixtures. Deletion waits for active jobs/uploads. Failed filesystem cleanup remains queued for retry on restart.
