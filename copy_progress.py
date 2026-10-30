import threading
from collections import deque
from dataclasses import dataclass

COST_FILE = 128 * 1024
COST_SKIP = 4 * 1024
COST_REMOTE_TASK = 64 * 1024 * 1024
BAR_RANGE = 10_000
TAIL_RANGE = BAR_RANGE // 10

STEP_PREPARE = "prepare"
STEP_HOOKS = "hooks"
STEP_COMPARE = "compare"
STEP_CONFIRM = "confirm"
STEP_DELETE = "delete"
STEP_SCAN = "scan"
STEP_COPY = "copy"
STEP_SYNC = "sync"
STEP_VERSIONS = "versions"

_MEASURED_STEPS = frozenset({STEP_SCAN, STEP_COPY})
_COUNTED_STEPS = frozenset({STEP_DELETE, STEP_VERSIONS})

_tls = threading.local()


def file_cost(size: int) -> int:
    return COST_FILE + (size if size > 0 else 0)


def set_stream_sink(sink) -> None:
    _tls.sink = sink


def stream_sink():
    return getattr(_tls, "sink", None)


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    mins, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


@dataclass(frozen=True, slots=True)
class Snapshot:
    step: str
    step_done: int
    step_total: int
    scan_complete: bool
    tail_reserved: bool
    files_done: int
    files_total: int
    work_done: int
    work_total: int
    copied: int
    skipped: int
    errors: int
    deleted: int
    size_copied: int
    size_skipped: int
    size_deleted: int
    bytes_copied: int
    bytes_total: int


class ProgressMeter:

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plans: dict[str, tuple[int, int, int]] = {}
        self._scan_complete = False
        self._tail_reserved = False
        self._step = STEP_PREPARE
        self._step_done = 0
        self._step_total = 0
        self._files_done = 0
        self._work_done = 0
        self._inflight_work = 0
        self._inflight_bytes = 0
        self._copied = 0
        self._skipped = 0
        self._errors = 0
        self._deleted = 0
        self._size_copied = 0
        self._size_skipped = 0
        self._size_deleted = 0

    def plan(self, key: str, files: int, work: int, size: int = 0) -> None:
        with self._lock:
            self._plans[key] = (files, work, size)

    def finish_scan(self) -> None:
        with self._lock:
            self._scan_complete = True
            if self._step == STEP_SCAN:
                self._step = STEP_COPY
                self._step_done = 0
                self._step_total = 0

    def reserve_tail(self) -> None:
        with self._lock:
            self._tail_reserved = True

    def set_step(self, step: str, done: int = 0, total: int = 0) -> None:
        with self._lock:
            self._step = step
            self._step_done = done
            self._step_total = total

    def step_progress(self, step: str, done: int, total: int) -> None:
        with self._lock:
            self._step = step
            self._step_done = done
            self._step_total = total

    def current_step(self) -> str:
        with self._lock:
            return self._step

    def add_inflight(self, work: int, size: int = 0) -> None:
        with self._lock:
            self._inflight_work += work
            self._inflight_bytes += size

    def record(self, ok, sk, er, de, *, work: int, release_work: int = 0, release_size: int = 0) -> None:
        size_ok = size_sk = size_de = 0
        for item in ok:
            if item[2] > 0:
                size_ok += item[2]
        for item in sk:
            if item[2] > 0:
                size_sk += item[2]
        for item in de:
            if item[2] > 0:
                size_de += item[2]
        with self._lock:
            self._files_done += len(ok) + len(sk) + len(er)
            self._work_done += work
            self._inflight_work = max(0, self._inflight_work - release_work)
            self._inflight_bytes = max(0, self._inflight_bytes - release_size)
            self._copied += len(ok)
            self._skipped += len(sk)
            self._errors += len(er)
            self._deleted += len(de)
            self._size_copied += size_ok
            self._size_skipped += size_sk
            self._size_deleted += size_de

    def files_total(self) -> int:
        with self._lock:
            return sum(p[0] for p in self._plans.values())

    def counts(self) -> tuple[int, int, int, int]:
        with self._lock:
            return self._copied, self._skipped, self._errors, self._deleted

    def snapshot(self) -> Snapshot:
        with self._lock:
            files_total = 0
            work_total = 0
            bytes_total = 0
            for files, work, size in self._plans.values():
                files_total += files
                work_total += work
                bytes_total += size
            work_done = self._work_done + self._inflight_work
            if files_total < self._files_done:
                files_total = self._files_done
            if work_total < work_done:
                work_total = work_done
            return Snapshot(
                step=self._step,
                step_done=self._step_done,
                step_total=self._step_total,
                scan_complete=self._scan_complete,
                tail_reserved=self._tail_reserved,
                files_done=self._files_done,
                files_total=files_total,
                work_done=work_done,
                work_total=work_total,
                copied=self._copied,
                skipped=self._skipped,
                errors=self._errors,
                deleted=self._deleted,
                size_copied=self._size_copied,
                size_skipped=self._size_skipped,
                size_deleted=self._size_deleted,
                bytes_copied=self._size_copied + self._inflight_bytes,
                bytes_total=bytes_total,
            )


