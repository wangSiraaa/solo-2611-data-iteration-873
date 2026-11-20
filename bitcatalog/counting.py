"""Split-piece counting and shift payroll over the observation warehouse.

A product may declare *counters* (see :mod:`bitcatalog.catalog`).  A counter's
value is ``(high << low_width) | low``; the two halves live in frames of
possibly different messages, and each half's frame carries its own settlement
identity, commit flag, ordering sequence and loom identity.

Everything below is derived from *locally generated frames only*.  No network,
no third-party data.

Assembly
========

Fragments are keyed by ``(loom, settlement, product, counter)``.  Arrival order
is irrelevant: each fragment names its own bus/frame_id, so a late fragment
simply fills its slot.

* A fragment only counts when its commit flag is set.  An uncommitted fragment
  is a *trial read* and can never become a confirmed count.
* A repeated fragment carrying the same value is a harmless re-delivery; one
  carrying a different value is a **conflict** and blocks the reading.
* If one committed half arrives while the other is still missing (or its trial
  read never commits) the reading is kept as ``incomplete`` (缺件) naming the
  missing segment.

Epochs and readings
===================

Confirmed readings are ordered per ``(loom, product, counter)`` by the device's
unique increasing settlement sequence -- never by arrival or event time -- so
two fragments at the same instant still order deterministically.  The first
reading opens an epoch.  A reading that regresses, or whose gap from the
previous reading would cross the full scale ``1 << (low+high)``, needs a new
epoch: the preceding epoch is closed and the interval is flagged
``split_required`` until it is split (paid only in pieces that stay inside one
epoch).

A *crossed half* is a reading where one segment is present and the other is
borrowed from the next reading in the same epoch (a shift boundary whose high
half has not arrived yet).  Borrowing is explicit and such a reading is **never**
accepted as a payroll boundary -- otherwise a missing high half would be
silently filled with the next reading's high bits and fabricate a value.

Payroll
=======

A shift group's pieces for a shift are the endpoint delta minus any boundary
deltas that belong to the other group.  The only legal boundary is a *complete*
confirmed reading.  If a boundary is incomplete the day's total may be recorded
but the per-group split is withheld (never guessed).

Integer fen are split by agreed integer work points: floor first, then the
leftover fen go one at a time to the members with the largest fractional
remainder, ties by employee number (largest-remainder / Hamilton method).

Recalculation
=============

Every issued payslip records the pin (reading identity) of each boundary it used
and every boundary reading can be cited by many payslips.  :class:`PayrollLedger`
keeps a ``pin -> (container, group)`` index so that when late proof is archived
(new committed fragments arrive, changing some reading from incomplete to
complete) only the containers that cited the affected readings are re-opened,
and the groups that must recompute are reported exactly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable, Mapping, Sequence

from .catalog import CatalogProduct, CompiledCounter, CompiledSegment
from .warehouse import ObservationWarehouse

# -- fragment / reading status ----------------------------------------------

FRAGMENT_CONFLICT = "conflict"
FRAGMENT_DUPLICATE = "duplicate"
FRAGMENT_UNCOMMITTED = "uncommitted"

READING_CONFIRMED = "confirmed"
READING_INCOMPLETE = "incomplete"
READING_CONFLICT = "conflict"
READING_UNCOMMITTED = "uncommitted"

# -- group / slip / container states ---------------------------------------

GROUP_SETTLED = "settled"
GROUP_HELD = "held"

# -- provenance ------------------------------------------------------------

PROVENANCE_INITIAL = "initial"
PROVENANCE_ARCHIVED = "archived"


class CountingError(ValueError):
    def __init__(self, message: str, *, code: str = "counting_error", **detail: Any):
        super().__init__(message)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _frame_identity(frame: Mapping[str, Any]) -> str:
    return str(frame["frame_event_id"])


def _frame_segment(
    product: CatalogProduct, counter: CompiledCounter, frame: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Read a fragment's fields from one frame, or ``None`` if not this counter."""

    bus = frame.get("bus")
    frame_id = int(frame.get("frame_id", -1))
    key = (bus, frame_id)
    if key not in counter.message_keys:
        return None
    segment: str | None = None
    segdef: CompiledSegment | None = None
    if key == counter.low.field.message_key:
        segment, segdef = "low", counter.low
    elif key == counter.high.field.message_key:
        segment, segdef = "high", counter.high
    else:
        return None
    msg = product.message(bus, frame_id)  # type: ignore[arg-type]
    decoded = msg.decode(bytes.fromhex(str(frame["payload_hex"])))
    by_name = decoded.by_name()
    if segdef.field.signal not in by_name:
        return None
    raw = by_name[segdef.field.signal].raw
    commit = by_name.get(segdef.commit.name)
    settlement = by_name.get(segdef.settlement.name)
    if settlement is None:
        return None
    sequence = by_name.get(segdef.sequence.name).raw if segdef.sequence is not None and segdef.sequence.name in by_name else None
    if counter.loom_constant is not None:
        loom = counter.loom_constant
    else:
        assert segdef.loom is not None
        loom_sig = by_name.get(segdef.loom.name)
        loom = str(loom_sig.raw) if loom_sig is not None else None
    return {
        "segment": segment,
        "bus": bus,
        "frame_id": frame_id,
        "frame_event_id": _frame_identity(frame),
        "raw": raw,
        "effective": raw & ((1 << segdef.width) - 1),
        "committed": bool(commit is not None and commit.raw != 0),
        "occurred_at_ns": int(frame.get("occurred_at_ns", 0)),
        "settlement": str(settlement.raw),
        "sequence": sequence,
        "loom": loom,
    }


