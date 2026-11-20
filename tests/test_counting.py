"""Counting / payroll tests: split halves, epochs, boundaries, fen split, recalc."""

from __future__ import annotations

import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.catalog import CompileError, build_product  # noqa: E402
from bitcatalog.counting import (  # noqa: E402
    GROUP_HELD,
    GROUP_SETTLED,
    READING_CONFIRMED,
    READING_CONFLICT,
    READING_INCOMPLETE,
    DaySpec,
    Employee,
    Group,
    PayrollLedger,
    Shift,
    assemble_counter,
    assemble_from_warehouse,
    settle_day,
    split_integer,
)
from bitcatalog.warehouse import ObservationWarehouse  # noqa: E402

BASE_NS = 1_800_000_000_000_000_000


def counter_messages():
    """Low half and high half in two messages; shared identity fields."""

    common = [
        {"name": "settlement", "start": 0, "width": 16, "order": "little", "signed": False,
         "factor": "1", "offset": "0"},
        {"name": "sequence", "start": 16, "width": 16, "order": "little", "signed": False,
         "factor": "1", "offset": "0"},
        {"name": "loom_id", "start": 32, "width": 8, "order": "little", "signed": False,
         "factor": "1", "offset": "0"},
        {"name": "commit", "start": 40, "width": 1, "order": "little", "signed": False,
         "factor": "1", "offset": "0"},
    ]
    low = {
        "bus": "L", "frame_id": 10, "payload_bytes": 8,
        "signals": common + [
            {"name": "count_low", "start": 48, "width": 8, "order": "little", "signed": False,
             "factor": "1", "offset": "0"},
        ],
    }
    high = {
        "bus": "H", "frame_id": 11, "payload_bytes": 8,
        "signals": [
            {"name": "settlement", "start": 0, "width": 16, "order": "little", "signed": False,
             "factor": "1", "offset": "0"},
            {"name": "sequence", "start": 16, "width": 16, "order": "little", "signed": False,
             "factor": "1", "offset": "0"},
            {"name": "loom_id", "start": 32, "width": 8, "order": "little", "signed": False,
             "factor": "1", "offset": "0"},
            {"name": "commit", "start": 40, "width": 1, "order": "little", "signed": False,
             "factor": "1", "offset": "0"},
            {"name": "count_high", "start": 48, "width": 8, "order": "little", "signed": False,
             "factor": "1", "offset": "0"},
        ],
    }
    return low, high


def counter_definition():
    low, high = counter_messages()
    counter = {
        "name": "pieces",
        "loom_constant": "loom-7",
        "low": {
            "bus": "L", "frame_id": 10, "signal": "count_low", "width": 8,
            "commit": {"bus": "L", "frame_id": 10, "signal": "commit"},
            "settlement": {"bus": "L", "frame_id": 10, "signal": "settlement"},
            "sequence": {"bus": "L", "frame_id": 10, "signal": "sequence"},
        },
        "high": {
            "bus": "H", "frame_id": 11, "signal": "count_high", "width": 8,
            "commit": {"bus": "H", "frame_id": 11, "signal": "commit"},
            "settlement": {"bus": "H", "frame_id": 11, "signal": "settlement"},
            "sequence": {"bus": "H", "frame_id": 11, "signal": "sequence"},
        },
    }
    return [low, high], counter


def product():
    messages, counter = counter_definition()
    return build_product(messages, [counter])


def low_frame(product, event_id, settlement, sequence, low, *, commit=1, loom=7, ns=BASE_NS):
    msg = product.message("L", 10)
    payload = msg.encode(
        {"settlement": settlement, "sequence": sequence, "loom_id": loom, "commit": commit,
         "count_low": low}
    ).payload
    return {"frame_event_id": event_id, "bus": "L", "frame_id": 10, "occurred_at_ns": ns,
            "payload_hex": payload.hex()}


def high_frame(product, event_id, settlement, sequence, high, *, commit=1, loom=7, ns=BASE_NS):
    msg = product.message("H", 11)
    payload = msg.encode(
        {"settlement": settlement, "sequence": sequence, "loom_id": loom, "commit": commit,
         "count_high": high}
    ).payload
    return {"frame_event_id": event_id, "bus": "H", "frame_id": 11, "occurred_at_ns": ns,
            "payload_hex": payload.hex()}


