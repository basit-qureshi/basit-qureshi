from app.brokers.base import BrokerAdapter
from app.config import settings


def get_broker() -> BrokerAdapter:
    if settings.broker_mode == "mt5":
        from app.brokers.mt5_broker import MT5Broker

        if not (settings.mt5_login and settings.mt5_password and settings.mt5_server):
            raise RuntimeError("MT5_LOGIN, MT5_PASSWORD and MT5_SERVER must be set for BROKER_MODE=mt5")
        return MT5Broker(login=settings.mt5_login, password=settings.mt5_password, server=settings.mt5_server)

    from app.brokers.mock_broker import MockBroker

    return MockBroker()