# ---------------------------------------------------------------------------
# value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fragment:
    segment: str
    bus: str
    frame_id: int
    frame_event_id: str
    raw: int
    effective: int
    committed: bool
    arrival: int
    occurred_at_ns: int
    settlement: str
    sequence: int | None
    loom: str

    def as_json(self) -> dict[str, Any]:
        return {
            "segment": self.segment,
            "bus": self.bus,
            "frame_id": self.frame_id,
            "frame_event_id": self.frame_event_id,
            "raw": self.raw,
            "effective": self.effective,
            "committed": self.committed,
            "arrival": self.arrival,
            "occurred_at_ns": self.occurred_at_ns,
            "settlement": self.settlement,
            "sequence": self.sequence,
            "loom": self.loom,
        }


@dataclass(frozen=True)
class MissedFragment:
    fragment: Fragment
    reason: str

    def as_json(self) -> dict[str, Any]:
        return {"reason": self.reason, **self.fragment.as_json()}


@dataclass(frozen=True)
class CounterPin:
    """The identity of one reading, stable enough to index and re-issue."""

    counter: str
    loom: str
    settlement: str
    product_sha: str
    epoch: int
    value: int | None
    status: str
    revision: int
    complete: bool
    sequence: int | None = None
    missing_segment: str | None = None
    conflict: tuple[Any, ...] = ()

    @property
    def pin_id(self) -> str:
        return _sha(
            {
                "counter": self.counter,
                "loom": self.loom,
                "settlement": self.settlement,
                "product_sha": self.product_sha,
                "epoch": self.epoch,
                "value": self.value,
                "status": self.status,
                "revision": self.revision,
                "complete": self.complete,
                "missing_segment": self.missing_segment,
            }
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "pin_id": self.pin_id,
            "counter": self.counter,
            "loom": self.loom,
            "settlement": self.settlement,
            "product_sha256": self.product_sha,
            "epoch": self.epoch,
            "value": self.value,
            "value_hex": None if self.value is None else f"0x{self.value:04X}",
            "status": self.status,
            "revision": self.revision,
            "complete": self.complete,
            "sequence": self.sequence,
            "missing_segment": self.missing_segment,
        }


@dataclass(frozen=True)
class Reading:
    """An assembled settlement identity before epoch placement."""

    counter: str
    loom: str
    settlement: str
    product_sha: str
    low: int | None
    high: int | None
    low_width: int
    status: str
    revision: int
    segments: tuple[Fragment, ...] = ()
    missing_segment: str | None = None
    conflict: tuple[Any, ...] = ()
    sequence: int | None = None
    first_arrival: int = 0

    @property
    def complete(self) -> bool:
        return self.status == READING_CONFIRMED and self.low is not None and self.high is not None

    @property
    def value(self) -> int | None:
        if self.low is None or self.high is None:
            return None
        return (self.high << self.low_width) | self.low

    def pin(self, epoch: int) -> CounterPin:
        return CounterPin(
            counter=self.counter,
            loom=self.loom,
            settlement=self.settlement,
            product_sha=self.product_sha,
            epoch=epoch,
            value=self.value if self.complete else None,
            status=self.status,
            revision=self.revision,
            complete=self.complete,
            sequence=self.sequence,
            missing_segment=self.missing_segment,
            conflict=self.conflict,
        )


@dataclass(frozen=True)
class EpochReading:
    """A reading placed in an epoch, optionally shown as a crossed half."""

    reading: Reading
    epoch: int
    crossed: bool = False
    borrowed_segment: str | None = None
    borrowed_from: str | None = None

    @property
    def settlement(self) -> str:
        return self.reading.settlement

    @property
    def loom(self) -> str:
        return self.reading.loom

    @property
    def complete(self) -> bool:
        return self.reading.complete and not self.crossed

    @property
    def value(self) -> int | None:
        if self.crossed:
            return None
        return self.reading.value

    @property
    def low(self) -> int | None:
        return self.reading.low

    @property
    def high(self) -> int | None:
        return self.reading.high

    @property
    def status(self) -> str:
        return self.reading.status

    @property
    def sequence(self) -> int | None:
        return self.reading.sequence

    @property
    def pin(self) -> CounterPin:
        return CounterPin(
            counter=self.reading.counter,
            loom=self.reading.loom,
            settlement=self.reading.settlement,
            product_sha=self.reading.product_sha,
            epoch=self.epoch,
            value=self.reading.value if self.complete else None,
            status=self.reading.status,
            revision=self.reading.revision,
            complete=self.complete,
            sequence=self.reading.sequence,
            missing_segment=self.reading.missing_segment or self.borrowed_segment,
            conflict=self.reading.conflict,
        )

    def as_json(self) -> dict[str, Any]:
        out = {
            "counter": self.reading.counter,
            "loom": self.reading.loom,
            "settlement": self.reading.settlement,
            "epoch": self.epoch,
            "value": self.value,
            "value_hex": None if self.value is None else f"0x{self.value:04X}",
            "status": self.status,
            "revision": self.reading.revision,
            "low": self.low,
            "high": self.high,
            "sequence": self.sequence,
            "missing_segment": self.reading.missing_segment,
            "crossed": self.crossed,
            "borrowed_segment": self.borrowed_segment,
            "borrowed_from": self.borrowed_from,
            "frames": [f.frame_event_id for f in self.reading.segments],
        }
        return out

    def crossed_with(self, segment: str, source: "EpochReading") -> "EpochReading":
        """A *diagnostic* view: borrow ``segment`` from ``source``.

        This is never a payable boundary.  It exists only to make the trap
        visible -- e.g. borrowing a high half of ``0x02`` for a boundary whose
        real high half was ``0x01`` would fabricate ``0x02FF``.
        """

        return EpochReading(
            reading=self.reading,
            epoch=self.epoch,
            crossed=True,
            borrowed_segment=segment,
            borrowed_from=source.settlement,
        )

    def naive_value(self, other: "EpochReading") -> int | None:
        """What a naive assembler would compute by borrowing a missing half."""

        low = self.low if self.low is not None else other.low
        high = self.high if self.high is not None else other.high
        if low is None or high is None:
            return None
        return (high << self.reading.low_width) | low


