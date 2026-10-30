"""Nemotron rework tests -- canary generation, forensic narrative, client path.

All transports are mocked: zero network calls. Plain asserts, prints PASS.
"""
import json
import os

from continuity.canary import (
    CANARY_TASKS, ORACLES, CanaryRunner, NemotronCanaryGenerator,
    StaticCanaryGenerator, default_canary_generator,
)
from continuity.diagnostician import (
    NemotronDiagnostician, StubDiagnostician, default_diagnostician,
)
from continuity.nemotron import DEFAULT_BASE_URL, DEFAULT_MODEL, NemotronClient
from test_continuity import make_world


def _scenarios_transport(scenarios):
    calls = []

    def transport(body):
        calls.append(body)
        return json.dumps(scenarios)
    transport.calls = calls
    return transport


def _scenarios():
    return [
        {"name": "cache-stampede", "incident": {"service": "cart-api", "symptom": "5xx spike", "severity": "P1"}},
        {"name": "queue-backlog", "incident": {"service": "billing-worker", "symptom": "lag 40k msgs", "severity": "P2"}},
        {"name": "dns-flap", "incident": {"service": "edge-dns", "symptom": "intermittent NXDOMAIN", "severity": "P1"},
         "diagnosis": {"root_cause": "stale zone", "evidence": ["dig log"]}},
    ]


def _no_key_env():
    old = os.environ.pop("NEBIUS_API_KEY", None)
    return old


def _restore_key(old):
    if old is not None:
        os.environ["NEBIUS_API_KEY"] = old


def _no_live_env():
    old = os.environ.pop("CONTINUITY_LIVE", None)
    return old


def _restore_live(old):
    if old is not None:
        os.environ["CONTINUITY_LIVE"] = old


def test_generator_falls_back_without_key():
    old = _no_key_env()
    live_old = _no_live_env()
    try:
        gen = default_canary_generator()
        assert isinstance(gen, StaticCanaryGenerator), type(gen)
        assert gen.source == "static"
        tasks = gen.generate("remediation")
        assert len(tasks) == 1 and tasks[0].name == "remediation-canary-1"
        assert tasks[0].oracle is ORACLES["remediation"]
    finally:
        _restore_key(old)
        _restore_live(live_old)
    print("PASS generator: no key -> static fallback, deterministic oracle")


def test_generator_stays_offline_with_key_unless_live():
    # Credit-safety: key presence alone must NEVER select the network path.
    old = _no_key_env()
    live_old = _no_live_env()
    try:
        os.environ["NEBIUS_API_KEY"] = "<redacted>"
        gen = default_canary_generator()
        assert isinstance(gen, StaticCanaryGenerator), type(gen)
        assert gen.source == "static"
    finally:
        _restore_key(old)
        _restore_live(live_old)
        os.environ.pop("NEBIUS_API_KEY", None)
    print("PASS generator: key set but no live opt-in -> stays static (offline)")


def test_generator_uses_nemotron_with_live_opt_in():
    old = _no_key_env()
    live_old = _no_live_env()
    try:
        os.environ["NEBIUS_API_KEY"] = "<redacted>"
        gen = default_canary_generator(live=True)
        assert isinstance(gen, NemotronCanaryGenerator), type(gen)
        assert gen.source == "nemotron" and gen.available
        os.environ["CONTINUITY_LIVE"] = "1"
        gen2 = default_canary_generator()
        assert isinstance(gen2, NemotronCanaryGenerator), type(gen2)
    finally:
        _restore_key(old)
        _restore_live(live_old)
        os.environ.pop("NEBIUS_API_KEY", None)
    print("PASS generator: live=True or CONTINUITY_LIVE=1 + key -> Nemotron selected")


def test_nemotron_generates_three_scenarios():
    gen = NemotronCanaryGenerator(
        client=NemotronClient(api_key="<redacted>",
                              transport=_scenarios_transport(_scenarios())))
    tasks = gen.generate("remediation")
    assert len(tasks) == 3, [t.name for t in tasks]
    assert [t.name for t in tasks] == ["cache-stampede", "queue-backlog", "dns-flap"]
    assert all(t.role == "remediation" for t in tasks)
    # Every generated scenario is judged by the DETERMINISTIC oracle.
    assert all(t.oracle is ORACLES["remediation"] for t in tasks)
    assert tasks[2].synthetic_ctx["diagnosis"]["root_cause"] == "stale zone"
    print("PASS generator: 3 Nemotron scenarios, deterministic oracle attached")


