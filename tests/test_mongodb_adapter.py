from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from zen_agent.adapters.mongodb import MongoConfigurationError, MongoSettings, evaluate_privileges


class MongoAdapterTests(unittest.TestCase):
    def test_credentials_are_required_from_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MongoConfigurationError):
                MongoSettings.from_environment()

    def test_write_capability_is_rejected_by_audit(self):
        audit = evaluate_privileges(
            {
                "authenticatedUserRoles": [{"role": "readWrite", "db": "test"}],
                "authenticatedUserPrivileges": [
                    {"resource": {"db": "test"}, "actions": ["find", "insert", "update"]}
                ],
            }
        )
        self.assertFalse(audit.server_enforced_read_only)
        self.assertEqual(audit.write_actions, ("insert", "update"))

    def test_read_only_privileges_pass(self):
        audit = evaluate_privileges(
            {
                "authenticatedUserRoles": [{"role": "read", "db": "test"}],
                "authenticatedUserPrivileges": [
                    {"resource": {"db": "test"}, "actions": ["find", "listIndexes"]}
                ],
            }
        )
        self.assertTrue(audit.server_enforced_read_only)
        self.assertEqual(audit.write_actions, ())