@dataclass(frozen=True)
class Interval:
    counter: str
    loom: str
    epoch: int
    readings: tuple[EpochReading, ...]

    @property
    def confirmed(self) -> tuple[EpochReading, ...]:
        return tuple(r for r in self.readings if r.complete)

    @property
    def start(self) -> EpochReading | None:
        return self.confirmed[0] if self.confirmed else None

    @property
    def end(self) -> EpochReading | None:
        return self.confirmed[-1] if self.confirmed else None

    @property
    def monotonic(self) -> bool:
        values = [r.value for r in self.confirmed]
        return all(a <= b for a, b in zip(values, values[1:]))

    @property
    def delta(self) -> int | None:
        if self.start is None or self.end is None or not self.monotonic:
            return None
        return self.end.value - self.start.value  # type: ignore[operator]


@dataclass(frozen=True)
class Assembly:
    counter: str
    loom: str
    product_sha: str
    full_scale: int
    low_width: int
    readings: tuple[EpochReading, ...]
    intervals: tuple[Interval, ...]
    missed: tuple[MissedFragment, ...]
    split_intervals: tuple[Interval, ...]

    def find(self, settlement: str, loom: str | None = None) -> EpochReading | None:
        wanted = loom if loom is not None else self.loom
        fallback: EpochReading | None = None
        for r in self.readings:
            if r.settlement == settlement:
                if r.loom == wanted:
                    return r
                if fallback is None:
                    fallback = r
        return fallback

    def as_json(self) -> dict[str, Any]:
        return {
            "counter": self.counter,
            "loom": self.loom,
            "product_sha256": self.product_sha,
            "full_scale": self.full_scale,
            "low_width": self.low_width,
            "readings": [r.as_json() for r in self.readings],
            "intervals": [
                {
                    "epoch": i.epoch,
                    "start": i.start.settlement if i.start else None,
                    "end": i.end.settlement if i.end else None,
                    "delta": i.delta,
                    "monotonic": i.monotonic,
                }
                for i in self.intervals
            ],
            "split_required": [i.epoch for i in self.split_intervals],
            "missed": [m.as_json() for m in self.missed],
        }


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


@dataclass
class _Group:
    loom: str
    settlement: str
    sequence: int | None
    slots: dict[str, list[Fragment]] = field(default_factory=lambda: {"low": [], "high": []})
    first_arrival: int = 1 << 62

    def add(self, frag: Fragment) -> None:
        self.slots[frag.segment].append(frag)
        self.first_arrival = min(self.first_arrival, frag.arrival)
        if self.sequence is None and frag.sequence is not None:
            self.sequence = frag.sequence

    def values(self, segment: str) -> set[int]:
        return {f.effective for f in self.slots[segment]}

    def committed(self, segment: str) -> list[Fragment]:
        return sorted(
            (f for f in self.slots[segment] if f.committed),
            key=lambda f: (f.arrival, f.frame_event_id),
        )


