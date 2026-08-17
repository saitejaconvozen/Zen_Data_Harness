from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from zen_agent.mongodb_credential import EphemeralMongoCredential


class MongoCredentialTests(unittest.TestCase):
    def test_hidden_credential_is_injected_and_removed(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MONGODB_URI", None)
            credential = EphemeralMongoCredential.prompt(lambda _prompt: "mongodb://private.example/test")
            credential.inject()
            self.assertEqual(os.environ["MONGODB_URI"], "mongodb://private.example/test")
            credential.restore()
            self.assertNotIn("MONGODB_URI", os.environ)
            self.assertEqual(credential.uri, "")

    def test_invalid_scheme_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must start"):
            EphemeralMongoCredential.prompt(lambda _prompt: "https://not-mongo")


if __name__ == "__main__":
    unittest.main()
