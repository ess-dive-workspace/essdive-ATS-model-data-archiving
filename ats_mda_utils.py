"""File operations used by the ATS MDA notebook. Requires Python 3.10+ and pandas."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    import pandas as pd
except ImportError as exc:
    raise ImportError("Install pandas in this notebook's kernel: %pip install pandas") from exc

logger = logging.getLogger("ats-mda")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    logger.addHandler(logging.StreamHandler(sys.stdout))

# FLMD release-v1.2.0, verified against commit dddb7776cb118332cb77f1d49803657894478d53.
FLMD_COLUMNS = ["file_name", "file_description", "standard", "data_dictionary_file_name",
                "file_version", "data_orientation", "header_rows", "column_or_row_name_position", "notes"]
DD_COLUMNS = ["column_or_row_name", "unit", "definition", "column_or_row_long_name", "data_type", "missing_value_code"]
ATS_STANDARD = "ESS-DIVE ATS MDA v1"
SYMBOL_URL = "https://raw.githubusercontent.com/amanzi/ats/refs/heads/master/docs/documentation/source/input_spec/symbol_table.org"
BLOCK = 8 * 1024 * 1024


@dataclass(frozen=True)
class Config:
    simulation_dir: Path
    data_pkg_dir: Path
    include_extensions: tuple[str, ...] = ("exo", "xml", "csv", "dat", "txt", "xmf", "h5", "out", "nc", "jpg", "png", "pdf", "sh", "py", "ipynb", "md")
    include_name_globs: tuple[str, ...] = ("slurm*",)
    run_tokens: tuple[str, ...] = ("run0", "run1", "run2")
    keep_checkpoint_token: str = "final"
    cleanup_mode: str = "preview"  # preview, apply, or skip
    # Explicit relative directories containing spinup/ensemble visualization to remove.
    visualization_run_dirs: tuple[str, ...] = ()
    obs_file_handle: str = "water_balance"
    obs_file_format: str = "csv"
    dd_file_name: str = "dd.csv"
    header_line_hint: int | None = None  # physical, 0-based line; same override for all matches
    header_probe_max_lines: int = 400
    write_new_csv: bool = False
    inplace: bool = False  # only staged copies; ignored unless write_new_csv=True
    overwrite_existing: bool = False  # differing copied/cleaned data; does not replace edited DD fields
    download_definitions: bool = True
    network_timeout_seconds: float = 15
    network_attempts: int = 2
    create_archives: bool = False
    archive_output_dir: Path | None = None  # defaults to a sibling <package>_archives directory
    split_size_gib: float | None = 5  # None keeps intact archives
    create_checksums: bool = True
    log_level: str = "INFO"  # DEBUG also shows every file and parsed header
    log_to_file: bool = True
    progress_seconds: float = 5


def human_bytes(value):
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.2f} {unit}"
        value /= 1024


def within(path, root):
    return path == root or root in path.parents


def paths(cfg, *, require_source=True):
    source = Path(cfg.simulation_dir).expanduser().resolve()
    stage = Path(cfg.data_pkg_dir).expanduser().resolve()
    if within(stage, source) or within(source, stage):
        raise ValueError("Source and staging directories must be separate, non-nested paths. Update simulation_dir/data_pkg_dir.")
    if require_source and not source.is_dir():
        raise FileNotFoundError(f"Source directory not found: {source}. Set simulation_dir to a completed ATS simulation.")
    if stage.exists() and not stage.is_dir():
        raise NotADirectoryError(f"Staging path is a file: {stage}")
    return source, stage


def validate_options(cfg):
    paths(cfg, require_source=False)
    if cfg.cleanup_mode not in {"preview", "apply", "skip"}:
        raise ValueError("cleanup_mode must be 'preview', 'apply', or 'skip'.")
    for name, values in (("include_extensions", cfg.include_extensions), ("include_name_globs", cfg.include_name_globs), ("run_tokens", cfg.run_tokens)):
        if isinstance(values, str) or any(not x or '/' in x or '\\' in x for x in values):
            raise ValueError(f"{name} must be a tuple of nonempty names, not paths or one string.")
    if not cfg.keep_checkpoint_token.strip():
        raise ValueError("keep_checkpoint_token cannot be empty; specify how final checkpoints are named.")
    if cfg.header_line_hint is not None and (not isinstance(cfg.header_line_hint, int) or cfg.header_line_hint < 0):
        raise ValueError("header_line_hint must be a nonnegative, 0-based line number or None.")
    if cfg.header_probe_max_lines < 1 or not math.isfinite(cfg.progress_seconds) or cfg.progress_seconds <= 0:
        raise ValueError("header_probe_max_lines and progress_seconds must be positive.")
    if not 1 <= cfg.network_attempts <= 5 or not 0 < cfg.network_timeout_seconds <= 120:
        raise ValueError("Use 1–5 network attempts and a timeout between 0 and 120 seconds.")
    if cfg.split_size_gib is not None and (not math.isfinite(cfg.split_size_gib) or cfg.split_size_gib * 1024**3 < 1):
        raise ValueError("split_size_gib must be positive (at least one byte), or None.")
    if cfg.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("log_level must be DEBUG, INFO, WARNING, or ERROR.")
    if ('/' in cfg.dd_file_name or '\\' in cfg.dd_file_name or
            not (cfg.dd_file_name == "dd.csv" or cfg.dd_file_name.endswith("_dd.csv"))):
        raise ValueError("dd_file_name must be dd.csv or a basename ending in _dd.csv.")
    for value in (cfg.obs_file_handle, cfg.obs_file_format):
        if not value or '/' in value or '\\' in value:
            raise ValueError("Observation handle/format must be nonempty filename patterns, not directory paths.")


def configure_logging(cfg, stream=None):
    """Install our own handlers so Jupyter/root logger settings cannot hide INFO logs."""
    validate_options(cfg)
    source, stage = paths(cfg, require_source=False)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(getattr(logging, cfg.log_level.upper()))
    formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
    console = logging.StreamHandler(stream if stream is not None else sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)
    if cfg.log_to_file:
        logfile = stage.parent / (stage.name + ".workflow.log")
        try:
            if logfile.is_symlink() or within(logfile.resolve(), source):
                raise ValueError(f"Log path must be outside the source: {logfile}")
            logfile.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(logfile, encoding="utf-8")
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            logger.info("Session log (appended, outside the archive): %s", logfile)
        except OSError as exc:
            logger.warning("Cannot write the session log: %s. Console logging remains available.", exc)
    logger.info("Logging ready: level=%s; progress interval=%gs", cfg.log_level.upper(), cfg.progress_seconds)


@contextmanager
def step(title):
    started = time.monotonic()
    logger.info("START | %s", title)
    try:
        yield
    except KeyboardInterrupt:
        logger.warning("INTERRUPTED | %s after %.1fs. Completed files remain; rerun after checking the log.", title, time.monotonic() - started)
        raise
    except Exception as exc:
        logger.error("FAILED | %s after %.1fs: %s", title, time.monotonic() - started, exc)
        logger.debug("Failure details", exc_info=True)
        raise
    else:
        logger.info("DONE | %s | %.1fs", title, time.monotonic() - started)


class Progress:
    """Report bounded updates by elapsed time, including work within a large file."""
    def __init__(self, label, total, interval=5, unit="bytes"):
        self.label, self.total, self.interval, self.unit = label, total, interval, unit
        self.done = 0
        self.start = self.last = time.monotonic()
        self.report(force=True)

    def advance(self, count, detail=""):
        self.done += count
        self.report(detail=detail)

    def report(self, *, force=False, detail=""):
        now = time.monotonic()
        if not force and now - self.last < self.interval:
            return
        self.last = now
        fmt = human_bytes if self.unit == "bytes" else str
        percent = min(100, 100 * self.done / self.total) if self.total else 100
        rate = self.done / max(now - self.start, .001)
        speed = f" | {human_bytes(rate)}/s" if self.unit == "bytes" and self.done else ""
        logger.info("%s | %.1f%% | %s / %s %s | %.1fs%s %s", self.label, percent,
                    fmt(self.done), fmt(self.total), "" if self.unit == "bytes" else self.unit,
                    now - self.start, speed, detail)


def scan_files(root, *, reject_symlinks=False, interval=5):
    """Walk without following links; report long directory scans."""
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"Directory not found: {root}. Run validation/copy first.")
    result, skipped = [], 0
    last = time.monotonic()
    def fail(error):
        raise error
    for folder, directories, names in os.walk(root, followlinks=False, onerror=fail):
        for name in list(directories):
            path = Path(folder) / name
            if path.is_symlink():
                if reject_symlinks:
                    raise ValueError(f"Staging/archive directory contains a symbolic link: {path}. Replace it with a real copy.")
                directories.remove(name)
                skipped += 1
            elif name in {".git", ".ipynb_checkpoints"}:
                directories.remove(name)
        for name in names:
            path = Path(folder) / name
            if path.is_symlink():
                if reject_symlinks:
                    raise ValueError(f"Staging/archive file is a symbolic link: {path}. Replace it with a real copy.")
                skipped += 1
            elif name.startswith(".ats-mda-"):
                skipped += 1  # leftovers from a killed kernel; never publish incomplete files
            elif path.is_file():
                if reject_symlinks and path.stat().st_nlink > 1:
                    raise ValueError(f'Staging/archive file has multiple hard links: {path}. Replace it with an independent copy.')
                result.append(path)
            else:
                raise ValueError(f"Unsupported special file: {path}")
        if time.monotonic() - last >= interval:
            logger.info("Scanning %s: %d files found; now in %s", root, len(result), folder)
            last = time.monotonic()
    if skipped:
        logger.warning("Skipped %d symbolic links or temporary files under %s; review whether any need a real copy.", skipped, root)
    logger.info("Inventory: %d files under %s", len(result), root)
    return sorted(result)


def staging(cfg):
    validate_options(cfg)
    _, stage = paths(cfg)
    scan_files(stage, reject_symlinks=True, interval=cfg.progress_seconds)
    return stage


def fingerprint(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


@contextmanager
def atomic_target(target):
    """Keep the previous output intact until the new file is fully written."""
    target = Path(target)
    if target.is_symlink():
        raise ValueError(f"Refusing to replace symbolic link: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".ats-mda-", dir=target.parent)
    os.close(fd)
    temp = Path(name)
    try:
        yield temp
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def save_csv(frame, path):
    with atomic_target(path) as temp:
        frame.to_csv(temp, index=False, encoding="utf-8")
    logger.info("Saved %s: %d rows, %d columns", path, len(frame), len(frame.columns))


def digest(path, progress=None):
    before = fingerprint(path)
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(BLOCK), b""):
            h.update(block)
            if progress:
                progress.advance(len(block), path.name)
    if fingerprint(path) != before:
        raise RuntimeError(f"File changed while being read: {path}. Stop other writers and retry.")
    return h.hexdigest()


def validate_workspace(cfg):
    with step("1/9 Validate paths and configuration"):
        validate_options(cfg)
        source, stage = paths(cfg)
        source_files = scan_files(source, interval=cfg.progress_seconds)
        if not source_files:
            raise ValueError(f"Source is empty: {source}. Select a completed simulation directory.")
        stage.mkdir(parents=True, exist_ok=True)
        existing = scan_files(stage, reject_symlinks=True, interval=cfg.progress_seconds)
        logger.info("Source: %s", source)
        logger.info("Staging: %s | %d existing files", stage, len(existing))
        logger.info("Free space at staging: %s", human_bytes(shutil.disk_usage(stage).free))
        logger.info("Copy overwrite: %s | cleanup: %s | dictionary: %s | archives: %s | checksums: %s",
                    cfg.overwrite_existing, cfg.cleanup_mode, cfg.dd_file_name, cfg.create_archives, cfg.create_checksums)
        if existing:
            logger.info("Rerun: identical files can be reused; differing data requires overwrite_existing=True. Existing metadata is preserved.")
        logger.info("Next: copy selected files. Source data will only be read.")


def copy_files(cfg):
    with step("2/9 Copy selected simulation files"):
        source, _ = paths(cfg)
        stage = staging(cfg)
        extensions = {'.' + x.lower().lstrip('.') for x in cfg.include_extensions}
        files = [p for p in scan_files(source, interval=cfg.progress_seconds)
                 if p.suffix.lower() in extensions or any(p.match(g) for g in cfg.include_name_globs)]
        if not files:
            raise ValueError("No source files match include_extensions/include_name_globs. Update the selection and rerun.")
        total = sum(p.stat().st_size for p in files)
        logger.info("Selected %d files, %s. Checking existing destinations before copying.", len(files), human_bytes(total))
        todo, reused, conflicts = [], 0, []
        for index, src in enumerate(files, 1):
            dst = stage / src.relative_to(source)
            if dst.exists():
                if not dst.is_file() or dst.is_symlink():
                    raise ValueError(f"Copy destination is not a regular file: {dst}")
                logger.info("Comparing existing file %d/%d: %s", index, len(files), dst.relative_to(stage))
                progress = Progress("Compare contents", src.stat().st_size + dst.stat().st_size, cfg.progress_seconds)
                same = digest(src, progress) == digest(dst, progress)
                progress.report(force=True)
                if same:
                    reused += 1
                    continue
                if not cfg.overwrite_existing:
                    conflicts.append(dst)
                    continue
            todo.append((src, dst))
        if conflicts:
            raise FileExistsError(f"{len(conflicts)} differing staging files; no files copied. First: {conflicts[0]}. Use a fresh staging directory or review overwrite_existing=True.")
        required = sum(src.stat().st_size for src, _ in todo)
        if required > shutil.disk_usage(stage).free:
            raise OSError(f"Insufficient free space for copying up to {human_bytes(required)}. Free space or choose another staging directory.")
        progress = Progress("Copy bytes", required, cfg.progress_seconds)
        for index, (src, dst) in enumerate(todo, 1):
            logger.debug("Copy %d/%d: %s", index, len(todo), src.relative_to(source))
            before = fingerprint(src)
            with atomic_target(dst) as temp:
                with src.open('rb') as inp, temp.open('wb') as out:
                    for chunk in iter(lambda: inp.read(BLOCK), b''):
                        out.write(chunk)
                        progress.advance(len(chunk), f"file {index}/{len(todo)}: {src.name}")
                if fingerprint(src) != before:
                    raise RuntimeError(f"Source changed during copy: {src}. Stop the simulation/writer and retry.")
                shutil.copystat(src, temp)
        progress.report(force=True)
        logger.info("Copy summary: %d copied, %d identical files reused. Next: review the cleanup preview.", len(todo), reused)
        return {"copied": len(todo), "reused": reused}


def cleanup_files(cfg):
    with step("3/9 Review or apply cleanup"):
        stage = staging(cfg)
        columns = ['file_name', 'reason', 'size_bytes', 'action']
        if cfg.cleanup_mode == 'skip':
            logger.info("Cleanup skipped by configuration; all staged files retained.")
            return pd.DataFrame(columns=columns)
        all_files = scan_files(stage, reject_symlinks=True, interval=cfg.progress_seconds)
        run_dirs = sorted({p for p in stage.rglob('*') if p.is_dir() and any(t in p.name for t in cfg.run_tokens)})
        candidates = {}
        checkpoint_dirs = {p.parent for p in all_files if p.match('checkpoint*.h5') and any(within(p, run) for run in run_dirs)}
        for folder in sorted(checkpoint_dirs):
            checkpoints = [p for p in all_files if p.parent == folder and p.match('checkpoint*.h5')]
            if not any(cfg.keep_checkpoint_token in p.name for p in checkpoints):
                logger.warning("No final checkpoint token '%s' in %s; retaining all its checkpoints.", cfg.keep_checkpoint_token, folder.relative_to(stage))
                continue
            for p in checkpoints:
                if cfg.keep_checkpoint_token not in p.name:
                    candidates[p] = 'non-final checkpoint'
        if not run_dirs:
            logger.warning("No directories match run_tokens=%s. Checkpoint cleanup skipped; update tokens if needed.", cfg.run_tokens)
        for relative in cfg.visualization_run_dirs:
            run = (stage / relative).resolve()
            if Path(relative).is_absolute() or run == stage or not within(run, stage):
                raise ValueError(f"visualization_run_dirs must name subdirectories inside staging: {relative}")
            if not run.is_dir():
                logger.warning("Visualization directory does not exist; skipped: %s", relative)
                continue
            for p in all_files:
                if within(p, run) and (p.suffix == '.xmf' or p.match('ats_vis_*.h5')):
                    candidates[p] = 'explicitly selected spinup/ensemble visualization'
        logger.info("Cleanup plan: %d files, %s; mode=%s. Production visualization is retained unless explicitly selected.",
                    len(candidates), human_bytes(sum(p.stat().st_size for p in candidates)), cfg.cleanup_mode)
        rows = []
        progress = Progress('Cleanup files', len(candidates), cfg.progress_seconds, 'files')
        for p, reason in sorted(candidates.items()):
            row = dict(file_name='./'+p.relative_to(stage).as_posix(), reason=reason,
                       size_bytes=p.stat().st_size, action='removed' if cfg.cleanup_mode == 'apply' else 'would remove')
            logger.debug('%s: %s (%s)', row['action'], row['file_name'], reason)
            if cfg.cleanup_mode == 'apply':
                p.unlink()
            rows.append(row)
            progress.advance(1, p.name)
        progress.report(force=True)
        if cfg.cleanup_mode == 'preview':
            logger.info("PREVIEW ONLY: no files deleted. Inspect the displayed plan, set cleanup_mode='apply', rerun configuration and this cell; or choose 'skip'.")
        return pd.DataFrame(rows, columns=columns)


def read_table(path, columns):
    if not path.exists():
        return None
    frame = pd.read_csv(path, keep_default_na=False, dtype=str)
    if list(frame.columns) != columns:
        raise ValueError(f"{path.name} has a different metadata schema. Back it up and migrate it to the v1.2.0 template before rerunning.")
    key = columns[0]
    if frame[key].duplicated().any() or frame[key].str.strip().eq('').any():
        raise ValueError(f"{path.name} contains blank or duplicate {key} values. Correct them before rerunning.")
    logger.info("Loaded existing metadata: %s (%d rows); preserving entered values.", path.name, len(frame))
    return frame


def file_key(root, path):
    return './' + path.relative_to(root).as_posix()


def inventory(cfg):
    """Refresh FLMD from disk, retaining descriptions and dictionary links."""
    stage = staging(cfg)
    target = stage / 'flmd.csv'
    old = read_table(target, FLMD_COLUMNS)
    previous = {} if old is None else old.set_index('file_name').to_dict('index')
    files = sorted(set(scan_files(stage, reject_symlinks=True, interval=cfg.progress_seconds)) | {target})
    patterns = {'water_balance': 'Model output CSV with water balance diagnostics.',
                'checkpoint': 'ATS checkpoint containing restart/state information.',
                'ats_vis': 'ATS visualization output.', 'slurm': 'Slurm scheduler output log.',
                '.xml': 'ATS model input/configuration file.', '.exo': 'Computational mesh file.',
                'LAI': 'Leaf Area Index input forcing.'}
    rows = []
    for path in files:
        if path.name == 'sha256sums.txt':
            continue  # generated last; do not modify FLMD after hashing
        name = file_key(stage, path)
        row = dict.fromkeys(FLMD_COLUMNS, '')
        row.update(file_name=name, standard=ATS_STANDARD)
        for token, desc in patterns.items():
            if token in path.name:
                row['file_description'] = desc
        if path.name == 'flmd.csv' or path.name.endswith('_flmd.csv'):
            row.update(file_description='File Level Metadata for this archive.', standard='ESS-DIVE FLMD v1',
                       data_orientation='horizontal', header_rows=1, column_or_row_name_position=1)
        elif path.name == 'dd.csv' or path.name.endswith('_dd.csv'):
            row.update(file_description='Data Dictionary for associated tabular files.', standard='ESS-DIVE FLMD v1',
                       data_orientation='horizontal', header_rows=1, column_or_row_name_position=1)
        row.update(previous.get(name, {}))
        rows.append(row)
    frame = pd.DataFrame(rows, columns=FLMD_COLUMNS)
    removed = set(previous) - set(frame.file_name)
    if removed:
        logger.warning('Removed %d inventory entries for files no longer present. First: %s', len(removed), sorted(removed)[0])
    save_csv(frame, target)
    blank = frame.file_description.str.strip().eq('').sum()
    logger.info('Inventory summary: %d files; %d descriptions need completion. Existing entries retained.', len(frame), blank)
    return frame


def generate_flmd(cfg):
    with step('4/9 Generate File Level Metadata'):
        return inventory(cfg)


def parse_header(line):
    if line.lstrip('\ufeff \t').startswith('#'):
        raise ValueError('The header is commented out. Prepare an uncommented header in the staged CSV first.')
    names = next(csv.reader([line.lstrip('\ufeff').rstrip('\r\n')], strict=True))
    if not names or any(not name.strip() for name in names) or len(set(names)) != len(names):
        raise ValueError('CSV column names must be nonempty and unique.')
    units = []
    for name in names:
        match = re.match(r'^(.*?)\s*\[(.*?)\]\s*$', name)
        units.append((match.group(2).strip() or 'N/A') if match else 'N/A')
    return names, units


def normalized_names(names):
    result = []
    for name in names:
        name = re.sub(r'\s*\[.*?\]\s*$', '', name).strip()
        name = re.sub(r'\s+', '_', name)
        name = re.sub(r'[^0-9A-Za-z_.-]', '', name)
        result.append(re.sub('_+', '_', name))
    if any(not x for x in result) or len(set(result)) != len(result):
        raise ValueError('Normalization creates empty or duplicate column names. Keep original names or rename manually.')
    return result


def inspect_header(path, cfg):
    limit = max(cfg.header_probe_max_lines, (cfg.header_line_hint or 0) + 1)
    with path.open(encoding='utf-8-sig', newline='') as stream:
        lines = []
        for _ in range(limit):
            line = stream.readline()
            if not line:
                break
            lines.append(line)
    index = cfg.header_line_hint
    if index is None:
        index = next((i for i, line in enumerate(lines)
                      if not line.lstrip().startswith('#') and ',' in line and '[' in line and ']' in line), None)
        if index is None:
            preview = '\n'.join(f'{i}: {line.rstrip()[:160]}' for i, line in enumerate(lines[:8]))
            raise ValueError(f'Cannot detect the header in {path}. Set header_line_hint to its 0-based physical line, or increase header_probe_max_lines. First lines:\n{preview}')
    if index >= len(lines):
        raise ValueError(f'header_line_hint={index} is outside {path} ({len(lines)} lines read).')
    names, units = parse_header(lines[index])
    position = sum(not line.lstrip().startswith('#') for line in lines[:index + 1])
    logger.debug('Header %s: physical line=%d, FLMD position=%d, names=%s', path, index, position, names)
    return names, units, index, position


def rewrite_header(path, target, index, names, cfg):
    """Stream a single header replacement; leave data values untouched."""
    before = fingerprint(path)
    with atomic_target(target) as temp:
        progress = Progress('Rewrite '+path.name, before[0], cfg.progress_seconds)
        with path.open('rb') as inp, temp.open('wb') as out:
            for line_index in range(index + 1):
                line = inp.readline()
                if line_index == index:
                    buffer = io.StringIO()
                    csv.writer(buffer, lineterminator='\n').writerow(names)
                    out.write(buffer.getvalue().encode('utf-8'))
                else:
                    out.write(line)
                progress.advance(len(line))
            for chunk in iter(lambda: inp.read(BLOCK), b''):
                out.write(chunk)
                progress.advance(len(chunk))
        if fingerprint(path) != before:
            raise RuntimeError(f'CSV changed while rewriting: {path}. Stop other writers and retry.')
        if target.exists() and target != path and not cfg.overwrite_existing:
            if digest(temp) != digest(target):
                raise FileExistsError(f'Differing cleaned output exists: {target}. Review it or set overwrite_existing=True.')
            logger.info('Cleaned output already matches: %s', target.name)
        progress.report(force=True)


def generate_dictionary(cfg):
    with step('5/9 Build a Data Dictionary and file associations'):
        stage = staging(cfg)
        flmd = read_table(stage/'flmd.csv', FLMD_COLUMNS)
        if flmd is None:
            raise RuntimeError('flmd.csv is missing. Run step 4 before generating a dictionary.')
        pattern = f'{cfg.obs_file_handle}*.{cfg.obs_file_format}'
        files = [p for p in scan_files(stage, reject_symlinks=True, interval=cfg.progress_seconds)
                 if p.match(pattern) and not p.name.endswith(('_dd.csv', '_flmd.csv'))
                 and (not p.name.endswith('_cleaned.csv') or '_cleaned' in cfg.obs_file_handle)
                 and p.name not in {'dd.csv', 'flmd.csv'}]
        if not files:
            logger.warning('No files match %s. No dictionary created. Update obs_file_handle/obs_file_format, or supply dictionaries for other tabular data before publication.', pattern)
            return None
        logger.info('Checking %d CSV headers for one shared schema: %s', len(files), pattern)
        headers, reference = {}, None
        progress = Progress('CSV header checks', len(files), cfg.progress_seconds, 'files')
        for path in files:
            try:
                names, units, index, position = inspect_header(path, cfg)
            except (ValueError, csv.Error, UnicodeError) as exc:
                raise ValueError(f'Header parsing failed for {path}: {exc}') from exc
            if reference is None:
                reference = names, units
                logger.info('Reference schema: %s | %d columns | physical header line %d', path.relative_to(stage), len(names), index)
            elif (names, units) != reference:
                raise ValueError(f'Different observation schema in {path}. Nothing rewritten. Narrow the observation pattern and use a separate *_dd.csv for each schema.')
            headers[path] = index, position, fingerprint(path)
            progress.advance(1, path.name)
        progress.report(force=True)
        names, units = reference
        output_names = normalized_names(names) if cfg.write_new_csv else names
        dd_path = stage/cfg.dd_file_name
        existing = read_table(dd_path, DD_COLUMNS)
        if existing is not None:
            if existing.column_or_row_name.tolist() != output_names or existing.unit.tolist() != units:
                raise ValueError(f'{cfg.dd_file_name} describes another schema. Use a new *_dd.csv name; existing metadata was not overwritten.')
            dd = existing
        else:
            dd = pd.DataFrame({column: (output_names if column == 'column_or_row_name' else units if column == 'unit' else ['']*len(names)) for column in DD_COLUMNS})
        targets = {p: p if not cfg.write_new_csv or cfg.inplace else p.with_name(p.stem+'_cleaned.csv') for p in files}
        if len(set(targets.values())) != len(targets):
            raise ValueError('Several inputs produce the same cleaned filename; select one extension/schema at a time.')
        if cfg.write_new_csv and not cfg.inplace and not cfg.overwrite_existing:
            collisions = [target for path, target in targets.items() if target.exists()]
            if collisions:
                raise FileExistsError(f'Cleaned output already exists: {collisions[0]}. No headers rewritten. Review it, select existing cleaned files separately, or explicitly set overwrite_existing=True.')
        for path, (index, position, original_state) in headers.items():
            if fingerprint(path) != original_state:
                raise RuntimeError(f'CSV changed after schema validation: {path}. Retry after stopping other writers.')
            if cfg.write_new_csv:
                rewrite_header(path, targets[path], index, output_names, cfg)
        save_csv(dd, dd_path)
        flmd = inventory(cfg)
        for path, (_, position, _) in headers.items():
            mask = flmd.file_name.eq(file_key(stage, targets[path]))
            previous = flmd.loc[mask, 'data_dictionary_file_name'].tolist()
            if any(x and x != cfg.dd_file_name for x in previous):
                logger.warning('Updating dictionary association for %s: %s -> %s', targets[path].name, previous, cfg.dd_file_name)
            flmd.loc[mask, ['data_dictionary_file_name', 'data_orientation', 'header_rows', 'column_or_row_name_position']] = [cfg.dd_file_name, 'horizontal', str(position), str(position)]
        save_csv(flmd, stage/'flmd.csv')
        logger.info('Dictionary summary: %s | %d variables | %d associated files. Review definitions, units, data types, and one actual missing-value code per column.', cfg.dd_file_name, len(dd), len(files))
        if cfg.write_new_csv and not cfg.inplace:
            logger.warning('Original CSVs are retained. Provide their own matching dictionary or remove unwanted originals before publishing.')
        return dd


def parse_symbol_table(text):
    rows = []
    for line in text.splitlines():
        if line.strip().startswith('|') and set(line.replace('|','').strip()) - set('-+ '):
            rows.append([x.strip() for x in line.strip().strip('|').split('|')])
    if not rows:
        raise ValueError('Downloaded symbol table contains no table rows.')
    header = [x.lower() for x in rows[0]]
    var = next((i for i,x in enumerate(header) if 'variable' in x and 'name' in x), None)
    desc = next((i for i,x in enumerate(header) if 'description' in x), None)
    if var is None or desc is None:
        raise ValueError('Symbol table columns are unrecognized; fill definitions manually.')
    return {row[var]: row[desc] for row in rows[1:] if len(row) > max(var,desc) and row[var] and row[desc]}


def enrich_dictionary(cfg):
    with step('6/9 Fill missing definitions (optional network lookup)'):
        stage = staging(cfg)
        target = stage/cfg.dd_file_name
        dd = read_table(target, DD_COLUMNS)
        if dd is None:
            logger.warning('%s does not exist. Run step 5 for your observation schema; definition lookup skipped.', target.name)
            return None
        missing = dd.definition.str.strip().eq('')
        if not missing.any() or not cfg.download_definitions:
            logger.info('Definition lookup skipped: %d blanks; download_definitions=%s. Existing definitions retained.', missing.sum(), cfg.download_definitions)
            return dd
        mapping = None
        for attempt in range(1, cfg.network_attempts+1):
            logger.info('Downloading ATS definitions: attempt %d/%d (timeout %gs)', attempt, cfg.network_attempts, cfg.network_timeout_seconds)
            try:
                with urllib.request.urlopen(SYMBOL_URL, timeout=cfg.network_timeout_seconds) as response:
                    mapping = parse_symbol_table(response.read().decode('utf-8'))
                break
            except (OSError, ValueError, UnicodeError) as exc:
                logger.warning('Definition lookup attempt %d failed: %s', attempt, exc)
        if mapping is None:
            logger.warning('Continuing without downloaded definitions. Your dictionary is intact; fill blanks manually or rerun this step later.')
            return dd
        keys = sorted(mapping, key=len, reverse=True)
        for index in dd.index[missing]:
            name = dd.at[index, 'column_or_row_name']
            # Use only the variable portion, not units, when suggesting a definition.
            name = re.sub(r'\s*\[.*?\]\s*$', '', name)
            for key in keys:
                if re.search(r'(?<![A-Za-z0-9])'+re.escape(key)+r'(?![A-Za-z0-9])', name):
                    dd.at[index, 'definition'] = mapping[key]
                    break
        save_csv(dd, target)
        remaining = dd.definition.str.strip().eq('').sum()
        logger.info('Definitions: %d newly suggested, %d still blank. Suggestions are heuristic; review scientific meaning.', missing.sum()-remaining, remaining)
        return dd


def review_package(cfg):
    with step('7/9 Review package completeness'):
        stage = staging(cfg)
        files = scan_files(stage, reject_symlinks=True, interval=cfg.progress_seconds)
        flmd = read_table(stage/'flmd.csv', FLMD_COLUMNS)
        issues = []
        def issue(file, message):
            issues.append({'file_name': file, 'needs_review': message})
        if flmd is None:
            issue('flmd.csv', 'Missing inventory: run step 4.')
        else:
            indexed = set(flmd.file_name)
            for path in files:
                if path.name != 'sha256sums.txt' and file_key(stage,path) not in indexed:
                    issue(file_key(stage,path), 'Not in FLMD: rerun step 4.')
            for row in flmd.to_dict('records'):
                relative = row['file_name'].removeprefix('./')
                data_path = (stage/relative).resolve()
                if not within(data_path, stage) or not data_path.is_file():
                    issue(row['file_name'], 'Inventory path is missing or outside the package.')
                if len(str(row['file_description']).strip()) < 10:
                    issue(row['file_name'], 'Complete a distinguishing description of at least 10 characters.')
                if data_path.suffix.lower() in {'.csv','.dat','.txt'} and not (data_path.name == 'flmd.csv' or data_path.name == 'dd.csv' or data_path.name.endswith(('_dd.csv','_flmd.csv'))):
                    if not row['data_dictionary_file_name']:
                        issue(row['file_name'], 'Check whether this text/tabular file needs a dictionary; none is associated.')
                dd_name = row['data_dictionary_file_name']
                if dd_name:
                    dd_path = stage/dd_name
                    if Path(dd_name).name != dd_name or not dd_path.is_file():
                        issue(row['file_name'], 'Associated dictionary is missing or not a basename.')
            for path in files:
                if path.name == 'dd.csv' or path.name.endswith('_dd.csv'):
                    dd = read_table(path, DD_COLUMNS)
                    for row in dd.to_dict('records'):
                        if not row['definition'].strip():
                            issue(path.name, 'Missing definition: '+row['column_or_row_name'])
                        if not row['unit'].strip():
                            issue(path.name, 'Missing unit (use N/A when inapplicable): '+row['column_or_row_name'])
        logger.info('Package: %d files | %s | %d items requiring review', len(files), human_bytes(sum(p.stat().st_size for p in files)), len(issues))
        if issues:
            logger.warning('The package still needs review. See the table below; this check does not certify scientific accuracy or ESS-DIVE compliance.')
        else:
            logger.info('Automated inventory checks found no issues. Still review scientific descriptions, dates, units, and run coverage before publication.')
        return pd.DataFrame(issues, columns=['file_name','needs_review'])


def archive_directory(cfg):
    source, stage = paths(cfg)
    output = (Path(cfg.archive_output_dir).expanduser() if cfg.archive_output_dir else stage.parent/(stage.name+'_archives')).resolve()
    for root in (source, stage):
        if within(output, root) or within(root, output):
            raise ValueError('archive_output_dir must be separate from, and not nested with, source or staging.')
    return output


def split_archive(path, max_bytes, cfg):
    """Write numbered parts with Python; verify their combined SHA256 before reporting success."""
    count = math.ceil(path.stat().st_size/max_bytes)
    width = max(3, len(str(count)))
    progress = Progress('Split '+path.name, path.stat().st_size, cfg.progress_seconds)
    parts = []
    expected = hashlib.sha256()
    with path.open('rb') as source:
        for index in range(1,count+1):
            target = path.with_name(path.name+f'.part{index:0{width}d}')
            if target.exists():
                raise FileExistsError(f'Split part already exists: {target}. Use a fresh archive output directory.')
            with atomic_target(target) as temp:
                with temp.open('wb') as output:
                    remaining = max_bytes
                    while remaining:
                        block = source.read(min(BLOCK,remaining))
                        if not block:
                            break
                        output.write(block)
                        expected.update(block)
                        remaining -= len(block)
                        progress.advance(len(block), f'part {index}/{count}')
            parts.append(target)
            logger.debug('Created split part %d/%d: %s', index, count, target.name)
    progress.report(force=True)
    verify = Progress('Verify split parts', path.stat().st_size, cfg.progress_seconds)
    actual = hashlib.sha256()
    for part in parts:
        with part.open('rb') as stream:
            for block in iter(lambda: stream.read(BLOCK), b''):
                actual.update(block)
                verify.advance(len(block), part.name)
    verify.report(force=True)
    if expected.digest()!=actual.digest() or sum(p.stat().st_size for p in parts)!=path.stat().st_size:
        raise RuntimeError(f'Split verification failed for {path}; retain the original archive and retry in a fresh directory.')
    logger.info('Verified %d parts reproduce %s exactly. Original archive retained.', len(parts), path.name)
    return parts


class CountingReader:
    def __init__(self, stream, progress, detail):
        self.stream, self.progress, self.detail = stream, progress, detail
    def read(self, count=-1):
        data = self.stream.read(count)
        self.progress.advance(len(data), self.detail)
        return data


def package_archives(cfg):
    with step('8/9 Optional archive packaging'):
        if not cfg.create_archives:
            logger.info('Packaging disabled (create_archives=False). Upload curated files directly, or enable this option after reviewing metadata.')
            return pd.DataFrame(columns=['directory','archive','size_bytes','parts'])
        stage = staging(cfg)
        output = archive_directory(cfg)
        if output.exists() and any(output.iterdir()):
            raise FileExistsError(f'Archive directory is not empty: {output}. Archives may be stale or incomplete. Choose a fresh archive_output_dir; to checksum existing reviewed archives, run only step 9.')
        files = scan_files(stage, reject_symlinks=True, interval=cfg.progress_seconds)
        if not (stage/'flmd.csv').exists():
            raise RuntimeError('Run metadata generation and review before packaging (flmd.csv is missing).')
        subdirs = sorted(p for p in stage.iterdir() if p.is_dir() and not p.name.startswith('.'))
        reserved = {p.name+'.tar.gz' for p in subdirs} | {'package-manifest.json'}
        for path in files:
            if path.parent == stage and (path.name in reserved or any(path.name.startswith(name+'.part') for name in reserved)):
                raise ValueError(f'Root file collides with a packaging output: {path.name}. Move or rename it before packaging.')
        output.mkdir(parents=True,exist_ok=True)
        estimated = sum(p.stat().st_size for p in files)
        logger.info('Packaging %s of uncompressed data into %s. Compression ratio is unknown; splitting also keeps the original archive.', human_bytes(estimated), output)
        if shutil.disk_usage(output).free < estimated:
            logger.warning('Free space is below uncompressed input size. Compression may fit, but consider a larger output location.')
        input_states = {file_key(stage,p): list(fingerprint(p)) for p in files if p.name != 'sha256sums.txt'}
        rows = []
        for index, directory in enumerate(subdirs,1):
            members = [p for p in files if within(p,directory)]
            target = output/(directory.name+'.tar.gz')
            logger.info('Archive %d/%d: %s | %d files',index,len(subdirs),target.name,len(members))
            progress = Progress('Compress '+directory.name,sum(p.stat().st_size for p in members),cfg.progress_seconds)
            with atomic_target(target) as temp:
                with tarfile.open(temp,'w:gz') as archive:
                    archive.add(directory,arcname=directory.name,recursive=False)
                    for path in members:
                        before = fingerprint(path)
                        info = archive.gettarinfo(str(path),arcname=path.relative_to(stage).as_posix())
                        if not info.isfile():
                            raise ValueError(f'Archive member is not a regular file: {path}')
                        with path.open('rb') as stream:
                            archive.addfile(info,CountingReader(stream,progress,path.name))
                        if fingerprint(path)!=before:
                            raise RuntimeError(f'File changed during packaging: {path}. Stop other writers and retry.')
            progress.report(force=True)
            logger.info('Created %s (%s compressed)',target.name,human_bytes(target.stat().st_size))
            parts = []
            if cfg.split_size_gib is not None and target.stat().st_size > int(cfg.split_size_gib*1024**3):
                parts=split_archive(target,int(cfg.split_size_gib*1024**3),cfg)
            rows.append(dict(directory=directory.name,archive=target.name,size_bytes=target.stat().st_size,parts='; '.join(p.name for p in parts)))
        # Root files (including metadata) must accompany the per-directory archives.
        root_files = [p for p in files if p.parent==stage and p.name!='sha256sums.txt']
        for path in root_files:
            progress=Progress('Copy root file '+path.name,path.stat().st_size,cfg.progress_seconds)
            before=fingerprint(path)
            with atomic_target(output/path.name) as temp:
                with path.open('rb') as source,temp.open('wb') as destination:
                    for block in iter(lambda:source.read(BLOCK),b''):
                        destination.write(block); progress.advance(len(block))
                if fingerprint(path)!=before:
                    raise RuntimeError(f'Root file changed while packaging: {path}')
            progress.report(force=True)
        current_states = {file_key(stage,p): list(fingerprint(p)) for p in scan_files(stage, reject_symlinks=True) if p.name != 'sha256sums.txt'}
        if current_states != input_states:
            raise RuntimeError('Staging changed during packaging. Build again in a fresh output directory.')
        manifest = {'staging_directory': str(stage), 'staging_files': input_states,
                    'output_files': {p.name: list(fingerprint(p)) for p in output.iterdir() if p.is_file()}}
        with atomic_target(output/'package-manifest.json') as temp:
            temp.write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
        logger.info('Packaged %d directories and %d root files. Original staging tree retained. Next: create checksums for the final payload.',len(rows),len(root_files))
        return pd.DataFrame(rows,columns=['directory','archive','size_bytes','parts'])


def write_checksum_manifest(root,cfg):
    files=[p for p in scan_files(root,reject_symlinks=True,interval=cfg.progress_seconds) if p!=root/'sha256sums.txt']
    target=root/'sha256sums.txt'
    progress=Progress('SHA256 '+root.name,sum(p.stat().st_size for p in files),cfg.progress_seconds)
    with atomic_target(target) as temp:
        with temp.open('w',encoding='utf-8',newline='\n') as out:
            for index,path in enumerate(files,1):
                relative=path.relative_to(root).as_posix()
                if '\n' in relative or '\r' in relative or '\\' in relative:
                    raise ValueError(f'Filename cannot be represented safely in the checksum manifest: {relative!r}. Rename it before publication.')
                logger.debug('SHA256 file %d/%d: %s',index,len(files),relative)
                out.write(f'{digest(path,progress)}  {relative}\n')
    progress.report(force=True)
    logger.info('Saved %s (%d checksums). Verify from this directory with: sha256sum -c sha256sums.txt',target,len(files))
    return target


def create_checksums(cfg):
    with step('9/9 Checksum the final files'):
        stage=staging(cfg)
        if not cfg.create_checksums:
            logger.info('Checksums disabled (create_checksums=False). No integrity manifest created.')
            return []
        targets=[stage]
        if cfg.create_archives:
            output=archive_directory(cfg)
            if not output.is_dir() or not any(output.iterdir()):
                raise RuntimeError('Archive output is missing. Run step 8 before checksumming the transfer payload.')
            marker = output/'package-manifest.json'
            if not marker.is_file():
                raise RuntimeError('Packaging did not complete (package-manifest.json is missing). Rebuild into a fresh archive directory before checksumming.')
            manifest = json.loads(marker.read_text(encoding='utf-8'))
            current = {file_key(stage,p): list(fingerprint(p)) for p in scan_files(stage, reject_symlinks=True) if p.name != 'sha256sums.txt'}
            actual_outputs = {p.name: list(fingerprint(p)) for p in scan_files(output, reject_symlinks=True) if p.name not in {'sha256sums.txt', 'package-manifest.json'}}
            if manifest.get('staging_directory') != str(stage) or manifest.get('staging_files') != current or manifest.get('output_files') != actual_outputs:
                raise RuntimeError('Staging or archive output changed after packaging. Rebuild into a fresh directory before generating final checksums.')
            targets.append(output)
        manifests=[write_checksum_manifest(root,cfg) for root in targets]
        logger.info('Checksums complete. Do not edit metadata or data after this step without regenerating the affected manifests and archives.')
        return manifests
