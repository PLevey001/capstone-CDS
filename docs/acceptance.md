# Stage 10 combined acceptance

This is a reproducible, synthetic local prototype case. Expected records, timestamps, file sizes and SHA-256 hashes are constructed from the inputs in `scripts/make_demo.py::make_acceptance`, before CDS analyzes them. The builder reuses the existing browser, FAT, Stage 6 filesystem, Registry and EVTX builders. It never uses parser output as its oracle. WAL salts and filesystem-generated metadata can vary between builds; logical answers and known file bytes remain the same.

## Build and import

Complete the README setup, including the image fixture tools, and start the app. In a second terminal, run this one command from the checkout:

```bash
.venv/bin/python scripts/import_demo.py --acceptance
```

It creates a fresh `demo-evidence/acceptance-*/sources/` directory, builds all sources, imports them into one case, waits for analysis, and checks every source hash and record count. Expect **“Verified 14 sources and 469 records”**. It saves `manifest.json` (expected facts) and `import.json` (actual case/source IDs). Each invocation creates another case; it never replaces an existing acceptance fixture.

To build without a server, use `.venv/bin/python scripts/make_demo.py --acceptance --output /tmp/cds-acceptance-sources` with a new directory. Do not upload the source-tree/reference files as additional evidence. The importer chooses the 14 manifest sources for you.

| Source | Records | Meaning |
| --- | ---: | --- |
| Chrome-History.sqlite | 60 | Standalone base snapshot |
| Firefox-places.sqlite | 60 | Standalone base snapshot; unvisited/bookmark-only places excluded |
| Four separate WAL/SHM uploads | 0 | Original bytes preserved; standalone uploads do **not** attach journals |
| NTUSER.DAT | 12 | Three keys and nine typed values; only keys have timestamps |
| Application.evtx | 3 | Structured events, including an offset-bearing SystemTime |
| fat.img | 0 | MBR FAT12 inventory, known file and retained deleted bytes |
| ntfs.img | 197 | MBR NTFS: 61 visits per browser with WAL, 60 visits in a profile with its journal deliberately omitted, 12 Registry and 3 EVTX records |
| ext4.img | 137 | Bare ext4: 61 visits per browser with WAL, 12 Registry and 3 EVTX records |
| unknown.sqlite | 0 | Unsupported browser schema |
| damaged.evtx | 0 | Truncated declared chunk; parser failure retained in coverage |
| incomplete.img | 0 | Truncated FAT image; filesystem unreadable |

The imported originals total **139,035,252 bytes**. Allow additional disk space for source trees, reference images, uploaded copies and saved results (roughly half a GiB for a new demonstration).

## Numbered walkthrough

The expectations below are asserted through the real analysis workers and application API in `tests/test_acceptance.py`. **The interactive steps were not executed in this session:** no computer-use browser was available, the importer shell could not open sockets, and the Playwright server could not bind its port. The importer’s generation/upload/poll/verification path was exercised with TestClient HTTP transport. These are explicit verification limits, not a claim of a completed live UI rehearsal.

1. **Open the combined case.** Refresh the app and select **Stage 10 · Combined acceptance**. Expect 14 sources and 469 records. Open each source to compare its SHA-256 to `manifest.json`. A completed job is not a statement of complete coverage: the unknown schema, damaged log and standalone journals have qualified coverage. The incomplete image has a failed analysis.

2. **Inspect inventory and coverage.** Open `ntfs.img`, inspect its partition at sector **2048**, and search the file inventory for `History`, `places.sqlite`, `NTUSER.DAT` and `Application.evtx`. The two ordinary browser candidates each include matching WAL and SHM provenance and 61 visits. `MissingJournal/History` has only 60 visits and explicitly says base database only, with no sidecars in the available inventory. It must not borrow Default's journal. Open `ext4.img`: sector **0** is a readable bare volume, but its partition layout is unknown, so whole-image coverage is partial.

3. **Show the shared UTC timeline.** Close the drawer, select **Timeline**, and apply From and Through **2023-11-14 22:13:20** (UTC). Both ends are inclusive. The known control events include browser visits, Registry root-key last-write times, event creation times, and filesystem MAC times at exactly the same instant. This tie spans several pages; use Next. The primary labels are **Browser visit**, **Registry key last-write**, **Event creation time**, and the appropriate **Created/Modified/Accessed/Metadata changed** label. Registry values must not appear as key-write events.

4. **Show interleaving and boundaries.** Apply From **22:14:20** and Through **22:14:21**, on the same date. Expect filesystem access and Registry key-write events at `22:14:20.000000`, EVTX creation at `.000001`, then browser visits at `.123456`. Set Through to **22:14:20** to exclude both fractional groups. The UI inputs have one-second resolution; exact microsecond boundaries and equivalent `Z`/`-05:00` ranges are exercised by the API tests, not by inventing a finer UI control. Clear the range afterward.

