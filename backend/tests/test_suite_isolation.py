"""The test suite must never reach a real broker.

This is a safety property, not a style preference. `get_broker()` reads
BROKER_MODE from the operator's .env, so on a machine set up for live trading
the tests would otherwise construct an MT5 adapter and reach for the running
terminal. Asserting it here means the guarantee breaks loudly, in CI, rather
than quietly on someone's trading machine.
"""


def test_the_suite_never_builds_a_real_broker():
    from app.brokers import get_broker
    from app.brokers.mock_broker import MockBroker
    from app.config import settings

    assert settings.broker_mode == "mock", (
        f"the test suite is configured for BROKER_MODE={settings.broker_mode!r}; "
        "conftest must pin it to 'mock' before app.config is imported"
    )
    assert isinstance(get_broker(), MockBroker)


def test_settings_do_not_depend_on_the_operators_env():
    """A result that changes with whose .env is next to the tests is not a
    result. The symbol and account type are pinned too."""
    from app.config import settings

    assert settings.symbol == "XAUUSD"
    assert settings.account_type == "demo"


def test_the_api_manager_uses_the_mock_broker(client):
    """The concrete case that failed: BotManager() built an MT5 adapter from a
    live .env, so every /api test depended on a running terminal."""
    from app.brokers.mock_broker import MockBroker

    _, manager = client
    assert isinstance(manager.broker, MockBroker)
    assert isinstance(manager.engine.broker, MockBroker)
    # and the candle the daily accounting is derived from is actually readable
    assert manager.engine._current_candle_time() is not None