def test_generated_probes_judged_deterministically():
    gen = NemotronCanaryGenerator(
        client=NemotronClient(api_key="<redacted>",
                              transport=_scenarios_transport(_scenarios())))
    tasks = gen.generate("remediation")
    runner = CanaryRunner()
    _, _, primaries = make_world()
    healthy = [runner.run(primaries["remediation"], t).passed for t in tasks]
    assert all(healthy), "healthy agent must pass Nemotron-written probes"
    primaries["remediation"].backend.mode = "gray"
    degraded = [runner.run(primaries["remediation"], t).passed for t in tasks]
    assert not any(degraded), "gray agent must fail Nemotron-written probes"
    print("PASS oracle: Nemotron-written probes judged deterministically "
          "(healthy passes, gray fails)")


def test_generator_never_raises_on_garbage():
    def garbage(body):
        return "sure, here are some scenarios (not json at all)"
    gen = NemotronCanaryGenerator(
        client=NemotronClient(api_key="<redacted>", transport=garbage,
                              backoff_base_s=0))

    def dead(body):
        raise ConnectionError("nope")
    gen2 = NemotronCanaryGenerator(
        client=NemotronClient(api_key="<redacted>", transport=dead,
                              backoff_base_s=0, retries=0))
    for g in (gen, gen2):
        tasks = g.generate("remediation")
        assert len(tasks) == 1 and tasks[0].name == "remediation-canary-1"
    print("PASS generator: garbage output / dead transport -> silent static fallback")


def test_narrative_labels_and_never_verdict():
    stub = StubDiagnostician()
    report = {
        "failure_windows": [{"agent": "remediation", "first_anomaly_tick": 7,
                             "failover_tick": 7, "reason": "canary failed"}],
        "affected_work": {"INC-1042": "completed clean"},
        "duplicates_prevented": 0,
        "human_review": [],
        "timeline": [],
    }
    s = stub.incident_narrative(report)
    assert s.startswith("stub-narrative"), s[:60]

    def narrative_transport(body):
        assert "forensic summary" in body["messages"][1]["content"]
        return "The remediation role failed over cleanly; no work was lost."
    nd = NemotronDiagnostician(api_key="<redacted>", transport=narrative_transport,
                               backoff_base_s=0)
    n = nd.incident_narrative(report)
    assert n.startswith("nemotron-narrative"), n[:60]
    assert "(advisory only)" in n

    # Advisory means: calling it changes nothing about the verdict path.
    from test_continuity import incident, process_incident
    layer, store, primaries = make_world(diagnostician=nd)
    inc = incident("INC-9001")
    process_incident(layer, store, primaries, inc)
    assert layer.sentinel.state_of("remediation") == "HEALTHY"
    n2 = nd.incident_narrative(layer.ledger.report())
    assert layer.sentinel.state_of("remediation") == "HEALTHY"
    assert "nemotron-narrative" in n2
    print("PASS narrative: labeled advisory, never alters a verdict")


def test_client_platform_facts():
    assert DEFAULT_BASE_URL == "https://api.tokenfactory.nebius.com/v1", DEFAULT_BASE_URL
    assert DEFAULT_MODEL == "nvidia/Nemotron-3.5-Super-VL", DEFAULT_MODEL
    c = NemotronClient(api_key="<redacted>")
    assert c.base_url == DEFAULT_BASE_URL and c.model == DEFAULT_MODEL
    print("PASS client: base URL (.com) and default model match verified docs")


