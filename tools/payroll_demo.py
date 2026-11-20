"""End-to-end payroll scenario over locally generated split-counter frames.

Builds a small but *dense* night so the requested edge cases are inspectable:

* many settlement identities that arrive as only one half (只到半份),
* out-of-order completion (异序补件),
* a handover and a day end at the same instant (同刻) distinguished only by the
  device's unique increasing settlement sequence,
* a handover whose high half arrives late (archived proof), so the first
  payroll run holds the night slip and the re-run pays it,
* many payslips citing a few shared handover readings, so the recalculation
  index reports exactly which groups must recompute.

Outputs (under ``artifacts/``):
``payroll_frames.jsonl``, ``payroll_catalog.json``, ``payroll_report.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.catalog import build_product  # noqa: E402
from bitcatalog.counting import (  # noqa: E402
    GROUP_HELD,
    GROUP_SETTLED,
    DaySpec,
    Employee,
    Group,
    PayrollLedger,
    Shift,
    assemble_counter,
    settle_day,
)

BASE_NS = 1_800_000_000_000_000_000


def _common(width_sig: str, extra: int = 0) -> list[dict[str, Any]]:
    return [
        {"name": "settlement", "start": 0, "width": 16, "order": "little", "signed": False,
         "factor": "1", "offset": "0"},
        {"name": "sequence", "start": 16, "width": 16, "order": "little", "signed": False,
         "factor": "1", "offset": "0"},
        {"name": "loom_id", "start": 32, "width": 8, "order": "little", "signed": False,
         "factor": "1", "offset": "0"},
        {"name": "commit", "start": 40, "width": 1, "order": "little", "signed": False,
         "factor": "1", "offset": "0"},
        {"name": width_sig, "start": 48, "width": 8 + extra, "order": "little", "signed": False,
         "factor": "1", "offset": "0"},
    ]


def catalog_defs() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    low = {"bus": "loom", "frame_id": 20, "payload_bytes": 8, "signals": _common("count_low")}
    high = {"bus": "loom", "frame_id": 21, "payload_bytes": 8, "signals": _common("count_high")}
    counter = {
        "name": "pieces",
        "loom_constant": "loom-7",
        "low": {
            "bus": "loom", "frame_id": 20, "signal": "count_low", "width": 8,
            "commit": {"bus": "loom", "frame_id": 20, "signal": "commit"},
            "settlement": {"bus": "loom", "frame_id": 20, "signal": "settlement"},
            "sequence": {"bus": "loom", "frame_id": 20, "signal": "sequence"},
        },
        "high": {
            "bus": "loom", "frame_id": 21, "signal": "count_high", "width": 8,
            "commit": {"bus": "loom", "frame_id": 21, "signal": "commit"},
            "settlement": {"bus": "loom", "frame_id": 21, "signal": "settlement"},
            "sequence": {"bus": "loom", "frame_id": 21, "signal": "sequence"},
        },
    }
    return [low, high], counter


def counter_product():
    messages, counter = catalog_defs()
    return build_product(messages, [counter])


def _seg_frame(product, event_id, segment, settlement, sequence, value, *, commit=1, ns=BASE_NS):
    bus, frame_id = ("loom", 20) if segment == "low" else ("loom", 21)
    name = "count_low" if segment == "low" else "count_high"
    payload = product.message(bus, frame_id).encode(
        {"settlement": settlement, "sequence": sequence, "loom_id": 7, "commit": commit, name: value}
    ).payload
    return {
        "frame_event_id": event_id,
        "bus": bus,
        "frame_id": frame_id,
        "occurred_at_ns": ns,
        "payload_hex": payload.hex(),
    }


def generate_frames(product) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """A night with many half-readings plus the payroll boundaries."""

    frames: list[dict[str, Any]] = []
    # Settlements 1..12 are the "night's many half-readings": each has only the
    # low half committed; their high halves never arrive.  Some arrive out of
    # order and some at the exact same nanosecond.
    half_values = [0x40, 0x41, 0x42, 0x50, 0x51, 0x60, 0x61, 0x70, 0x80, 0x90, 0xA0, 0xB0]
    for i, value in enumerate(half_values):
        settlement = 100 + i
        sequence = 100 + i
        ns = BASE_NS + (i // 3) * 1_000_000  # groups of three share a timestamp
        frames.append(_seg_frame(product, f"night-{settlement}-low", "low", settlement, sequence, value, ns=ns))
    # Out-of-order completion: settlement 200 low before high, 201 high before low.
    frames.append(_seg_frame(product, "s200-low", "low", 200, 200, 0xFE, ns=BASE_NS + 10_000_000))
    frames.append(_seg_frame(product, "s200-high", "high", 200, 200, 0x01, ns=BASE_NS + 20_000_000))
    frames.append(_seg_frame(product, "s201-high", "high", 201, 201, 0x01, ns=BASE_NS + 30_000_000))
    frames.append(_seg_frame(product, "s201-low", "low", 201, 201, 0xFF, ns=BASE_NS + 40_000_000))
    # Same instant: day end 0x0200 and night handover 0x01FF at identical ns;
    # only the sequence (300 < 301) orders them.
    same_ns = BASE_NS + 50_000_000
    frames.append(_seg_frame(product, "s202-low", "low", 202, 300, 0xFF, ns=same_ns))
    frames.append(_seg_frame(product, "s202-high", "high", 202, 300, 0x01, ns=same_ns))
    frames.append(_seg_frame(product, "s203-low", "low", 203, 301, 0x00, ns=same_ns))
    frames.append(_seg_frame(product, "s203-high", "high", 203, 301, 0x02, ns=same_ns))
    # Archived late proof: settlement 204's high half arrives only in the
    # re-run; the low half is here from the start.
    frames.append(_seg_frame(product, "s204-low", "low", 204, 302, 0x05, ns=same_ns + 1))
    late = _seg_frame(product, "s204-high", "high", 204, 302, 0x02, ns=same_ns + 2)
    meta = {
        "night_half_readings": [100 + i for i in range(len(half_values))],
        "out_of_order": [200, 201],
        "same_instant": {"ns": same_ns, "settlements": [202, 203]},
        "archived_late": {"settlement": 204, "late_frame": late["frame_event_id"]},
    }
    return frames, (meta, late)


def run_mass_scenario(product, groups_count: int = 50) -> dict[str, Any]:
    """Many payslips citing a few shared handover readings.

    A night handover (settlement 5, real value 0x01FF) arrives with only its low
    half; the same-instant day end (settlement 6, 0x0200) arrives complete.
    A naive assembler borrowing 6's high half would fabricate 0x02FF for the
    handover.  ``groups_count`` group slips all cite the same two boundary
    readings, are held on the first run, and are re-issued together when the
    handover's high half is archived.
    """

    def frame(event_id, segment, settlement, sequence, value, ns):
        return _seg_frame(product, event_id, segment, settlement, sequence, value, ns=ns)

    t0 = BASE_NS + 900_000_000
    base_frames = [
        frame("m5-low", "low", 5, 5, 0xFF, t0),
        frame("m6-low", "low", 6, 6, 0x00, t0),
        frame("m6-high", "high", 6, 6, 0x02, t0),
    ]
    late = frame("m5-high", "high", 5, 5, 0x01, t0 + 1)

    before = assemble_counter(product, "pieces", base_frames)
    after = assemble_counter(product, "pieces", base_frames + [late])

    groups = {
        f"crew{i:02d}": Group(f"crew{i:02d}", (Employee(f"W{i:02d}", 1),))
        for i in range(groups_count)
    }
    shifts = tuple(
        Shift(f"night-{i:02d}", f"crew{i:02d}", "5", "6") for i in range(groups_count)
    )
    spec = DaySpec("loom7-night-mass", "pieces", groups, shifts, fen_per_piece=1)
    ledger = PayrollLedger(product)
    first = settle_day(before, product.counter("pieces"), container=spec.container,
                       groups=groups, shifts=shifts)
    ledger.record(spec, first)

    here = before.find("5")
    nxt = before.find("6")
    naive = here.naive_value(nxt) if here and nxt else None
    crossed_view = here.crossed_with("high", nxt).as_json() if here and nxt else None

    affected = ledger.affected_containers(before, after)
    recalc = ledger.recalculate(after, old_assembly=before)
    pin5 = after.find("5").pin.pin_id if after.find("5") else None
    return {
        "groups": groups_count,
        "first_run": {
            "held_slips": sum(1 for s in first.shifts if s.group.status == GROUP_HELD),
            "settled_slips": sum(1 for s in first.shifts if s.group.status == GROUP_SETTLED),
            "handover_status": here.status if here else None,
            "handover_missing": here.reading.missing_segment if here else None,
            "naive_borrowed_value": None if naive is None else f"0x{naive:04X}",
            "crossed_view": crossed_view,
        },
        "real_handover_value": None if after.find("5") is None else f"0x{after.find('5').value:04X}",
        "late_frame": "m5-high",
        "affected_containers": affected,
        "citers_of_handover_pin": len(ledger.citing(pin5)) if pin5 else 0,
        "recalc": recalc.as_json(),
    }


def run_demo(write_dir: Path | None = None) -> dict[str, Any]:
    product = counter_product()
    frames, (meta, late) = generate_frames(product)

    groups = {
        "day": Group("day", (Employee("D1", 1),)),
        "night": Group("night", (Employee("A", 2), Employee("B", 2))),
    }
    # Day: start 200 (0x01FE) -> end 202 (0x01FF); night: 202 (0x01FF) -> 203
    # (0x0200).  One piece per group, one fen per piece.
    shifts = (
        Shift("day-shift", "day", "200", "202"),
        Shift("night-shift", "night", "202", "203"),
    )
    spec = DaySpec("loom7-day1", "pieces", groups, shifts, fen_per_piece=1)

    old_assembly = assemble_counter(product, "pieces", frames)
    ledger = PayrollLedger(product)
    first = settle_day(old_assembly, product.counter("pieces"), container=spec.container,
                       groups=groups, shifts=shifts)
    ledger.record(spec, first)

    # Re-run with the archived late proof.  Settlement 204 becomes complete, but
    # the payroll boundaries cite 200/202/203, so only containers that cited a
    # changed reading re-open.  Here 204 is not cited, so nothing reopens on
    # this trigger; a second scenario adds a shift that does cite 204.
    new_assembly = assemble_counter(product, "pieces", frames + [late])
    affected_without = ledger.affected_containers(old_assembly, new_assembly)

    # A second loom-day whose night slip *does* cite 204 -> recompute.
    shifts2 = (Shift("night-shift-2", "night", "203", "204"),)
    spec2 = DaySpec("loom7-day2", "pieces", groups, shifts2, fen_per_piece=1)
    before2 = settle_day(old_assembly, product.counter("pieces"), container=spec2.container,
                         groups=groups, shifts=shifts2)
    ledger.record(spec2, before2)
    affected_after = ledger.affected_containers(old_assembly, new_assembly)
    recalc = ledger.recalculate(new_assembly, old_assembly=old_assembly)

    # Many payslips citing a few boundaries: 40 group-slips all cite 202 and
    # 203.  Enumerate the recompute queue for the changed pins.
    changed_pins = ledger.changed_pins(old_assembly, new_assembly)
    citing: dict[str, list[Any]] = {
        pin: ledger.citing(pin) for pin in changed_pins
    }

    report = {
        "counter": "pieces",
        "half_readings": {
            "count": len(meta["night_half_readings"]),
            "settlements": meta["night_half_readings"],
            "incomplete": [
                {"settlement": r.settlement, "missing": r.reading.missing_segment, "status": r.status}
                for r in old_assembly.readings
                if r.status != "confirmed" and r.loom == "loom-7"
                and int(r.settlement) in meta["night_half_readings"]
            ],
        },
        "out_of_order": [
            {"settlement": r.settlement, "status": r.status, "value": r.value,
             "frames": [f.frame_event_id for f in r.reading.segments]}
            for r in old_assembly.readings if int(r.settlement) in (200, 201)
        ],
        "same_instant": {
            "ns": meta["same_instant"]["ns"],
            "order": [r.settlement for r in old_assembly.readings
                      if int(r.settlement) in (202, 203)],
            "sequences": {r.settlement: r.sequence for r in old_assembly.readings
                          if int(r.settlement) in (202, 203)},
        },
        "first_run": {
            "day_pieces": first.shifts[0].group.pieces,
            "night_pieces": first.shifts[1].group.pieces,
            "day_fen": first.shifts[0].group.fen,
            "night_fen": first.shifts[1].group.fen,
            "night_allocations": [a.as_json() for a in first.shifts[1].group.allocations],
        },
        "archived_late_proof": {
            "settlement": meta["archived_late"]["settlement"],
            "late_frame": meta["archived_late"]["late_frame"],
            "affected_containers_initial_spec": affected_without,
            "affected_containers_with_citing_spec": affected_after,
            "recomputed": recalc.as_json(),
        },
        "changed_pins": changed_pins,
        "citation_index": {pin: [list(x) for x in rows] for pin, rows in citing.items()},
        "many_slips_few_boundaries": {
            "note": "each night payslip cites the same 202/203 boundary readings",
            "citing_202": ledger.citing(
                next((r for r in new_assembly.readings if r.settlement == "202"), None).pin.pin_id
            ) if new_assembly.find("202") else [],
        },
        "split_required_epochs": [i.epoch for i in new_assembly.split_intervals],
        "missed_fragments": [m.as_json() for m in old_assembly.missed],
        "mass_scenario": run_mass_scenario(product),
    }

    if write_dir is not None:
        write_dir.mkdir(parents=True, exist_ok=True)
        product.save(write_dir / "payroll_catalog.json")
        definitions, counter = catalog_defs()
        (write_dir / "payroll_definitions.json").write_text(
            json.dumps({"messages": definitions, "counters": [counter]}, indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with (write_dir / "payroll_frames.jsonl").open("w", encoding="utf-8") as fh:
            for frame in frames:
                fh.write(json.dumps(frame, sort_keys=True) + "\n")
        (write_dir / "payroll_late_frames.jsonl").write_text(
            json.dumps(late, sort_keys=True) + "\n", encoding="utf-8"
        )
        (write_dir / "payroll_report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "artifacts"))
    args = parser.parse_args(argv)
    report = run_demo(Path(args.out))
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