5. **Follow a record to its image bytes.** Open a browser/Registry/EVTX timeline event from `ntfs.img`. The drawer pins the saved run and record. Check its kind, timestamp, parser and original fields; choose **Open source artifact**. Expect the same run, original image path, partition sector 2048, metadata address, and content hash. Download that artifact and compare its SHA-256 to the corresponding `filesystems.ntfs.img.files` entry in the manifest. Browser downloads are the original main-database bytes; they are not rewritten snapshots with WAL merged in.

6. **Extract and recover.** In `fat.img`, download `REPORT.TXT` and recover `_ECRET.TXT`; their bytes match the manifest. In NTFS, `/small.txt` and resident `/deleted.txt` have known bytes. In ext4, `/deleted-retained.txt` recovers its retained extents, while `/deleted.txt` produces an empty file after its size/extents were cleared. Download eligibility is not proof that original deleted bytes survive. The ext4 symlink and directories show an explanation instead of a download; their API download requests return 422.

7. **Export all matching records.** Open standalone Chrome Records: the first page contains 50 of 60 records. Export matching JSON or CSV with an empty search and expect all **60**, not just the page. Search `%_` and expect one matching visit. JSON keeps `=2+2`; CSV prefixes it with an apostrophe. With `ntfs.img` selected, an empty search exports all **197** records in that run, across its parsers. Exports include source name/hash, source ID, run ID/number, parser, source artifact and coverage. The case header's export remains the **file inventory** export. For all **469 parsed records** across latest runs, open `/api/cases/<case_id>/records/export` using the ID in `import.json`; there is no new all-case-record button.

8. **Reanalyze and restart.** Select an NTFS record, note its ID/run, and choose **Analyze again**. Keep the original run selected in Analysis history. Its saved records, fields and coverage remain unchanged; old-run downloads explain that downloads require the current saved run. Select the latest run to download. Stop and restart the app using the same data directory, reopen the case, and select the old run again. API acceptance tests repeat actual analysis for every source and verify old exports exactly after a new application/coordinator starts.

9. **Inspect understandable failures.** Open `unknown.sqlite` (unsupported schema), `damaged.evtx` (declared chunk truncated/missing), and `incomplete.img` (unreadable filesystem). Hashing still succeeds. Compare these with the qualified missing-journal and bare-volume scopes; none may claim that unexamined records or missing content were searched successfully.

10. **Demonstrate owned cleanup.** Create a second disposable case and import `NTUSER.DAT` into it. After all jobs finish, delete only the combined acceptance case through **Delete case**. Expect the second case, its original and its 12 records to remain. Interrupted-work cleanup is deliberately tested automatically, rather than asking a presenter to kill a worker: the test leaves an active run with a WAL working set and a symlink to another case's work, starts a new coordinator, confirms requeue/cleanup, preserves all old results and the other case, then verifies deletion and restart of pending file cleanup.

## Limits and how acceptance exercises them

These bounds describe the prototype; export completeness means all **saved matching** records, not all possible evidence. Exports do not have a separate case-wide row cap and can become large across many runs/sources. The API provides latest results across a case or one selected historical run. Browser/Registry/EVTX inputs can stop before all records are saved, and their coverage accompanies the export.

| Bound | Default | Verification |
| --- | ---: | --- |
| Upload | 4 GiB/source, three active uploads | Reduced 100-byte rejection removes partial uploads; importer uses three upload threads |
| Inventory | 20,000 artifacts/source; 128 partition offsets | Mixed NTFS case at a reduced artifact budget; injected 129-partition discovery guard |
| Command output / stderr | 16 MiB total / 64 KiB stderr | Real subprocess output rejection at a reduced total and the actual stderr ceiling |
| Extraction/download | 64 MiB | Reduced-size rejection; existing extraction suite covers incomplete/nonzero-exit output |
| Tool / content candidate time | 90 seconds, or shorter configured tool timeout | Real short subprocess deadline; reduced content deadline on the combined image |
| Image content stage | 32 candidates, 300 seconds, 2 GiB cumulative extraction | Reduced candidate, time and byte budgets on the mixed-family NTFS source |
| Candidate working set | 64 MiB including browser sidecars | Reduced combined-image limit; actual standalone 64 MiB and 64 MiB + 1 probes |
| Saved records / payload | 10,000 / 16 MiB **per source run**, shared by all its parsers | Reduced mixed-family budgets; actual default count and payload probes |
| Record text | 4,096 characters/field | Shortened-field diagnostics at reduced limit; default-width payload probe |
| Timeline / record page | 100 / 50 defaults; 200 maximum | Multi-page ties and exports; out-of-range API requests rejected |