def test_client_request_path():
    import continuity.nemotron as nemotron_mod
    seen = {}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "  hello  "}}]}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers, timeout=timeout)
        return FakeResp()

    real_post = nemotron_mod.requests.post
    nemotron_mod.requests.post = fake_post
    try:
        c = NemotronClient(api_key="<redacted>", timeout_s=11)
        assert c.chat([{"role": "user", "content": "hi"}]) == "hello"
    finally:
        nemotron_mod.requests.post = real_post
    assert seen["url"] == "https://api.tokenfactory.nebius.com/v1/chat/completions", seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer <redacted>"
    assert seen["json"]["model"] == "nvidia/Nemotron-3.5-Super-VL"
    assert seen["json"]["messages"] == [{"role": "user", "content": "hi"}]
    assert seen["timeout"] == 11
    print("PASS client: POST path, Bearer auth, body shape, parsing all verified")


def test_default_diagnostician_still_key_gated():
    # Key alone -> stub. Live opt-in + key -> Nemotron. Never live by accident.
    old = _no_key_env()
    live_old = _no_live_env()
    try:
        assert isinstance(default_diagnostician(), StubDiagnostician)
        os.environ["NEBIUS_API_KEY"] = "<redacted>"
        assert isinstance(default_diagnostician(), StubDiagnostician)
        assert isinstance(default_diagnostician(live=True),
                          NemotronDiagnostician)
    finally:
        _restore_key(old)
        _restore_live(live_old)
        os.environ.pop("NEBIUS_API_KEY", None)
    print("PASS diagnostician: stub by default; Nemotron only with live opt-in + key")


def test_full_world_makes_zero_http_calls_with_key_set():
    # Strong tripwire (Gap B): patch requests.post at the transport
    # boundary, run the whole offline world with a (fake) key set, and
    # assert the HTTP call count stays exactly zero.
    from unittest import mock
    import continuity.nemotron as nemotron_mod
    old = _no_key_env()
    live_old = _no_live_env()
    calls = []

    def _boom(*a, **k):
        calls.append((a, k))
        raise AssertionError("HTTP call attempted in offline mode")

    try:
        os.environ["NEBIUS_API_KEY"] = "<redacted>"
        with mock.patch.object(nemotron_mod.requests, "post", _boom):
            layer, store, primaries = make_world()
            assert layer.canary_generator.source == "static"
            from test_continuity import process_incident, incident
            process_incident(layer, store, primaries, incident("INC-9001"))
            kinds = [e["kind"] for e in layer.ledger.timeline]
            assert "canary_probes_generated" in kinds
        assert calls == [], f"HTTP calls attempted: {len(calls)}"
    finally:
        _restore_key(old)
        _restore_live(live_old)
        os.environ.pop("NEBIUS_API_KEY", None)
    print("PASS credit-safety: full world, key set, requests.post tripwire -> zero HTTP calls")


def test_direct_client_construction_still_gated():
    # Gap A: the transport boundary enforces opt-in even when the
    # factories are bypassed. Mocked transports stay usable offline.
    old = _no_key_env()
    live_old = _no_live_env()
    try:
        os.environ["NEBIUS_API_KEY"] = "<redacted>"
        direct = NemotronClient()
        assert direct.available
        try:
            direct.chat([{"role": "user", "content": "hi"}])
            raise AssertionError("direct real-transport call must be refused")
        except RuntimeError as exc:
            assert "opt-in" in str(exc), str(exc)
        mock_ok = NemotronClient(transport=lambda body: "mock-reply")
        assert mock_ok.chat([{"role": "user", "content": "hi"}]) == "mock-reply"
        live_mock = NemotronClient(transport=lambda body: "mock-reply",
                                   live=True)
        assert live_mock.chat([{"role": "user", "content": "hi"}]) == "mock-reply"
    finally:
        _restore_key(old)
        _restore_live(live_old)
        os.environ.pop("NEBIUS_API_KEY", None)
    print("PASS credit-safety: direct client construction gated at transport boundary")


if __name__ == "__main__":
    test_generator_falls_back_without_key()
    test_generator_stays_offline_with_key_unless_live()
    test_generator_uses_nemotron_with_live_opt_in()
    test_nemotron_generates_three_scenarios()
    test_generated_probes_judged_deterministically()
    test_generator_never_raises_on_garbage()
    test_narrative_labels_and_never_verdict()
    test_client_platform_facts()
    test_client_request_path()
    test_default_diagnostician_still_key_gated()
    test_full_world_makes_zero_http_calls_with_key_set()
    test_direct_client_construction_still_gated()
    print("\n12/12 nemotron rework tests passed")