def _assemble_group(sha: str, counter: CompiledCounter, group: _Group) -> tuple[Reading, list[MissedFragment]]:
    missed: list[MissedFragment] = []
    kept: dict[str, Fragment] = {}
    revision_parts: list[Any] = []
    conflict: list[Any] = []
    for segment in ("low", "high"):
        frags = group.slots[segment]
        vals = group.values(segment)
        committed = group.committed(segment)
        if len(vals) > 1:
            conflict.append(
                {
                    "segment": segment,
                    "values": sorted(vals),
                    "frames": [f.frame_event_id for f in frags],
                }
            )
            for f in frags:
                missed.append(MissedFragment(f, FRAGMENT_CONFLICT))
            revision_parts.append((segment, "conflict", tuple(sorted(vals))))
            continue
        if not committed:
            for f in frags:
                missed.append(MissedFragment(f, FRAGMENT_UNCOMMITTED))
            revision_parts.append((segment, "uncommitted"))
            continue
        kept[segment] = committed[0]
        for f in committed[1:]:
            missed.append(MissedFragment(f, FRAGMENT_DUPLICATE))
        revision_parts.append((segment, committed[0].effective))
    low = kept["low"].effective if "low" in kept else None
    high = kept["high"].effective if "high" in kept else None
    if conflict:
        status = READING_CONFLICT
        missing = None
    elif low is not None and high is not None:
        status = READING_CONFIRMED
        missing = None
    elif low is None and high is None:
        status = READING_UNCOMMITTED
        missing = "low+high"
    else:
        status = READING_INCOMPLETE
        missing = "high" if high is None else "low"
    revision = int(_sha([counter.name, group.loom, group.settlement, revision_parts])[:8], 16)
    segments = tuple(f for f in (kept.get("low"), kept.get("high")) if f is not None)
    sequence = group.sequence
    if sequence is None:
        sequenced = [f.sequence for f in segments if f.sequence is not None]
        sequence = sequenced[0] if sequenced else None
    reading = Reading(
        counter=counter.name,
        loom=group.loom,
        settlement=group.settlement,
        product_sha=sha,
        low=low,
        high=high,
        low_width=counter.low.width,
        status=status,
        revision=revision,
        segments=segments,
        missing_segment=missing,
        conflict=tuple(conflict),
        sequence=sequence,
        first_arrival=group.first_arrival,
    )
    return reading, missed


def _order_key(r: Reading) -> tuple[Any, ...]:
    times = [f.occurred_at_ns for f in r.segments] or [0]
    return (
        0 if r.sequence is not None else 1,
        r.sequence if r.sequence is not None else 0,
        min(times),
        r.settlement,
    )


def assemble_counter(
    product: CatalogProduct,
    counter: str,
    frames: Iterable[Mapping[str, Any]],
) -> Assembly:
    """Assemble one counter from frame mappings (dicts with bus/frame_id/
    occurred_at_ns/payload_hex).  Arrival order is the iteration order; the
    final ordering uses the device sequence."""

    compiled = product.counter(counter)
    groups: dict[tuple[str, str], _Group] = {}
    arrival = 0
    for frame in frames:
        info = _frame_segment(product, compiled, frame)
        if info is None:
            continue
        if info["loom"] is None:
            raise CountingError(
                f"frame {info['frame_event_id']} carries no loom identity for counter {counter!r}",
                code="missing_loom",
            )
        arrival += 1
        frag = Fragment(
            segment=info["segment"],
            bus=info["bus"],
            frame_id=info["frame_id"],
            frame_event_id=info["frame_event_id"],
            raw=info["raw"],
            effective=info["effective"],
            committed=info["committed"],
            arrival=arrival,
            occurred_at_ns=info["occurred_at_ns"],
            settlement=info["settlement"],
            sequence=info["sequence"],
            loom=info["loom"],
        )
        group_key = (info["loom"], info["settlement"])
        group = groups.get(group_key)
        if group is None:
            group = _Group(loom=info["loom"], settlement=info["settlement"], sequence=info["sequence"])
            groups[group_key] = group
        group.add(frag)

    assembled: list[Reading] = []
    missed: list[MissedFragment] = []
    for group in groups.values():
        reading, group_missed = _assemble_group(product.fingerprint, compiled, group)
        assembled.append(reading)
        missed.extend(group_missed)
    ordered = sorted(assembled, key=_order_key)

    # Epoch assignment is per loom: readings of different looms must never be
    # compared for monotonicity.
    intervals: list[Interval] = []
    placed: list[EpochReading] = []
    state: dict[str, dict[str, Any]] = {}
    split_epochs: set[tuple[str, int]] = set()

    def state_of(loom: str) -> dict[str, Any]:
        st = state.get(loom)
        if st is None:
            st = {"epoch": 0, "opened": False, "last": None, "bucket": []}
            state[loom] = st
        return st

    def flush(loom: str) -> None:
        st = state_of(loom)
        if st["bucket"]:
            intervals.append(
                Interval(counter=compiled.name, loom=loom, epoch=st["epoch"], readings=tuple(st["bucket"]))
            )
            st["bucket"] = []

    for reading in ordered:
        st = state_of(reading.loom)
        if reading.complete:
            value = reading.value
            assert value is not None
            if not st["opened"]:
                st["opened"] = True
            elif st["last"] is not None and (
                value < st["last"] or value - st["last"] >= compiled.full_scale
            ):
                split_epochs.add((reading.loom, st["epoch"]))
                flush(reading.loom)
                st["epoch"] += 1
            st["last"] = value
        er = EpochReading(reading=reading, epoch=st["epoch"])
        placed.append(er)
        st["bucket"].append(er)
    for loom in state:
        flush(loom)

    for interval in intervals:
        if not interval.monotonic:
            split_epochs.add((interval.loom, interval.epoch))

    loom = placed[0].loom if placed else ""
    return Assembly(
        counter=compiled.name,
        loom=loom,
        product_sha=product.fingerprint,
        full_scale=compiled.full_scale,
        low_width=compiled.low.width,
        readings=tuple(placed),
        intervals=tuple(intervals),
        missed=tuple(missed),
        split_intervals=tuple(i for i in intervals if (i.loom, i.epoch) in split_epochs),
    )


