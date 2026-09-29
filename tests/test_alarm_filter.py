from app.services.alarm_filter import classify
from tests.conftest import nx_row


def test_security_event_is_high_priority():
    c = classify(nx_row(1, type_="softTrigger"))
    assert (c.is_alarm, c.category, c.priority) == (True, "security", 2)


def test_forced_ack_rule_is_critical_even_for_unlisted_type():
    c = classify(nx_row(1, type_="motion", ack=True))
    assert (c.is_alarm, c.priority) == (True, 1)


def test_system_health_event():
    c = classify(nx_row(1, type_="deviceDisconnected"))
    assert (c.is_alarm, c.category, c.priority) == (True, "system", 3)


def test_noise_is_dropped():
    assert not classify(nx_row(1, type_="integrationDiagnostic")).is_alarm
    assert not classify(nx_row(1, type_="serverStarted")).is_alarm
    assert not classify(nx_row(1, type_="motion")).is_alarm


def test_prolonged_event_stop_is_not_an_alarm():
    assert not classify(nx_row(1, type_="analyticsObject", state="stopped")).is_alarm


def test_site_override_include_and_exclude():
    override = {"include": ["motion"], "exclude": ["generic"]}
    assert classify(nx_row(1, type_="motion"), override).is_alarm
    assert not classify(nx_row(1, type_="generic"), override).is_alarm