class CounterDeclaration(unittest.TestCase):
    def test_counters_compile_and_persist(self):
        p = product()
        self.assertEqual(len(p.counters), 1)
        c = p.counter("pieces")
        self.assertEqual(c.full_scale, 1 << 16)
        self.assertEqual(c.low.width, 8)

    def test_bad_counter_rejected(self):
        messages, counter = counter_definition()
        counter["low"]["commit"] = {"bus": "H", "frame_id": 11, "signal": "commit"}
        with self.assertRaises(CompileError) as ctx:
            build_product(messages, [counter])
        self.assertEqual(ctx.exception.code, "counter_identity_frame")

    def test_missing_loom_identity_rejected(self):
        messages, counter = counter_definition()
        del counter["loom_constant"]
        with self.assertRaises(Exception):
            build_product(messages, [counter])


class Assembly(unittest.TestCase):
    def setUp(self):
        self.p = product()

    def test_out_of_order_pieces_assemble(self):
        # High arrives before low; still one confirmed reading.
        frames = [
            high_frame(self.p, "h1", settlement=0x00FF, sequence=5, high=0x02),
            low_frame(self.p, "l1", settlement=0x00FF, sequence=5, low=0x10),
        ]
        asm = assemble_counter(self.p, "pieces", frames)
        reading = asm.find("255")
        self.assertEqual(reading.status, READING_CONFIRMED)
        self.assertEqual(reading.value, 0x0210)
        self.assertEqual(len(asm.readings), 1)

    def test_missing_half_is_incomplete_not_fabricated(self):
        frames = [
            low_frame(self.p, "l1", settlement=0x00FF, sequence=5, low=0xFF),
            high_frame(self.p, "h2", settlement=0x0100, sequence=6, high=0x02),
        ]
        asm = assemble_counter(self.p, "pieces", frames)
        first = asm.find("255")
        self.assertEqual(first.status, READING_INCOMPLETE)
        self.assertEqual(first.reading.missing_segment, "high")
        self.assertIsNone(first.value)

    def test_uncommitted_trial_never_confirms(self):
        frames = [
            low_frame(self.p, "l1", settlement=0x0001, sequence=1, low=0x11, commit=0),
            high_frame(self.p, "h1", settlement=0x0001, sequence=1, high=0x00, commit=0),
        ]
        asm = assemble_counter(self.p, "pieces", frames)
        reading = asm.find("1")
        self.assertNotEqual(reading.status, READING_CONFIRMED)
        self.assertIsNone(reading.value)
        self.assertTrue(all(m.reason == "uncommitted" for m in asm.missed))

    def test_same_value_repeat_deduped_different_value_conflicts(self):
        frames = [
            low_frame(self.p, "l1", settlement=7, sequence=1, low=0x10),
            low_frame(self.p, "l1b", settlement=7, sequence=1, low=0x10),
            high_frame(self.p, "h1", settlement=7, sequence=1, high=0x00),
        ]
        asm = assemble_counter(self.p, "pieces", frames)
        self.assertEqual(asm.find("7").status, READING_CONFIRMED)

        frames = [
            low_frame(self.p, "l1", settlement=8, sequence=1, low=0x10),
            low_frame(self.p, "l2", settlement=8, sequence=1, low=0x11),
            high_frame(self.p, "h1", settlement=8, sequence=1, high=0x00),
        ]
        asm = assemble_counter(self.p, "pieces", frames)
        reading = asm.find("8")
        self.assertEqual(reading.status, READING_CONFLICT)
        self.assertIsNone(reading.value)
        self.assertTrue(any(m.reason == "conflict" for m in asm.missed))

    def test_same_instant_ordered_by_sequence(self):
        # Two settlements with the same event time, reversed arrival.
        frames = [
            high_frame(self.p, "a_h", settlement=1, sequence=1, high=0),
            low_frame(self.p, "a_l", settlement=1, sequence=1, low=0x10, ns=BASE_NS),
            high_frame(self.p, "b_h", settlement=2, sequence=2, high=0, ns=BASE_NS),
            low_frame(self.p, "b_l", settlement=2, sequence=2, low=0x11, ns=BASE_NS),
        ]
        asm = assemble_counter(self.p, "pieces", frames)
        self.assertEqual([r.settlement for r in asm.readings], ["1", "2"])

    def test_epoch_split_on_regression_and_full_scale(self):
        frames = [
            low_frame(self.p, "l1", settlement=1, sequence=1, low=0xFE),
            high_frame(self.p, "h1", settlement=1, sequence=1, high=0x00),
            low_frame(self.p, "l2", settlement=2, sequence=2, low=0xFF),
            high_frame(self.p, "h2", settlement=2, sequence=2, high=0x00),
            # regression -> new epoch
            low_frame(self.p, "l3", settlement=3, sequence=3, low=0x01),
            high_frame(self.p, "h3", settlement=3, sequence=3, high=0x00),
        ]
        asm = assemble_counter(self.p, "pieces", frames)
        epochs = {(r.settlement, r.epoch) for r in asm.readings}
        self.assertEqual(epochs, {("1", 0), ("2", 0), ("3", 1)})
        self.assertTrue(asm.split_intervals)
        self.assertEqual(asm.split_intervals[0].epoch, 0)

    def test_split_required_holds_day(self):
        # 0xFFFE -> 0x0001 wraps the full 16-bit range: needs a new epoch, so
        # the day cannot be paid as a single interval.
        frames = [
            low_frame(self.p, "l1", settlement=1, sequence=1, low=0xFE),
            high_frame(self.p, "h1", settlement=1, sequence=1, high=0xFF),
            low_frame(self.p, "l2", settlement=2, sequence=2, low=0x01),
            high_frame(self.p, "h2", settlement=2, sequence=2, high=0x00),
        ]
        asm = assemble_counter(self.p, "pieces", frames)
        self.assertEqual(asm.find("1").value, 0xFFFE)
        self.assertEqual(asm.find("2").value, 0x0001)
        self.assertEqual(asm.find("2").epoch, 1)
        self.assertTrue(asm.split_intervals)
        g = Group("g", (Employee("A", 1),))
        report = settle_day(asm, self.p.counter("pieces"), container="c", groups={"g": g},
                            shifts=(Shift("s", "g", "1", "2"),))
        self.assertEqual(report.shifts[0].group.status, GROUP_HELD)
        self.assertTrue(report.split_required)