def assemble_from_warehouse(
    product: CatalogProduct,
    counter: str,
    warehouse: ObservationWarehouse,
) -> Assembly:
    compiled = product.counter(counter)
    frames: list[dict[str, Any]] = []
    for bus, frame_id in compiled.message_keys:
        for stored in warehouse.message_frames(product, bus, frame_id):
            frames.append(
                {
                    "frame_event_id": stored.frame_event_id,
                    "bus": stored.bus,
                    "frame_id": stored.frame_id,
                    "occurred_at_ns": stored.occurred_at_ns,
                    "payload_hex": stored.payload_hex,
                }
            )
    frames.sort(key=lambda f: (f["occurred_at_ns"], f["frame_event_id"]))
    return assemble_counter(product, counter, frames)


# ---------------------------------------------------------------------------
# payroll split
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Employee:
    employee_id: str
    points: int

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "Employee":
        emp = str(raw["employee_id"])
        points = raw.get("points")
        if isinstance(points, bool) or not isinstance(points, int) or points < 0:
            raise CountingError(f"employee {emp!r} points must be a non-negative integer", code="bad_points")
        return cls(employee_id=emp, points=points)

    def as_json(self) -> dict[str, Any]:
        return {"employee_id": self.employee_id, "points": self.points}


@dataclass(frozen=True)
class Group:
    group_id: str
    employees: tuple[Employee, ...]

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "Group":
        members = tuple(Employee.parse(m) for m in raw["employees"])
        if not members:
            raise CountingError("a group needs at least one employee", code="empty_group")
        return cls(group_id=str(raw["group_id"]), employees=members)


@dataclass(frozen=True)
class Allocation:
    employee_id: str
    points: int
    whole: int
    remainder_num: int
    remainder_den: int
    extra: int
    amount: int

    @property
    def remainder(self) -> Fraction:
        return Fraction(self.remainder_num, self.remainder_den) if self.remainder_den else Fraction(0)

    def as_json(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "points": self.points,
            "whole": self.whole,
            "remainder": f"{self.remainder_num}/{self.remainder_den}" if self.remainder_num else "0",
            "extra": self.extra,
            "amount": self.amount,
        }


@dataclass(frozen=True)
class SplitResult:
    total: int
    allocations: tuple[Allocation, ...]
    leftover: int = 0

    def amount_of(self, employee_id: str) -> int:
        return next(a.amount for a in self.allocations if a.employee_id == employee_id)

    def as_json(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "leftover": self.leftover,
            "allocations": [a.as_json() for a in self.allocations],
        }


def split_integer(total: int, members: Sequence[Employee]) -> SplitResult:
    """Split ``total`` fen by integer work points.

    Floor each member's exact share, then hand the leftover fen out one at a
    time by largest fractional remainder; equal remainders go to the smaller
    employee number.
    """

    total = int(total)
    if total < 0:
        raise CountingError("cannot split a negative amount", code="bad_amount")
    members = list(members)
    if not members:
        raise CountingError("split needs at least one member", code="empty_group")
    total_points = sum(m.points for m in members)
    if total_points <= 0:
        raise CountingError("total work points must be positive", code="bad_points")

    whole: dict[str, int] = {}
    remainder: dict[str, Fraction] = {}
    for m in members:
        exact = Fraction(total * m.points, total_points)
        whole[m.employee_id] = exact.numerator // exact.denominator
        remainder[m.employee_id] = exact - whole[m.employee_id]
    leftover = total - sum(whole.values())
    ranked = sorted(members, key=lambda m: (-remainder[m.employee_id], m.employee_id))
    extra = {m.employee_id: 0 for m in members}
    for i in range(leftover):
        extra[ranked[i % len(ranked)].employee_id] += 1
    allocations = tuple(
        Allocation(
            employee_id=m.employee_id,
            points=m.points,
            whole=whole[m.employee_id],
            remainder_num=remainder[m.employee_id].numerator,
            remainder_den=remainder[m.employee_id].denominator,
            extra=extra[m.employee_id],
            amount=whole[m.employee_id] + extra[m.employee_id],
        )
        for m in members
    )
    return SplitResult(total=total, allocations=allocations, leftover=leftover)


# ---------------------------------------------------------------------------
# shift / payslip
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Shift:
    shift_id: str
    group_id: str
    start_settlement: str
    end_settlement: str
    other_group_boundary: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class GroupPayslip:
    shift_id: str
    group_id: str
    status: str
    fen: int | None
    pieces: int | None
    start_settlement: str | None
    end_settlement: str | None
    start_pin: str | None
    end_pin: str | None
    allocations: tuple[Allocation, ...] = ()
    reasons: tuple[str, ...] = ()
    # provenance (for re-issue)
    reason: str = PROVENANCE_INITIAL
    supersedes: str | None = None
    previous_fen: int | None = None

    @property
    def slip_id(self) -> str:
        return f"{self.shift_id}:{self.group_id}"

    def as_json(self) -> dict[str, Any]:
        return {
            "slip_id": self.slip_id,
            "shift_id": self.shift_id,
            "group_id": self.group_id,
            "status": self.status,
            "fen": self.fen,
            "pieces": self.pieces,
            "start_settlement": self.start_settlement,
            "end_settlement": self.end_settlement,
            "start_pin": self.start_pin,
            "end_pin": self.end_pin,
            "allocations": [a.as_json() for a in self.allocations],
            "reasons": list(self.reasons),
            "reason": self.reason,
            "supersedes": self.supersedes,
            "previous_fen": self.previous_fen,
        }


