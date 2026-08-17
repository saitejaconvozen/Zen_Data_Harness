from __future__ import annotations

import json
from pathlib import Path
import threading
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from zen_agent.coding_state import CodingStateStore
from zen_agent.gateway import create_gateway_server


class GatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.store = CodingStateStore(Path(self.temporary.name) / "coding.db")
        self.server = create_gateway_server(self.store, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.temporary.cleanup()

    def request(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> tuple[int, dict[str, object]]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {} if body is None else {"Content-Type": "application/json"}
        request = Request(self.base + path, data=data, headers=headers, method=method)
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())

    def error(self, method: str, path: str, body: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {} if body is None else {"Content-Type": "application/json"}
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            urlopen(request, timeout=2)
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())
        self.fail("request unexpectedly succeeded")

    def test_health_create_list_get_and_patch_session(self):
        status, health = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")

        status, created = self.request(
            "POST",
            "/v1/sessions",
            {"objective": "Fix tests", "workspace": self.temporary.name, "metadata": {"source": "test"}},
        )
        self.assertEqual(status, 201)
        session = created["session"]
        session_id = session["id"]

        _, listing = self.request("GET", "/v1/sessions?status=PLANNED&limit=10")
        self.assertEqual(listing["count"], 1)
        _, fetched = self.request("GET", f"/v1/sessions/{session_id}")
        self.assertEqual(fetched["session"]["objective"], "Fix tests")
        _, patched = self.request(
            "PATCH", f"/v1/sessions/{session_id}", {"status": "RUNNING"}
        )
        self.assertEqual(patched["session"]["status"], "RUNNING")

    def test_feedback_steering_cancel_and_incremental_events(self):
        session_id = self.store.create_session("Work", self.temporary.name)
        _, feedback = self.request(
            "POST",
            f"/v1/sessions/{session_id}/feedback",
            {"message": "Please add tests", "author": "operator"},
        )
        self.assertEqual(feedback["feedback"]["kind"], "feedback")
        _, steering = self.request(
            "POST",
            f"/v1/sessions/{session_id}/steering",
            {"message": "Preserve compatibility"},
        )
        self.assertEqual(steering["feedback"]["kind"], "steering")
        _, cancelled = self.request(
            "POST", f"/v1/sessions/{session_id}/cancel", {"reason": "stop now"}
        )
        self.assertTrue(cancelled["session"]["cancel_requested"])

        _, first_page = self.request("GET", f"/v1/sessions/{session_id}/events?limit=2")
        self.assertEqual(first_page["count"], 2)
        _, second_page = self.request(
            "GET", f"/v1/sessions/{session_id}/events?after={first_page['next_after']}"
        )
        self.assertGreaterEqual(second_page["count"], 1)
        _, pending = self.request("GET", f"/v1/sessions/{session_id}/feedback?pending=true")
        self.assertEqual(pending["count"], 2)

    def test_errors_are_json_and_external_binding_is_rejected(self):
        code, missing = self.error("GET", "/v1/sessions/no-such-session")
        self.assertEqual(code, 404)
        self.assertEqual(missing["error"]["code"], "not_found")
        code, invalid = self.error(
            "POST", "/v1/sessions", {"objective": "", "workspace": self.temporary.name}
        )
        self.assertEqual(code, 400)
        self.assertEqual(invalid["error"]["code"], "invalid_request")
        with self.assertRaisesRegex(ValueError, "loopback"):
            create_gateway_server(self.store, host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()
