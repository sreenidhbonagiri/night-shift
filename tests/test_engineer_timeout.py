"""Regression test: the Engineer must not silently override the shared
client's configured model timeout (OLLAMA_TIMEOUT).

Independently reproduced: engineer_agent called
_call(..., timeout=180.0), overriding OLLAMA_TIMEOUT=300 on the deployed
host, so the Engineer never actually tested the configured 300-second
timeout.
"""
import os
import unittest
from unittest.mock import patch

from app import engineer_agent
from tests.test_grounded_live import fake_model_fn, make_interpretation_response


class TestEngineerHonorsConfiguredTimeout(unittest.TestCase):

    def test_engineer_does_not_override_configured_timeout(self):
        """propose_upgrade must call the model with timeout=None (use the
        shared client's configured default), never a hardcoded value."""
        captured = {}

        def spy_model_fn(system_prompt, user_prompt, expect_json=True,
                         timeout=None):
            captured["timeout"] = timeout
            return fake_model_fn(make_interpretation_response())(
                system_prompt, user_prompt, expect_json=expect_json,
                timeout=timeout)

        out = engineer_agent.propose_upgrade("ctx", None, model_fn=spy_model_fn)
        # The call must succeed through the spied path.
        self.assertIsNotNone(out["proposal"])
        # The critical assertion: no hardcoded timeout override.
        self.assertIsNone(
            captured["timeout"],
            "Engineer passed a hardcoded timeout, overriding the shared "
            "client's configured OLLAMA_TIMEOUT setting")

    def test_shared_client_reads_env_timeout(self):
        """The shared client must honor OLLAMA_TIMEOUT when set."""
        import importlib
        import app.ollama_client as oc
        with patch.dict(os.environ, {"OLLAMA_TIMEOUT": "300"}):
            importlib.reload(oc)
            try:
                # call_model with a stubbed httpx.post that records timeout.
                recorded = {}

                def fake_post(url, json=None, timeout=None):
                    recorded["timeout"] = timeout

                    class R:
                        status_code = 200

                        def raise_for_status(self):
                            pass

                        def json(self):
                            return {"response": "{}"}

                    return R()

                with patch.object(oc.httpx, "post", side_effect=fake_post):
                    oc.call_model("s", "u")
                self.assertEqual(recorded["timeout"], 300.0)
            finally:
                importlib.reload(oc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