@dataclass(frozen=True)
class ShiftReport:
    shift_id: str
    settlement: str
    container: str
    loom: str
    group: GroupPayslip
    picture: dict[str, Any]
    day_delta: int | None
    reasons: tuple[str, ...] = ()
    provenance: str = PROVENANCE_INITIAL

    @property
    def referenced_pins(self) -> tuple[str, ...]:
        pins = []
        if self.group.start_pin:
            pins.append(self.group.start_pin)
        if self.group.end_pin:
            pins.append(self.group.end_pin)
        return tuple(dict.fromkeys(pins))

    def as_json(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift_id,
            "settlement": self.settlement,
            "container": self.container,
            "loom": self.loom,
            "provenance": self.provenance,
            "picture": self.picture,
            "day_delta": self.day_delta,
            "reasons": list(self.reasons),
            "payslip": self.group.as_json(),
            "referenced_pins": list(self.referenced_pins),
        }


@dataclass(frozen=True)
class DayReport:
    container: str
    loom: str
    counter: str
    epoch: int
    day_delta: int | None
    start_pin: str | None
    end_pin: str | None
    shifts: tuple[ShiftReport, ...]
    split_required: bool
    reasons: tuple[str, ...] = ()
    provenance: str = PROVENANCE_INITIAL

    @property
    def referenced_pins(self) -> tuple[str, ...]:
        pins = []
        if self.start_pin:
            pins.append(self.start_pin)
        if self.end_pin:
            pins.append(self.end_pin)
        for s in self.shifts:
            pins.extend(s.referenced_pins)
        return tuple(dict.fromkeys(pins))

    def as_json(self) -> dict[str, Any]:
        return {
            "container": self.container,
            "loom": self.loom,
            "counter": self.counter,
            "epoch": self.epoch,
            "day_delta": self.day_delta,
            "start_pin": self.start_pin,
            "end_pin": self.end_pin,
            "split_required": self.split_required,
            "reasons": list(self.reasons),
            "provenance": self.provenance,
            "referenced_pins": list(self.referenced_pins),
            "shifts": [s.as_json() for s in self.shifts],
        }


def _boundary(
    reading: EpochReading | None,
) -> tuple[str | None, dict[str, Any] | None, list[str]]:
    if reading is None:
        return None, None, ["boundary missing"]
    reasons: list[str] = []
    if reading.crossed:
        reasons.append(
            f"boundary {reading.settlement} is a crossed half: {reading.borrowed_segment} borrowed from "
            f"{reading.borrowed_from}"
        )
    elif reading.status == READING_CONFLICT:
        reasons.append(f"boundary {reading.settlement} conflicts (repeated segment differs)")
    elif reading.status == READING_UNCOMMITTED:
        reasons.append(f"boundary {reading.settlement} has no committed fragment")
    elif not reading.complete:
        reasons.append(f"boundary {reading.settlement} is incomplete (missing {reading.reading.missing_segment})")
    if reading.sequence is None:
        reasons.append(f"boundary {reading.settlement} has no settlement sequence")
    return reading.pin.pin_id, reading.pin.as_json(), reasons


