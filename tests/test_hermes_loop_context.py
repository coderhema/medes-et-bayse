from types import SimpleNamespace

from medes_et_bayse.hermes.loop import HermesAgent, HermesLoopConfig, MAX_CONTEXT_CHARS, MAX_HISTORY_LOGS


class FakeStore:
    def __init__(self):
        self.memory = {
            "last_prediction": [SimpleNamespace(value='{"event_title":"A very long event title","market_title":"A very long market title","side":"buy","outcome":"YES","price":0.42,"confidence":0.91,"signal":"trade","currency":"USD","rationale":"' + 'x' * 4000 + '"}')],
            "last_trade": [SimpleNamespace(value='{"attempted":true,"dry_run":true,"status":"dry_run","message":"' + 'y' * 4000 + '","notional":5,"price":0.42,"side":"buy","outcome":"YES","order":{"event_id":"evt-1","market_id":"mkt-1","side":"BUY","outcome":"YES","amount":5,"currency":"USD","price":0.42}}')],
            "last_reflection": [SimpleNamespace(value='{"summary":"' + 'z' * 4000 + '","lessons":["one","two","three","four","five","six"]}')],
            "last_framework_response": [SimpleNamespace(value='a' * 8000)],
        }
        self.logs = [
            SimpleNamespace(id=i, level="info", category="category-" + str(i), message="m" * 1000, created_at="2026-05-05T00:00:00Z")
            for i in range(1, 10)
        ]

    def recall(self, namespace, key):
        return self.memory.get(key, [])

    def recent_logs(self, limit=20, category=None):
        return self.logs[:limit]


class DummyClient:
    pass


def test_framework_context_is_compact_and_trimmed():
    agent = HermesAgent.__new__(HermesAgent)
    agent.client = DummyClient()
    agent.store = FakeStore()
    agent.config = HermesLoopConfig()
    snapshot = agent._framework_snapshot("run-123")
    assert len(snapshot["recent_logs"]) == MAX_HISTORY_LOGS
    assert len(snapshot["memory"]["last_framework_response"]) <= 260
    assert snapshot["memory"]["last_prediction"]["rationale"].endswith("…")
    context = agent._framework_context_text("run-123")
    assert len(context) <= MAX_CONTEXT_CHARS
    assert '"rationale"' in context
    assert 'category-9' not in context