@dataclass(frozen=True, slots=True)
class ProgressView:
    step: str
    determinate: bool
    value: int
    pct: "int | None"
    files_done: int
    files_total: int
    step_done: int
    step_total: int
    speed_bps: "float | None"
    files_per_s: "float | None"
    eta_s: "int | None"


class ProgressModel:
    WINDOW_S = 8.0
    MIN_SPAN_S = 1.0

    def __init__(self) -> None:
        self._samples: deque[tuple[float, int, int, int]] = deque()
        self._floor = 0
        self._last_step = ""

    def update(self, snap: Snapshot, now: float, *, finished: bool = False, cancelled: bool = False) -> ProgressView:
        step = snap.step
        if step != self._last_step:
            self._last_step = step
            self._floor = 0

        determinate = False
        value = 0
        pct: "int | None" = None

        base = BAR_RANGE - TAIL_RANGE if snap.tail_reserved else BAR_RANGE

        if finished:
            if cancelled:
                if snap.scan_complete and snap.work_total > 0:
                    value = min(base, snap.work_done * base // snap.work_total)
                    determinate = True
                    pct = min(99, value // 100)
            else:
                determinate = True
                value = BAR_RANGE
                pct = 100
        elif step == STEP_COPY and snap.scan_complete:
            determinate = True
            if snap.work_total > 0:
                value = min(base, snap.work_done * base // snap.work_total)
            else:
                value = base
            value = min(BAR_RANGE - 1, max(value, self._floor))
            self._floor = value
            pct = value // 100
        elif step in _COUNTED_STEPS and snap.step_total > 0:
            determinate = True
            fraction = min(snap.step_done, snap.step_total)
            if step == STEP_VERSIONS and snap.tail_reserved:
                value = base + fraction * TAIL_RANGE // snap.step_total
                value = min(BAR_RANGE - 1, max(value, self._floor))
            else:
                value = min(BAR_RANGE, fraction * BAR_RANGE // snap.step_total)
                value = max(value, self._floor)
            self._floor = value
            pct = value // 100

        speed = None
        per_s = None
        eta = None
        if step in _MEASURED_STEPS and not finished:
            samples = self._samples
            samples.append((now, snap.bytes_copied, snap.files_done, snap.work_done))
            while len(samples) > 2 and now - samples[0][0] > self.WINDOW_S:
                samples.popleft()
            t0, b0, f0, w0 = samples[0]
            span = now - t0
            if span >= self.MIN_SPAN_S:
                if snap.bytes_copied > b0:
                    speed = (snap.bytes_copied - b0) / span
                if snap.files_done > f0:
                    per_s = (snap.files_done - f0) / span
                if step == STEP_COPY and snap.scan_complete and w0 < snap.work_done < snap.work_total:
                    eta = int((snap.work_total - snap.work_done) * span / (snap.work_done - w0))
        elif step not in _MEASURED_STEPS:
            self._samples.clear()

        return ProgressView(
            step=step,
            determinate=determinate,
            value=value,
            pct=pct,
            files_done=snap.files_done,
            files_total=snap.files_total,
            step_done=snap.step_done,
            step_total=snap.step_total,
            speed_bps=speed,
            files_per_s=per_s,
            eta_s=eta,
        )