def settle_day(
    assembly: Assembly,
    counter: CompiledCounter,
    *,
    container: str,
    groups: Mapping[str, Group],
    shifts: Sequence[Shift],
    fen_per_piece: int = 1,
    provenance: str = PROVENANCE_INITIAL,
    previous: Mapping[str, GroupPayslip] | None = None,
) -> DayReport:
    previous = previous or {}
    confirmed = [r for r in assembly.readings if r.complete]
    day_start = confirmed[0] if confirmed else None
    day_end = confirmed[-1] if confirmed else None
    day_delta: int | None = None
    if day_start is not None and day_end is not None and day_start.epoch == day_end.epoch:
        if day_end.value is not None and day_start.value is not None and day_end.value >= day_start.value:
            day_delta = day_end.value - day_start.value

    reports: list[ShiftReport] = []
    for shift in shifts:
        group = groups.get(shift.group_id)
        if group is None:
            raise CountingError(f"shift {shift.shift_id!r} names unknown group {shift.group_id!r}", code="unknown_group")
        start_read = assembly.find(shift.start_settlement)
        end_read = assembly.find(shift.end_settlement)
        other_read = assembly.find(shift.other_group_boundary) if shift.other_group_boundary else None
        start_pin, start_pic, start_reasons = _boundary(start_read)
        end_pin, end_pic, end_reasons = _boundary(end_read)
        other_pin, other_pic, other_reasons = (
            _boundary(other_read) if other_read is not None else (None, None, [])
        )
        reasons: list[str] = list(start_reasons) + list(end_reasons)

        def pieces_between(a: EpochReading | None, b: EpochReading | None) -> int | None:
            if a is None or b is None or not a.complete or not b.complete:
                return None
            if a.epoch != b.epoch or a.value is None or b.value is None or b.value < a.value:
                return None
            return b.value - a.value

        day_pieces = pieces_between(start_read, end_read)
        pieces: int | None = day_pieces
        if day_pieces is None:
            if start_read is not None and end_read is not None and start_read.epoch != end_read.epoch:
                reasons.append("boundaries lie in different epochs")
        elif shift.other_group_boundary is not None:
            other_pieces = pieces_between(start_read, other_read)
            if other_pieces is None:
                reasons.extend(other_reasons)
                reasons.append(
                    "other group's boundary is not a confirmed reading; day's total kept but the "
                    "per-group split is withheld"
                )
                pieces = None
            else:
                pieces = day_pieces - other_pieces
                if pieces < 0:
                    reasons.append("group interval is not monotonic")
                    pieces = None

        if pieces is None:
            status = GROUP_HELD
            fen = None
            allocations: tuple[Allocation, ...] = ()
        else:
            status = GROUP_SETTLED
            fen = pieces * fen_per_piece
            allocations = split_integer(fen, group.employees).allocations

        prev = previous.get(f"{shift.shift_id}:{shift.group_id}")
        slip = GroupPayslip(
            shift_id=shift.shift_id,
            group_id=shift.group_id,
            status=status,
            fen=fen,
            pieces=pieces,
            start_settlement=shift.start_settlement,
            end_settlement=shift.end_settlement,
            start_pin=start_pin,
            end_pin=end_pin,
            allocations=allocations,
            reasons=tuple(dict.fromkeys(reasons)),
            reason=PROVENANCE_ARCHIVED if prev is not None else provenance,
            supersedes=None,
            previous_fen=prev.fen if prev is not None else None,
        )
        picture = {
            "start": start_pic,
            "end": end_pic,
            "other_group_boundary": other_pic,
            "day_delta": day_pieces,
            "note": shift.note,
        }
        reports.append(
            ShiftReport(
                shift_id=shift.shift_id,
                settlement=shift.end_settlement,
                container=container,
                loom=assembly.loom,
                group=slip,
                picture=picture,
                day_delta=day_pieces,
                reasons=tuple(dict.fromkeys(reasons)),
                provenance=PROVENANCE_ARCHIVED if prev is not None else provenance,
            )
        )

    day_reasons: list[str] = []
    if assembly.split_intervals:
        day_reasons.append(
            "epoch needs splitting: a reading crossed the full scale or regressed; "
            "the cross-epoch interval is held pending split"
        )
    if day_start is None or day_end is None:
        day_reasons.append("day has no complete confirmed readings")
    elif day_start.epoch != day_end.epoch:
        day_reasons.append("day boundaries lie in different epochs")

    return DayReport(
        container=container,
        loom=assembly.loom,
        counter=assembly.counter,
        epoch=day_start.epoch if day_start else -1,
        day_delta=day_delta,
        start_pin=day_start.pin.pin_id if day_start else None,
        end_pin=day_end.pin.pin_id if day_end else None,
        shifts=tuple(reports),
        split_required=bool(assembly.split_intervals),
        reasons=tuple(day_reasons),
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# ledger / recalculation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecalcEntry:
    container: str
    group_id: str
    shift_id: str
    status: str
    reason: str
    provenance: str
    old_fen: int | None
    new_fen: int | None

    def as_json(self) -> dict[str, Any]:
        return {
            "container": self.container,
            "group_id": self.group_id,
            "shift_id": self.shift_id,
            "status": self.status,
            "reason": self.reason,
            "provenance": self.provenance,
            "old_fen": self.old_fen,
            "new_fen": self.new_fen,
        }


@dataclass(frozen=True)
class Recalculation:
    containers: tuple[str, ...]
    entries: tuple[RecalcEntry, ...]
    history: tuple[DayReport, ...]

    def as_json(self) -> dict[str, Any]:
        recompute = sorted(
            {(e.group_id, e.shift_id, e.container) for e in self.entries},
        )
        return {
            "containers": list(self.containers),
            "groups_to_recompute": [
                {"group_id": g, "shift_id": s, "container": c} for g, s, c in recompute
            ],
            "entries": [e.as_json() for e in self.entries],
            "history": [d.as_json() for d in self.history],
        }


@dataclass
class _StoredDay:
    spec: "DaySpec"
    report: DayReport
    previous: dict[str, GroupPayslip]


@dataclass(frozen=True)
class DaySpec:
    container: str
    counter: str
    groups: Mapping[str, Group]
    shifts: tuple[Shift, ...]
    fen_per_piece: int = 1


class PayrollLedger:
    """Container ledger with a boundary-pin -> container/group index.

    ``record`` stores an issued day report.  ``recalculate`` re-runs the given
    containers against a *new* assembly (with archived fragments added) and
    reports which groups changed, which must recompute, and the new slips.
    """

    def __init__(self, product: CatalogProduct) -> None:
        self.product = product
        self._days: dict[str, _StoredDay] = {}
        self._pins: dict[str, set[tuple[str, str, str]]] = {}
        self._history: dict[str, list[DayReport]] = {}

    # -- recording -------------------------------------------------------

    def record(self, spec: DaySpec, report: DayReport) -> None:
        previous = {
            s.group.slip_id: s.group for s in report.shifts
        } if report.shifts else {}
        self._days[spec.container] = _StoredDay(spec=spec, report=report, previous=previous)
        self._history.setdefault(spec.container, []).append(report)
        for pin in report.referenced_pins:
            entry = self._pins.setdefault(pin, set())
            for s in report.shifts:
                entry.add((spec.container, s.group.group_id, s.shift_id))

    def citing(self, pin_id: str) -> list[tuple[str, str, str]]:
        """Every ``(container, group_id, shift_id)`` that cited this pin."""

        return sorted(self._pins.get(pin_id, set()))

    def containers_citing(self, pin_ids: Iterable[str]) -> list[str]:
        out: list[str] = []
        for pin in pin_ids:
            for container, _, _ in self.citing(pin):
                if container not in out:
                    out.append(container)
        return out

    def containers(self) -> list[str]:
        return list(self._days)

    def report(self, container: str) -> DayReport | None:
        stored = self._days.get(container)
        return stored.report if stored else None

    def history(self, container: str) -> list[DayReport]:
        return list(self._history.get(container, []))

    # -- recalculation ---------------------------------------------------

    def changed_pins(self, old: Assembly, new: Assembly) -> list[str]:
        """Pins whose confirmed value/status changed between two assemblies."""

        changed: list[str] = []
        old_by = {(r.loom, r.settlement): r for r in old.readings}
        new_by = {(r.loom, r.settlement): r for r in new.readings}
        for key in sorted(set(old_by) | set(new_by)):
            before = old_by.get(key)
            after = new_by.get(key)
            before_sig = _reading_signature(before)
            after_sig = _reading_signature(after)
            if before_sig != after_sig:
                # Index by both old and new pin so either citation matches.
                if before is not None:
                    changed.append(before.pin.pin_id)
                if after is not None and after.pin.pin_id != (before.pin.pin_id if before else None):
                    changed.append(after.pin.pin_id)
        return changed

    def affected_containers(self, old: Assembly, new: Assembly) -> list[str]:
        return self.containers_citing(self.changed_pins(old, new))

    def recalculate(
        self,
        new_assembly: Assembly,
        *,
        containers: Iterable[str] | None = None,
        old_assembly: Assembly | None = None,
    ) -> Recalculation:
        if containers is None:
            if old_assembly is None:
                raise CountingError(
                    "pass containers or old_assembly to find what changed", code="recalc_scope"
                )
            containers = self.affected_containers(old_assembly, new_assembly)
        entries: list[RecalcEntry] = []
        history: list[DayReport] = []
        touched: list[str] = []
        for container in containers:
            stored = self._days.get(container)
            if stored is None:
                continue
            touched.append(container)
            new_report = settle_day(
                new_assembly,
                self.product.counter(new_assembly.counter),
                container=stored.spec.container,
                groups=stored.spec.groups,
                shifts=stored.spec.shifts,
                fen_per_piece=stored.spec.fen_per_piece,
                provenance=PROVENANCE_ARCHIVED,
                previous=stored.previous,
            )
            new_by = {s.group.slip_id: s.group for s in new_report.shifts}
            for shift in stored.report.shifts:
                old_slip = shift.group
                new_slip = new_by[old_slip.slip_id]
                if (
                    old_slip.status == new_slip.status
                    and old_slip.fen == new_slip.fen
                    and old_slip.start_pin == new_slip.start_pin
                    and old_slip.end_pin == new_slip.end_pin
                ):
                    continue
                reason = "recomputed" if new_slip.status == GROUP_SETTLED else "awaiting_reproof"
                entries.append(
                    RecalcEntry(
                        container=container,
                        group_id=new_slip.group_id,
                        shift_id=new_slip.shift_id,
                        status=new_slip.status,
                        reason=reason,
                        provenance=PROVENANCE_ARCHIVED,
                        old_fen=old_slip.fen,
                        new_fen=new_slip.fen,
                    )
                )
                self._pins_for(new_slip, container)
            self._days[container] = _StoredDay(
                spec=stored.spec, report=new_report, previous={s.group.slip_id: s.group for s in new_report.shifts}
            )
            self._history.setdefault(container, []).append(new_report)
            history.append(new_report)
        return Recalculation(containers=tuple(touched), entries=tuple(entries), history=tuple(history))

    def _pins_for(self, slip: GroupPayslip, container: str) -> None:
        for pin in (slip.start_pin, slip.end_pin):
            if pin:
                self._pins.setdefault(pin, set()).add((container, slip.group_id, slip.shift_id))


def _reading_signature(reading: EpochReading | None) -> tuple[Any, ...] | None:
    if reading is None:
        return None
    return (
        reading.status,
        reading.complete,
        reading.value,
        reading.reading.revision,
        reading.epoch,
        reading.reading.missing_segment,
        tuple(sorted(reading.reading.conflict)),
    )


__all__ = [
    "Allocation",
    "Assembly",
    "CounterPin",
    "DayReport",
    "DaySpec",
    "EpochReading",
    "Employee",
    "Fragment",
    "Group",
    "GroupPayslip",
    "Interval",
    "MissedFragment",
    "PayrollLedger",
    "Reading",
    "RecalcEntry",
    "Recalculation",
    "Shift",
    "ShiftReport",
    "SplitResult",
    "assemble_counter",
    "assemble_from_warehouse",
    "settle_day",
    "split_integer",
]