The older file-metadata parsers also retain their existing 64 KiB text sample, 4 MiB JSON, 50 JSON keys, 100 CSV columns and 256-character metadata bounds. Their tests remain in the full suite; the combined case does not add text/CSV/JSON interpretation.

## Performance

Run `.venv/bin/python scripts/benchmark_acceptance.py --output /tmp/cds-performance.json`. The benchmark also runs as an acceptance test and prints the report path with `pytest -q -s tests/test_acceptance.py::test_performance_fixture_shape_and_worker_bounds`.

See [the measured JSON report](acceptance-performance.json) for samples, fixture sizes, versions and worker probes. The API workload is **five synthetic saved sources**, 12,500 artifacts (50,000 filesystem events) and 10,000 parsed-record rows spanning both browsers, Registry and EVTX. It is a query benchmark, not a claim that five large real images were parsed. Each request is warmed once, then measured seven times end to end through FastAPI TestClient, including response serialization. It excludes network/browser rendering. The current implementation scans and sorts the full saved timeline before returning a page.

Measured October 5, 2026 on an **AMD Ryzen AI 9 HX 370**, 24 logical CPUs, 32,125,672 KiB reported RAM (30.64 GiB), Linux 7.1.13, Python 3.13.5 and SQLite 3.46.1. TSK was 4.12.1, e2fsprogs 1.47.2, ntfs-3g 2022.10.3, python-registry 1.3.1 and python-evtx 0.8.1. The saved fixture database occupied **7,966,720 bytes**.

| Warm request | Median | Maximum of 7 | Response |
| --- | ---: | ---: | ---: |
| First page | 0.1182 s | 0.1189 s | 39,556 bytes / 100 events |
| Filtered first page | **0.1089 s** | **0.1321 s** | 39,909 bytes / 100 of 21,605 matches |
| Later page | 0.1179 s | 0.1196 s | 40,077 bytes / 100 events |

The real worker probe saved 10,000 of 10,001 visits in 0.1820 s with `record_limit`. The default 16 MiB payload bound stopped at 1,920 long-title records in 0.1295 s. A 64 MiB input made a 64 MiB temporary copy and finished in 0.2031 s; a 64 MiB + 1 input was rejected before copying in 0.0311 s. Every temporary copy was removed. Observed worker peak RSS was **209,124 KiB** and parser-child peak RSS **100,788 KiB**.

The local target is a warm filtered first page under one second. No cache or new persistence model was introduced. Worker and parser RSS are separate process high-water marks, not a combined concurrent peak or an enforced memory ceiling. Temporary-copy measurements for the standalone immutable parser are taken when its subprocess starts, when the complete copy exists, and checked for removal afterward. The 2 GiB cumulative image allowance and full 32-candidate load were **not** stress-tested at their defaults; their shared-budget behavior is tested at reduced limits.

## Verification results

The required backend command passed **456 tests, zero skipped** in 41.70 seconds (439 baseline tests plus 17 acceptance tests). Frontend formatting and production build passed. `git diff --check` passed. The fresh Python environment also passed the importer integration test against the clean source copy. The existing Playwright suite could not start its server in this sandbox, so no browser-test success is claimed.

The combined review found browser-only wording in the shared Records drawer/list and the timeline's secondary action text. Registry/EVTX rows were described as visits and incorrectly displayed missing-URL fields. Those labels now use record terminology, retain browser fields for actual browser visits, and keep existing navigation. No new timestamp-ordering, parser-budget, export, or ownership defect was found by the combined assertions. The fixed builder remains linear; no configurable fixture framework or generic failure-scenario engine was introduced.

## Exclusions and verification limits

- **E01 is deliberately excluded.** On this machine TSK 4.12.1 links libewf 20140816 and segfaults on both single and segmented EWF input. Stage 9 was skipped for this reason. No E01 fixture or `-i ewf` path was added; E01 support is not claimed.
- Segmented uploads, standalone WAL replay, new parsers, new record families and new frontend features are outside this stage. The only UI changes correct browser-only wording in the shared record views.
- No carving of unallocated space, encrypted/compressed content, NTFS alternate streams, ext2/ext3, Registry transaction-log replay/deleted-cell recovery, EVTX message rendering or carved-event recovery is established. Path discovery is limited to the shipped candidate rules. This is not forensic certification or exhaustive filesystem/browser/Windows version compatibility.
- The clean-source check used an archive of `eaad057` plus this working diff, with no existing data or generated frontend. A fresh Python virtual environment installed the pinned requirements from cached wheels. Network installation failed because sandbox DNS was unavailable. `npm ci` also could not fetch uncached Vite 6.4.3; the clean-source build used the existing installed dependency tree. A completely fresh network dependency installation and interactive walkthrough remain unverified here. Local Python was 3.13.5 and Node was 26.7.0; CI uses Python 3.11/3.13 and Node 22.
