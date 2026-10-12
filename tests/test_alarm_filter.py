from app.services.alarm_filter import Policy, classify
from app.services.nx_events import summarize
from tests.conftest import nx_row


def test_default_levels():
    assert classify(nx_row(1, type_="softTrigger")).level == "critical"
    assert classify(nx_row(1, type_="cameraInput")).level == "critical"
    assert classify(nx_row(1, type_="analytics")).level == "alarm"
    assert classify(nx_row(1, type_="generic")).level == "alarm"
    c = classify(nx_row(1, type_="deviceDisconnected"))
    assert (c.level, c.category) == ("warning", "system")


def test_forced_ack_rule_is_critical_even_for_ignored_type():
    assert classify(nx_row(1, type_="motion", ack=True)).level == "critical"
    assert not classify(nx_row(1, type_="motion", ack=True), Policy(force_ack_critical=False)).is_alarm


def test_noise_is_ignored_and_unknown_types_surface():
    for t in ("integrationDiagnostic", "serverStarted", "motion"):
        assert not classify(nx_row(1, type_=t)).is_alarm
    assert classify(nx_row(1, type_="someFutureEvent")).level == "warning"


def test_prolonged_event_stop_is_not_an_alarm():
    assert not classify(nx_row(1, type_="analyticsObject", state="stopped")).is_alarm


def test_tenant_policy_and_site_override():
    policy = Policy.from_settings({"alarm_policy": {"levels": {"motion": "warning", "generic": "ignore", "x": "bogus"}}})
    assert classify(nx_row(1, type_="motion"), policy).level == "warning"
    assert not classify(nx_row(1, type_="generic"), policy).is_alarm
    assert classify(nx_row(1, type_="generic"), policy, {"generic": "critical"}).level == "critical"


def test_soft_trigger_caption_names_the_user():
    row = nx_row(1, type_="softTrigger", caption="", triggerName="", userId="{u-1}")
    info = summarize(row, {}, {"u-1": "Mike S"})
    assert info["caption"] == "Soft trigger" and info["description"] == "Pressed by Mike S"
    assert summarize(nx_row(1, type_="deviceDisconnected", caption=""), {})["caption"] == "Camera disconnected"


def test_rule_tag_parsing():
    from app.services.alarm_filter import rule_tag
    assert rule_tag("Front gate panic #Critical") == "critical"
    assert rule_tag("#warning test rule") == "warning"
    assert rule_tag("no tag here") is None and rule_tag(None) is None
    assert rule_tag("issue##critical") is None          # must be a standalone tag
    assert rule_tag("see email#alarm.com") is None


def test_rule_tag_beats_everything():
    row = nx_row(1, type_="softTrigger")                 # default critical
    row["ruleId"] = "{r-9}"
    c = classify(row, rule_levels={"r-9": "warning"})
    assert (c.level, c.source) == ("warning", "rule_tag")
    ack = nx_row(1, type_="generic", ack=True); ack["ruleId"] = "r-9"
    assert not classify(ack, rule_levels={"r-9": "ignore"}).is_alarm
    assert classify(nx_row(1, type_="softTrigger")).source == "default"
