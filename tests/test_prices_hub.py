from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cost_per_task.pricing import PricingTable
from cost_per_task.prices_hub import build_table, diff_tables, fetch_hub, load_hub, write_table

FIXTURE = Path(__file__).parent / "fixtures" / "hub_sample.json"


def test_anthropic_table_maps_ids_and_applies_cache_write_rules():
    table, warnings = build_table(load_hub(FIXTURE), "anthropic")
    assert table["as_of"] == "2026-09-02"
    assert table["currency"] == "USD"
    assert "optimtoken.optimnow.io" in table["source"]
    # dots in hub ids become hyphens, matching the API model ids
    haiku = table["models"]["claude-haiku-4-5"]
    assert haiku["input_per_mtok"] == 1.0
    assert haiku["cache_read_per_mtok"] == 0.1
    assert haiku["cache_write_5m_per_mtok"] == 1.25
    assert haiku["cache_write_1h_per_mtok"] == 2.0
    assert haiku["output_per_mtok"] == 5.0
    assert haiku["reasoning_per_mtok"] is None
    # no cached price: skipped, never guessed
    assert "claude-mystery" not in table["models"]
    assert any("Claude Mystery" in w and "not guessing" in w for w in warnings)
    # cache read far below the usual 0.1x: flagged but kept for the diff
    assert "claude-fable-5-1" in table["models"]
    assert any("suspicious Claude Fable 5.1" in w for w in warnings)


def test_openai_cache_write_rule_depends_on_model_generation():
    table, _ = build_table(load_hub(FIXTURE), "openai")
    assert table["models"]["gpt-5.5"]["cache_write_5m_per_mtok"] == 0.0
    assert table["models"]["gpt-5.6-sol"]["cache_write_5m_per_mtok"] == 5.0  # 1.25 x 4
    assert table["models"]["o3"]["cache_write_5m_per_mtok"] == 0.0
    assert "grok-4.6" not in table["models"]


def test_overrides_replace_the_default_mapping():
    table, _ = build_table(load_hub(FIXTURE), "anthropic", {"anthropic/claude-sonnet-5": "claude-sonnet-5-custom"})
    assert "claude-sonnet-5-custom" in table["models"]
    assert "claude-sonnet-5" not in table["models"]


def test_unknown_provider_rejected():
    with pytest.raises(ValueError):
        build_table(load_hub(FIXTURE), "google")


def test_built_table_loads_as_a_pricing_table(tmp_path):
    table, _ = build_table(load_hub(FIXTURE), "openai")
    path = tmp_path / "openai.json"
    write_table(table, path)
    loaded = PricingTable.load(path)
    assert loaded.rates_for("gpt-5.5-2026-04-01").output_per_mtok == 30.0


def test_diff_reports_added_removed_and_changed():
    new, _ = build_table(load_hub(FIXTURE), "anthropic")
    old = json.loads(json.dumps(new))
    old["as_of"] = "2026-08-27"
    old["models"]["claude-haiku-4-5"]["output_per_mtok"] = 4.0
    old["models"]["claude-opus-5"] = old["models"].pop("claude-fable-5-1")
    lines = diff_tables(old, new)
    assert "as_of: 2026-08-27 -> 2026-09-02" in lines
    assert any(line.startswith("+ claude-fable-5-1") for line in lines)
    assert any(line.startswith("- claude-opus-5") for line in lines)
    assert "~ claude-haiku-4-5.output_per_mtok: 4.0 -> 5.0" in lines
    assert diff_tables(new, new) == []
    assert any(line.startswith("+ ") for line in diff_tables(None, new))


class _HubHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        payload = FIXTURE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_fetch_hub_over_http():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        hub = fetch_hub(f"http://127.0.0.1:{server.server_address[1]}/api/llm-models")
        assert hub["meta"]["total"] == 8
    finally:
        server.shutdown()
        server.server_close()