class Payroll(unittest.TestCase):
    def setUp(self):
        self.p = product()

    def _assembly(self, *pairs):
        frames = []
        for i, (settlement, value) in enumerate(pairs, start=1):
            frames.append(low_frame(self.p, f"l{i}", settlement=settlement, sequence=i, low=value & 0xFF))
            frames.append(high_frame(self.p, f"h{i}", settlement=settlement, sequence=i, high=(value >> 8) & 0xFF))
        return assemble_counter(self.p, "pieces", frames)

    def test_two_groups_one_piece_each(self):
        # start 0x01FE, handover 0x01FF, end 0x0200.
        asm = self._assembly((1, 0x01FE), (2, 0x01FF), (3, 0x0200))
        day_group = Group("day", (Employee("E1", 1),))
        night = Group("night", (Employee("A", 2), Employee("B", 2)))
        spec = DaySpec(
            container="loom7-day1", counter="pieces",
            groups={"day": day_group, "night": night},
            shifts=(
                Shift("day1", "day", start_settlement="1", end_settlement="2",
                      other_group_boundary=None),
                Shift("night1", "night", start_settlement="2", end_settlement="3",
                      other_group_boundary=None),
            ),
        )
        report = settle_day(
            asm, self.p.counter("pieces"),
            container=spec.container, groups=spec.groups, shifts=spec.shifts,
        )
        by_shift = {s.shift_id: s.group for s in report.shifts}
        self.assertEqual(by_shift["day1"].pieces, 1)
        self.assertEqual(by_shift["night1"].pieces, 1)
        self.assertEqual(by_shift["day1"].fen, 1)
        self.assertEqual(by_shift["night1"].fen, 1)
        # equal points, A's number smaller -> A gets the single fen
        self.assertEqual(by_shift["night1"].allocations[0].amount, 1)
        self.assertEqual(by_shift["night1"].allocations[1].amount, 0)

    def test_missing_high_at_handover_does_not_fabricate(self):
        # Boundary 0x01FF low only; 0x0200 high present.  A naive assembler
        # would borrow 0x02 and compute 0x02FF.  Here the boundary is held.
        frames = [
            low_frame(self.p, "l1", settlement=1, sequence=1, low=0xFE),
            high_frame(self.p, "h1", settlement=1, sequence=1, high=0x01),
            low_frame(self.p, "l2", settlement=2, sequence=2, low=0xFF),
            low_frame(self.p, "l3", settlement=3, sequence=3, low=0x00),
            high_frame(self.p, "h3", settlement=3, sequence=3, high=0x02),
        ]
        asm = assemble_counter(self.p, "pieces", frames)
        boundary = asm.find("2")
        self.assertEqual(boundary.status, READING_INCOMPLETE)
        # Crossed half explicitly borrows the next high, but is not a boundary.
        crossed = asm.readings  # bibliography
        night = Group("night", (Employee("A", 1),))
        report = settle_day(
            asm, self.p.counter("pieces"),
            container="c", groups={"night": night},
            shifts=(Shift("n", "night", start_settlement="2", end_settlement="3"),),
        )
        slip = report.shifts[0].group
        self.assertEqual(slip.status, GROUP_HELD)
        self.assertIsNone(slip.fen)
        self.assertTrue(any("incomplete" in r for r in slip.reasons))

    def test_other_group_boundary_incomplete_keeps_total_withholds_split(self):
        # Day delta known (1 -> 3), but the handover reading is missing its low.
        frames = [
            low_frame(self.p, "l1", settlement=1, sequence=1, low=0x00),
            high_frame(self.p, "h1", settlement=1, sequence=1, high=0x01),
            high_frame(self.p, "h2", settlement=2, sequence=2, high=0x01),
            low_frame(self.p, "l3", settlement=3, sequence=3, low=0x02),
            high_frame(self.p, "h3", settlement=3, sequence=3, high=0x01),
        ]
        asm = assemble_counter(self.p, "pieces", frames)
        g = Group("g", (Employee("A", 1),))
        report = settle_day(
            asm, self.p.counter("pieces"),
            container="c", groups={"g": g},
            shifts=(Shift("s", "g", start_settlement="1", end_settlement="3",
                          other_group_boundary="2"),),
        )
        slip = report.shifts[0].group
        self.assertEqual(slip.status, GROUP_HELD)
        self.assertEqual(report.shifts[0].day_delta, 0x0102 - 0x0100)
        self.assertTrue(any("withheld" in r for r in slip.reasons))

    def test_integer_fen_split_largest_remainder(self):
        members = (Employee("1", 1), Employee("2", 1), Employee("3", 1))
        result = split_integer(10, members)
        self.assertEqual([a.amount for a in result.allocations], [4, 3, 3])
        self.assertEqual(result.leftover, 1)

    def test_split_follows_points_not_order(self):
        a = split_integer(1, (Employee("A", 1), Employee("B", 2)))
        self.assertEqual(a.amount_of("B"), 1)
        self.assertEqual(a.amount_of("A"), 0)

    def test_split_properties_random(self):
        import random

        rng = random.Random(4242)
        for _ in range(300):
            n = rng.randint(1, 7)
            members = tuple(
                Employee(f"{i:02d}", rng.randint(0, 5)) for i in range(n)
            )
            total_points = sum(m.points for m in members)
            if total_points == 0:
                continue
            total = rng.randint(0, 500)
            result = split_integer(total, members)
            # sums to the total, non-negative, and each member is within one
            # fen of their exact floor share
            self.assertEqual(sum(a.amount for a in result.allocations), total)
            for a in result.allocations:
                exact = Fraction(total * a.points, total_points)
                self.assertGreaterEqual(a.amount, exact.numerator // exact.denominator)
                self.assertLessEqual(a.amount, exact.numerator // exact.denominator + 1)
            # no one gets an extra when their remainder is smaller than another's
            for a in result.allocations:
                for b in result.allocations:
                    if a.extra == 0 and b.extra == 1:
                        if a.remainder == b.remainder:
                            # an extra went to a smaller employee number
                            self.assertLess(b.employee_id, a.employee_id)
                        else:
                            self.assertLess(a.remainder, b.remainder)


class WarehouseAssembly(unittest.TestCase):
    def test_assemble_from_warehouse(self):
        p = product()
        with tempfile.TemporaryDirectory() as tmp:
            with ObservationWarehouse(Path(tmp) / "c.db") as wh:
                events = [
                    {"frame_event_id": "l1", "bus": "L", "frame_id": 10, "occurred_at_ns": BASE_NS,
                     "payload_hex": low_frame(p, "x", 1, 1, 0x10)["payload_hex"]},
                    {"frame_event_id": "h1", "bus": "H", "frame_id": 11, "occurred_at_ns": BASE_NS + 1,
                     "payload_hex": high_frame(p, "y", 1, 1, 0x02)["payload_hex"]},
                ]
                stats = wh.ingest(p, events)
                self.assertEqual(stats.accepted, 2)
                asm = assemble_from_warehouse(p, "pieces", wh)
                self.assertEqual(asm.find("1").value, 0x0210)


class CrossedHalfTrap(unittest.TestCase):
    """The 0x01FE / 0x01FF / 0x0200 example from the brief."""

    def setUp(self):
        self.p = product()

    def test_naive_borrow_would_fabricate_02FF(self):
        frames = [
            low_frame(self.p, "h_low", settlement=2, sequence=5, low=0xFF),
            low_frame(self.p, "e_low", settlement=3, sequence=6, low=0x00),
            high_frame(self.p, "e_high", settlement=3, sequence=6, high=0x02),
        ]
        asm = assemble_counter(self.p, "pieces", frames)
        handover = asm.find("2")
        day_end = asm.find("3")
        self.assertEqual(handover.status, READING_INCOMPLETE)
        self.assertEqual(handover.reading.missing_segment, "high")
        # Diagnostic borrow would produce 0x02FF -- and is marked crossed.
        self.assertEqual(handover.naive_value(day_end), 0x02FF)
        crossed = handover.crossed_with("high", day_end)
        self.assertTrue(crossed.crossed)
        self.assertFalse(crossed.complete)
        # A complete reading of the same settlement blocks the borrow.
        asm2 = assemble_counter(self.p, "pieces", frames + [
            high_frame(self.p, "h_high", settlement=2, sequence=5, high=0x01),
        ])
        self.assertEqual(asm2.find("2").value, 0x01FF)


class SameInstantBoundary(unittest.TestCase):
    def test_handover_and_day_end_same_ns_use_sequence(self):
        p = product()
        ns = BASE_NS + 7
        frames = [
            low_frame(p, "b_low", settlement=10, sequence=50, low=0xFF, ns=ns),
            high_frame(p, "b_high", settlement=10, sequence=50, high=0x01, ns=ns),
            low_frame(p, "c_low", settlement=11, sequence=51, low=0x00, ns=ns),
            high_frame(p, "c_high", settlement=11, sequence=51, high=0x02, ns=ns),
        ]
        asm = assemble_counter(p, "pieces", frames)
        self.assertEqual([r.settlement for r in asm.readings], ["10", "11"])
        self.assertEqual(asm.find("10").value, 0x01FF)
        self.assertEqual(asm.find("11").value, 0x0200)


class MassRecalculation(unittest.TestCase):
    def test_many_slips_few_boundaries_recompute_queue(self):
        p = product()
        t = BASE_NS

        def frames(with_high):
            out = [
                low_frame(p, "h5", settlement=5, sequence=5, low=0xFF, ns=t),
                low_frame(p, "e6l", settlement=6, sequence=6, low=0x00, ns=t),
                high_frame(p, "e6h", settlement=6, sequence=6, high=0x02, ns=t),
            ]
            if with_high:
                out.append(high_frame(p, "h5h", settlement=5, sequence=5, high=0x01, ns=t + 1))
            return out

        before = assemble_counter(p, "pieces", frames(False))
        after = assemble_counter(p, "pieces", frames(True))
        groups = {
            f"crew{i:02d}": Group(f"crew{i:02d}", (Employee(f"W{i:02d}", 1),))
            for i in range(30)
        }
        shifts = tuple(Shift(f"n{i:02d}", f"crew{i:02d}", "5", "6") for i in range(30))
        spec = DaySpec("mass", "pieces", groups, shifts, fen_per_piece=1)
        ledger = PayrollLedger(p)
        ledger.record(spec, settle_day(before, p.counter("pieces"), container="mass",
                                       groups=groups, shifts=shifts))
        self.assertEqual(
            sum(1 for s in ledger.report("mass").shifts if s.group.status == GROUP_HELD), 30
        )
        old_pin5 = before.find("5").pin.pin_id
        self.assertEqual(len(ledger.citing(old_pin5)), 30)
        recalc = ledger.recalculate(after, old_assembly=before)
        self.assertEqual(recalc.containers, ("mass",))
        self.assertEqual(
            sorted(e.group_id for e in recalc.entries),
            sorted(f"crew{i:02d}" for i in range(30)),
        )
        self.assertTrue(all(e.new_fen == 1 for e in recalc.entries))
        self.assertEqual(
            sum(1 for s in ledger.report("mass").shifts if s.group.status == GROUP_SETTLED), 30
        )
        # After re-issue the slips now cite the confirmed reading's pin.
        pin5 = after.find("5").pin.pin_id
        self.assertEqual(len(ledger.citing(pin5)), 30)


class Recalculation(unittest.TestCase):
    def setUp(self):
        self.p = product()

    def test_late_proof_reopens_only_affected_containers(self):
        # Two loom-days.  Day A's end boundary 0x0100 is initially incomplete
        # (high missing) and its night slip is held; day B cites complete
        # readings only.
        a_frames = [
            low_frame(self.p, "a_l1", settlement=1, sequence=1, low=0x00),
            high_frame(self.p, "a_h1", settlement=1, sequence=1, high=0x01),
            low_frame(self.p, "a_l2", settlement=2, sequence=2, low=0x01),
            high_frame(self.p, "a_h2", settlement=2, sequence=2, high=0x01),
            low_frame(self.p, "a_l3", settlement=3, sequence=3, low=0x02),
            # high for settlement 3 missing
        ]
        old_asm = assemble_counter(self.p, "pieces", a_frames)
        b_frames = [
            low_frame(self.p, "b_l1", settlement=10, sequence=10, low=0x05),
            high_frame(self.p, "b_h1", settlement=10, sequence=10, high=0x00),
            low_frame(self.p, "b_l2", settlement=11, sequence=11, low=0x06),
            high_frame(self.p, "b_h2", settlement=11, sequence=11, high=0x00),
        ]
        b_asm = assemble_counter(self.p, "pieces", b_frames)

        g = Group("g", (Employee("A", 1),))
        spec_a = DaySpec("A", "pieces", {"g": g}, (Shift("sa", "g", "1", "3"),))
        spec_b = DaySpec("B", "pieces", {"g": g}, (Shift("sb", "g", "10", "11"),))

        ledger = PayrollLedger(self.p)
        ledger.record(spec_a, settle_day(old_asm, self.p.counter("pieces"), container="A",
                                         groups=spec_a.groups, shifts=spec_a.shifts))
        ledger.record(spec_b, settle_day(b_asm, self.p.counter("pieces"), container="B",
                                         groups=spec_b.groups, shifts=spec_b.shifts))
        self.assertEqual(ledger.report("A").shifts[0].group.status, GROUP_HELD)
        self.assertEqual(ledger.report("B").shifts[0].group.status, GROUP_SETTLED)

        # Late proof: the high half of settlement 3 arrives.
        new_asm = assemble_counter(self.p, "pieces", a_frames + [
            high_frame(self.p, "a_h3", settlement=3, sequence=3, high=0x01),
        ])
        affected = ledger.affected_containers(old_asm, new_asm)
        self.assertEqual(affected, ["A"])
        recalc = ledger.recalculate(new_asm, old_assembly=old_asm)
        self.assertEqual(recalc.containers, ("A",))
        entries = [e for e in recalc.entries if e.group_id == "g"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].old_fen, None)
        self.assertEqual(entries[0].new_fen, 2)
        self.assertEqual(ledger.report("A").shifts[0].group.fen, 2)


if __name__ == "__main__":
    unittest.main()
