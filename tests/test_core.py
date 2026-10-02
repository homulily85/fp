import random
import tempfile
import time
import unittest
from pathlib import Path

from ffp_sat.heuristic import portfolio, threat
from ffp_sat.instance import read_instance
from ffp_sat.preprocess import preprocess
from ffp_sat.simulator import simulate

from .helpers import graph


class CoreTests(unittest.TestCase):
    def test_dataset(self):
        for path in Path("dataset").glob("*.in"):
            instance = read_instance(path)
            self.assertIn(instance.n, (50, 100, 500, 1000))
            self.assertEqual(instance.initial_fire, frozenset({0}))

    def test_parser(self):
        valid = "7\n3\n1\nmetadata is not fire\n1\n2\n0 1\n"
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "instance.in"
            path.write_text(valid)
            self.assertEqual(read_instance(path).initial_fire, frozenset({2}))
            for invalid in [
                valid + "1 2\n",
                valid.replace("0 1", "0 0"),
                valid.replace("0 1", "0 3"),
                valid.replace("\n1\n2\n", "\n2\n2\n"),
                "0\n3\n2\nx\n1\n0\n0 1\n1 0\n",
            ]:
                path.write_text(invalid)
                with self.assertRaises(ValueError):
                    read_instance(path)

    def test_synchronous_and_order(self):
        instance = graph(4, [(0, 1), (1, 2), (2, 3)])
        saved = simulate(instance, 1, [[1]])
        self.assertEqual(saved.k, 1)
        self.assertEqual(saved.containment_time, 1)
        result = simulate(instance, 1, [[], [2]])
        self.assertEqual(result.burned, frozenset({0, 1}))
        self.assertEqual(result.containment_time, 2)
        self.assertEqual(simulate(instance, 1, []).containment_time, 3)
        self.assertEqual(simulate(graph(3, [(0, 1), (1, 2)]), 1, []).containment_time, 2)
        self.assertEqual(simulate(graph(2, []), 1, []).containment_time, 0)
        for schedule in [[[0]], [[1, 1]], [[1, 2]], [[], [1]]]:
            with self.assertRaises(ValueError):
                simulate(instance, 1, schedule)
        self.assertEqual(simulate(instance, 1, [[1], [0]]).schedule, ((1,),))

    def test_disconnected_and_multi_fire(self):
        instance = graph(5, [(0, 1), (2, 3)], fire=(0, 2))
        result = simulate(instance, 2, [[1, 3]])
        self.assertEqual(result.k, 2)
        distance, lower = preprocess(instance, 1)
        self.assertEqual(lower, 3)
        self.assertEqual(distance[:4], [0, 1, 0, 1])
        self.assertEqual(distance[4], float("inf"))

    def test_heuristics(self):
        instance = graph(7, [(0, v) for v in range(1, 7)])
        first = threat(instance, 2)
        self.assertEqual(first.k, 5)
        self.assertEqual(
            threat(instance, 2, "random", random.Random(3)), threat(instance, 2, "random", random.Random(3))
        )
        best, frontier = portfolio(instance, 2, first, time.monotonic() + 0.01, 0, lambda s: None)
        self.assertEqual(best.k, 5)
        for i, left in enumerate(frontier):
            for j, right in enumerate(frontier):
                if i != j:
                    self.assertFalse(
                        left.k <= right.k
                        and left.containment_time <= right.containment_time
                        and (left.k < right.k or left.containment_time < right.containment_time)
                    )
