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


def test_tagged_rules_only():
    """Only rules tagged #critical / #alarm / #warning raise alarms; the filter is never applied blind."""
    p = Policy(tagged_only=True)
    row = lambda rule, **kw: {**nx_row(1, type_="analytics", **kw), "ruleId": rule}   # noqa: E731
    known = {"tagged", "plain", "muted"}
    levels = {"tagged": "critical", "muted": "ignore"}
    assert classify(row("tagged"), p, None, levels, known).level == "critical"
    plain = classify(row("plain"), p, None, levels, known)
    assert not plain.is_alarm and plain.source == "untagged"                      # a known rule without a tag: dropped
    assert not classify(row("muted"), p, None, levels, known).is_alarm             # #ignore still ignores
    assert classify(row("brand-new"), p, None, levels, known).level == "alarm"      # not read yet: by event type
    assert classify(row("plain"), p, None, levels, None).level == "alarm"           # rules unreadable: by event type
    portal = {**nx_row(1, type_="portalSiteOffline"), "ruleId": ""}
    assert classify(portal, p, None, levels, known).is_alarm                      # the portal's own alarm always
    assert classify(row("plain"), Policy(), None, levels, known).level == "alarm"  # setting off: as before
    assert Policy.from_settings({"alarm_policy": Policy(tagged_only=True).to_settings()}).tagged_only


async def test_tagged_only_setting_saves_and_keeps(client, session, admin):
    from tests.conftest import login
    tenant, user = admin
    await login(client, user.email)
    r = await client.get("/api/settings/alarm-levels")
    levels = {t["type"]: t["level"] for t in r.json()["types"]}
    assert r.json()["tagged_only"] is False
    r = await client.put("/api/settings/alarm-levels", json={"levels": levels, "force_ack_critical": True, "tagged_only": True})
    assert r.json()["tagged_only"] is True
    r = await client.put("/api/settings/alarm-levels", json={"levels": levels, "force_ack_critical": True})     # older page: kept
    assert r.json()["tagged_only"] is True
