from __future__ import annotations

import unittest

from zen_agent.factory_planner import shortlist_agents


def agent(agent_id, count, project="", languages=("en-IN",)):
    return {
        "agent_id": agent_id,
        "conversation_count": count,
        "project_name": project,
        "languages": list(languages),
    }


class ShortlistTests(unittest.TestCase):
    """A full inventory overruns the planner's input limit; the slate is bounded."""

    def test_limit_is_respected(self) -> None:
        agents = [agent(f"a{i:03d}", 100, project=f"p{i}") for i in range(500)]
        self.assertEqual(len(shortlist_agents(agents, limit=120, min_conversations=20)), 120)

    def test_agents_below_the_floor_are_dropped(self) -> None:
        agents = [agent("busy", 50), agent("quiet", 5), agent("empty", 0)]
        picked = {a["agent_id"] for a in shortlist_agents(agents, limit=10, min_conversations=20)}
        self.assertEqual(picked, {"busy"})

    def test_selection_spreads_across_projects(self) -> None:
        # One project has far more agents; volume alone would crowd out the rest.
        agents = [agent(f"big{i}", 1000, project="crowded") for i in range(50)]
        agents += [agent(f"small{i}", 100, project=f"niche{i}") for i in range(5)]
        picked = shortlist_agents(agents, limit=10, min_conversations=20)
        projects = {a["project_name"] for a in picked}
        self.assertIn("crowded", projects)
        self.assertGreater(len(projects), 1, "selection collapsed onto one project")

    def test_highest_volume_agent_wins_inside_a_group(self) -> None:
        agents = [
            agent("low", 30, project="p"),
            agent("high", 900, project="p"),
        ]
        picked = shortlist_agents(agents, limit=1, min_conversations=20)
        self.assertEqual(picked[0]["agent_id"], "high")

    def test_selection_is_deterministic(self) -> None:
        agents = [agent(f"a{i}", 100, project=f"p{i % 7}") for i in range(60)]
        first = [a["agent_id"] for a in shortlist_agents(agents, limit=20, min_conversations=20)]
        second = [a["agent_id"] for a in shortlist_agents(agents, limit=20, min_conversations=20)]
        self.assertEqual(first, second)

    def test_thin_inventory_still_plans(self) -> None:
        # Nothing clears the floor, but the run should not stall with an empty slate.
        agents = [agent("a", 3), agent("b", 9)]
        picked = shortlist_agents(agents, limit=5, min_conversations=20)
        self.assertEqual([a["agent_id"] for a in picked], ["b", "a"])

    def test_no_duplicates(self) -> None:
        agents = [agent(f"a{i}", 100, project=f"p{i % 3}") for i in range(30)]
        picked = shortlist_agents(agents, limit=25, min_conversations=20)
        ids = [a["agent_id"] for a in picked]
        self.assertEqual(len(ids), len(set(ids)))

    def test_zero_limit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            shortlist_agents([agent("a", 100)], limit=0, min_conversations=20)


if __name__ == "__main__":
    unittest.main()
