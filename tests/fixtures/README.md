# Filesystem fixture evidence

These fixtures demonstrate **NTFS 3.1** and **ext4 with the feature set below** through CDS's existing raw-image analysis and saved-record paths. They do not establish ext2, ext3, arbitrary NTFS/ext4 configurations, alternate data streams, compression, encryption, or mounted-kernel deletion behavior.

Run the tests from the repository root:

```sh
.venv/bin/python -m pytest -q tests/test_filesystems.py
```

Generate inspectable images, source files, and a JSON manifest for each variant in a new directory:

```sh
.venv/bin/python -m tests.filesystem_fixtures --output /tmp/cds-filesystem-fixtures
```

The builders live in `tests/filesystem_fixtures.py`, keeping `scripts/make_demo.py`'s no-external-tools promise intact. Everything is synthetic and generated locally. No downloaded images, personal browser profiles, root privileges, loop devices, FUSE, or OS mounts are involved. Generation refuses an existing output directory.

## Demonstrated variants

Each filesystem has four layouts: unpartitioned and one DOS/MBR partition, each using 512- and 4096-byte logical sectors. Volumes are 64 MiB; partitions begin at byte 1,048,576 (LBA 2048 or 256 respectively).

| Filesystem | Geometry and contents |
| --- | --- |
| NTFS 3.1 | `mkntfs -s 512` or `-s 4096`, 4096-byte clusters, correct partition-start BPB field. Resident small and deleted files, a 256 KiB nonresident file, Unicode name, nested directories. No user alternate data streams. |
| ext4 | 4096-byte blocks, 256-byte inodes, journal, extents, 64-bit block groups, metadata checksums. Ordinary and Unicode files, nested directories, a 1 MiB sparse file with only two allocated data blocks, hard link, inline symbolic link, two explicit deletion states. |

The exact ext4 feature set is `has_journal,ext_attr,resize_inode,dir_index,filetype,extent,64bit,flex_bg,sparse_super,large_file,huge_file,dir_nlink,extra_isize,metadata_csum`. It is supplied explicitly instead of inheriting the host's default feature selection. `e2fsck -f -n` must pass after construction. ext4 does not store a device sector-size field; its 4096-byte blocks, volume length, and partition byte alignment are valid for either tested logical sector size. MBR LBAs are calculated in that layout's actual sectors. Tests inspect boot/superblock geometry and partition lengths, rather than just passing a different `-b` to TSK.

Both browser databases are present in **all eight images**. Chrome has a committed second visit only in its WAL, with matching WAL/SHM sidecars; Firefox has three visits in a clean main database. Tests prove discovery, exact extraction, journal inclusion, UTC visit times, URLs, parser identity, saved artifact/run provenance, hashes, and removal of disposable SQLite copies. Source-image hashes must remain unchanged after analysis and downloads.

## Known answers and reproducibility

Each `manifest.json` is written **before image construction and analysis**. It records original paths, source content files, SHA-256 values, sizes, all four filesystem timestamps, deliberate deletion states, partition layout, logical sector size, filesystem geometry, browser visit answers, exclusions, and tool versions. Source files remain beside the manifest, including the original bytes of deliberately deleted files. No expectation is derived from `fls`, `icat`, `fsstat`, or CDS output.

All tested file/directory times are Unix `1700000000` (2023-11-14 22:13:20 UTC). Browser visit times independently include microseconds. The builders and verification commands set their own timezone and locale. Rebuild tests use the same source bytes under Honolulu and Tokyo host timezone settings, checking manifests, extracted content, and timestamps.

The reproducibility guarantee is manifest-verifiable content, **not identical whole-image hashes**: formatter identifiers and SQLite WAL salts can vary. A new browser source set can have different WAL hashes with the same specified visits; its manifest records the actual source-side bytes before insertion into the image. Formatter-created system entries are outside the known-file manifest.

