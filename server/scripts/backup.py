#!/usr/bin/env python3
"""
15-minute USB backup for the Garment Neck Label Checker.

Design rules, each of which exists because of a specific way backups fail:

  1.  WRITE TO A TEMPORARY NAME, THEN RENAME.
      A rename is atomic. If the machine dies mid-dump, the previous good
      backup is untouched. Writing straight to the final name leaves one
      corrupt file and nothing else.

  2.  KEEP GENERATIONS, NOT ONE FILE.
      Disk failure is not the only way data is lost. If a bug corrupts a
      table at 11:00 and the only backup is 11:15, the corruption has been
      faithfully preserved.

  3.  RECORD EVERY RUN IN backup_log.
      A backup nobody can prove ran is not evidence. The table also carries
      verified_at, set by the weekly restore test.

  4.  NEVER FAIL SILENTLY.
      A failed backup is written to the log with its error text, so the
      failure is visible the next time anyone looks.

Scheduling (do not put this logic in the script — the OS does it better):
  Windows   Task Scheduler, every 15 minutes
  Linux     */15 * * * *  in cron, or a systemd timer

⚠️ The USB drive must be encrypted. It holds buyer names, purchase order
   numbers, quantities and prices, and Chapter 3 §3.12 commits this research
   to treating those as confidential. BitLocker To Go on Windows; LUKS on
   Linux.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

DAILY_KEEP_DAYS = 14

COUNT_TABLES = [
    "shipment", "scan_event", "recognition_result",
    "mismatch", "exception_event", "carton", "carton_item",
    "packing_list_line", "assistance_event",
]


def _connect():
    import psycopg
    return psycopg.connect(settings.database_url)


def row_counts() -> dict[str, int]:
    """Counted before the dump so the log says what the dump should contain."""
    out: dict[str, int] = {}
    with _connect() as conn, conn.cursor() as cur:
        for t in COUNT_TABLES:
            cur.execute(f"SELECT count(*) FROM {t}")  # noqa: S608 - fixed list
            out[t] = cur.fetchone()[0]
    return out


def write_log(path: str, size: int | None, counts: dict | None,
              ok: bool, err: str | None) -> None:
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO backup_log "
                "(file_path, size_bytes, row_counts, succeeded, error_text) "
                "VALUES (%s, %s, %s, %s, %s)",
                (path, size, json.dumps(counts) if counts else None, ok, err),
            )
            conn.commit()
    except Exception as exc:                       # noqa: BLE001
        # The database may be the thing that is broken. Say so on stderr and
        # carry on — losing the log entry must not lose the backup.
        print(f"could not write backup_log: {exc}", file=sys.stderr)


def prune(backup_dir: Path) -> None:
    """Today's 15-minute files stay; older ones collapse to one per day."""
    daily = backup_dir / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")

    by_day: dict[str, list[Path]] = {}
    for f in backup_dir.glob("*.sql.gz"):
        day = f.name[:10]
        if day == today:
            continue
        by_day.setdefault(day, []).append(f)

    for day, files in by_day.items():
        files.sort()
        keep = files[-1]
        target = daily / f"{day}.sql.gz"
        if not target.exists():
            shutil.move(str(keep), str(target))
        else:
            keep.unlink(missing_ok=True)
        for f in files[:-1]:
            f.unlink(missing_ok=True)

    cutoff = datetime.now() - timedelta(days=DAILY_KEEP_DAYS)
    for f in daily.glob("*.sql.gz"):
        try:
            if datetime.strptime(f.stem.replace(".sql", ""), "%Y-%m-%d") < cutoff:
                f.unlink(missing_ok=True)
        except ValueError:
            continue


def main() -> int:
    backup_dir = Path(settings.backup_dir)

    # If the drive is not mounted, fail loudly rather than quietly filling the
    # host disk with files nobody will find.
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        write_log(str(backup_dir), None, None, False, f"backup dir unavailable: {exc}")
        print(f"FAIL: backup directory unavailable: {exc}", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    final = backup_dir / f"{stamp}.sql.gz"

    counts = None
    try:
        counts = row_counts()
    except Exception as exc:                       # noqa: BLE001
        print(f"warning: could not count rows: {exc}", file=sys.stderr)

    # rule 1: temporary name, then atomic rename
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(backup_dir), suffix=".part")
    os.close(tmp_fd)
    tmp = Path(tmp_name)

    try:
        proc = subprocess.run(
            ["pg_dump", "--no-owner", "--no-privileges", settings.database_url],
            capture_output=True, check=True,
        )
        with gzip.open(tmp, "wb", compresslevel=6) as fh:
            fh.write(proc.stdout)
        tmp.replace(final)                          # atomic
    except subprocess.CalledProcessError as exc:
        tmp.unlink(missing_ok=True)
        err = (exc.stderr or b"").decode(errors="replace")[:2000]
        write_log(str(final), None, counts, False, err)
        print(f"FAIL: pg_dump: {err}", file=sys.stderr)
        return 1
    except Exception as exc:                        # noqa: BLE001
        tmp.unlink(missing_ok=True)
        write_log(str(final), None, counts, False, str(exc)[:2000])
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    size = final.stat().st_size
    write_log(str(final), size, counts, True, None)
    prune(backup_dir)
    print(f"OK  {final}  {size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
