import json
import tempfile
import unittest
from pathlib import Path

from agent.memory.config import MemoryConfig, set_global_memory_config
from agent.social_bridge.store import PENDING_STATUS, SENT_STATUS, BridgeStore, compute_pair_id


class TestSocialBridgeStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.workspace = Path(self.tmp.name)
        set_global_memory_config(MemoryConfig(workspace_root=str(self.workspace)))
        self.store = BridgeStore()

    def tearDown(self):
        set_global_memory_config(MemoryConfig())
        self.tmp.cleanup()

    def test_pair_id_is_stable_for_user_order(self):
        left = compute_pair_id("memory_b", "memory_a")
        right = compute_pair_id("memory_a", "memory_b")

        self.assertEqual(left, right)
        self.assertTrue(left.startswith("pair_"))

    def test_register_and_list_visible_users(self):
        self.store.register_user("actor:a", "memory_a", "Alice")
        self.store.register_user("actor:b", "memory_b", "Bob", {"channel": "web"})

        visible = self.store.list_visible_users("actor:a")

        self.assertEqual([user.actor_user_id for user in visible], ["actor:b"])
        self.assertEqual(visible[0].metadata, {"channel": "web"})

    def test_relationship_writes_database_and_relation_memory_files(self):
        self.store.register_user("actor:a", "memory_a", "Alice")
        self.store.register_user("actor:b", "memory_b", "Bob")

        relationship = self.store.set_relationship("actor:a", "actor:b", "met through project notes")

        self.assertEqual(relationship.pair_id, compute_pair_id("memory_a", "memory_b"))
        relation_dir = self.workspace / "memory" / "relations" / relationship.pair_id
        memory_text = (relation_dir / "MEMORY.md").read_text(encoding="utf-8")
        daily_files = list(relation_dir.glob("????-??-??.md"))

        self.assertIn("actor:a -> actor:b", memory_text)
        self.assertIn("met through project notes", memory_text)
        self.assertEqual(len(daily_files), 1)
        self.assertIn("met through project notes", daily_files[0].read_text(encoding="utf-8"))

    def test_relationship_is_pair_scoped_for_both_directions(self):
        self.store.register_user("actor:a", "memory_a", "Alice")
        self.store.register_user("actor:b", "memory_b", "Bob")

        first = self.store.set_relationship("actor:a", "actor:b", "spouses")
        second = self.store.set_relationship("actor:b", "actor:a", "working through a disagreement")

        self.assertEqual(first.pair_id, second.pair_id)
        self.assertIn("spouses", second.relation_text)
        self.assertIn("working through a disagreement", second.relation_text)
        reverse = self.store.get_relationship("actor:a", "actor:b")
        self.assertEqual(reverse.pair_id, first.pair_id)
        self.assertIn("working through a disagreement", reverse.relation_text)

    def test_create_mark_and_list_pending_message(self):
        self.store.register_user("actor:a", "memory_a", "Alice")
        self.store.register_user("actor:b", "memory_b", "Bob")
        self.store.set_relationship("actor:a", "actor:b", "trusted collaborator")

        message = self.store.create_bridge_message(
            "actor:a",
            "actor:b",
            "Please review this idea.",
            {"source": "unit-test"},
        )
        pending = self.store.list_pending_for_actor("actor:b")
        sender_pending = self.store.list_pending_for_actor("actor:a")

        self.assertEqual(message.status, PENDING_STATUS)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].message.message_id, message.message_id)
        self.assertEqual(len(sender_pending), 1)
        self.assertEqual(sender_pending[0].message.message_id, message.message_id)
        self.assertEqual(pending[0].sender.display_name, "Alice")
        self.assertEqual(pending[0].relationship.relation_text, "trusted collaborator")

        sent = self.store.mark_sent(message.message_id, {"delivered": True})
        self.assertEqual(sent.status, SENT_STATUS)
        self.assertEqual(sent.result, {"delivered": True})
        self.assertEqual(self.store.list_pending_for_actor("actor:b"), [])
        self.assertEqual(self.store.list_pending_for_actor("actor:a"), [])

        pending_again = self.store.mark_pending(message.message_id)
        self.assertEqual(pending_again.status, PENDING_STATUS)
        self.assertIsNone(pending_again.sent_at)

    def test_dtos_are_json_serializable(self):
        user = self.store.register_user("actor:a", "memory_a", "Alice")

        encoded = json.dumps(BridgeStore.dto_to_dict(user), ensure_ascii=False)

        self.assertIn("actor:a", encoded)


if __name__ == "__main__":
    unittest.main()