`populate_ntfs.c` is a small fixture-only writer because `ntfscp` alone cannot create the required directory tree or unlink a file. It uses opaque ntfs-3g handles, creates directories before their files, and deletes `/deleted.txt` after the last allocation. Its executable supplies a fixed realtime clock to ntfs-3g, leaving monotonic time and the test process untouched. No private NTFS structure layout or manual MFT editing is used. The public API declarations come from upstream [dir.h](https://github.com/tuxera/ntfs-3g/blob/2022.10.3/include/ntfs-3g/dir.h), [attrib.h](https://github.com/tuxera/ntfs-3g/blob/2022.10.3/include/ntfs-3g/attrib.h), [inode.h](https://github.com/tuxera/ntfs-3g/blob/2022.10.3/include/ntfs-3g/inode.h), and [volume.h](https://github.com/tuxera/ntfs-3g/blob/2022.10.3/include/ntfs-3g/volume.h). Only a C compiler and the runtime shared library are needed; development headers are not required.

## Observed recovery and link behavior

- NTFS `/deleted.txt` retains its resident data. CDS marks it deleted and `icat -r` returns its exact original bytes in all four layouts. The tested file metadata addresses are `inode-type-id` triples and pass the shared metadata-address predicate through saved extraction targets.
- ext4 `debugfs rm` alone retains the deleted inode's size and extents. `/deleted-retained.txt` consequently recovers exactly. This is a debugfs-specific retained-metadata case, not evidence that ordinary ext4 deletion is generally recoverable.
- ext4 `/deleted.txt` is punched, set to size zero, then unlinked, explicitly modeling the cleared-size/extent state. CDS lists it as deleted with size zero. `icat -r` succeeds with **zero bytes**, not the known original contents. Download eligibility still means an address can be attempted, not that old content survives. Inventory coverage says file contents were not examined; this stage adds no carving or general ext4 recovery promise. Both deletion states are constructed without a mounted kernel.
- A hard link shares the original inode and extracts the same bytes. An inline symlink is listed as `/symlink.txt -> small.txt`, with `l/l` mode. On TSK 4.12.1, `icat` returns nine NUL bytes for that nine-character target. The narrow runtime fix blocks symbolic-link downloads with an explicit reason, including previously saved entries carrying that mode. It keeps the target text visible in inventory and does not follow the link. List, detail, and download endpoint behavior are tested.
- NTFS `fls` also emits `$FILE_NAME` attribute rows. These remain inventory metadata; only the intended `$DATA` paths become browser candidates. Unpartitioned volumes retain CDS's existing `partition_layout_unknown`/partial overall coverage even when filesystem and browser steps succeed. Partitioned fixtures have complete coverage.

## Tools and CI

Local validation on October 5, 2026 used:

| Tool | Version |
| --- | --- |
| Sleuth Kit (`fls`, `icat`, `mmls`, `fsstat`, `istat`) | 4.12.1; Debian `4.12.1+dfsg-3` |
| e2fsprogs (`mke2fs`, `debugfs`, `e2fsck`) | 1.47.2; Debian `1.47.2-3+b12` |
| ntfs-3g / `mkntfs` / `libntfs-3g.so.89` | 2022.10.3; Debian `1:2022.10.3-5+deb13u2` |
| C compiler | GCC 14.2.0; Debian `14.2.0-19` |
| Python | 3.13.5 |
| SQLite (Python module) | 3.46.1 |

Each generated manifest also captures the current tool output and SQLite version. CI logs installed package and tool versions; its results bound the versions demonstrated there rather than implying every distribution version works.

The designated `image-tests` job installs `sleuthkit`, `e2fsprogs`, `ntfs-3g`, and `build-essential`, then runs the full backend suite with `--require-image-tests`. The session fails if a required tool or fixture source is missing, and any skipped test makes the job fail. Outside that job, local skips name the missing tools precisely. Generated fixture/manifest failures are assertions, never skips.

Local results: **26 filesystem integration/rebuild cases passed without skips**, plus the symbolic-link API regression; **300 backend tests passed without skips**, both normally and with `--require-image-tests`. Frontend formatting and build passed. The missing-tool, missing-fixture, and unexpected-skip failure paths were checked locally. Hosted CI has not been run from this uncommitted work.
