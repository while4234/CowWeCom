import importlib.util
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "skills" / "plugin-12306-ticket" / "scripts" / "railway_12306.py"

spec = importlib.util.spec_from_file_location("railway_12306", SCRIPT_PATH)
railway_12306 = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = railway_12306
spec.loader.exec_module(railway_12306)


def make_ticket_row() -> str:
    parts = [""] * 36
    parts[1] = "预订"
    parts[2] = "550000G1230"
    parts[3] = "G123"
    parts[6] = "VNP"
    parts[7] = "AOH"
    parts[8] = "08:00"
    parts[9] = "12:30"
    parts[10] = "04:30"
    parts[11] = "Y"
    parts[13] = "20260602"
    parts[16] = "01"
    parts[17] = "03"
    parts[23] = ""
    parts[26] = "有"
    parts[28] = "--"
    parts[29] = "--"
    parts[30] = "有"
    parts[31] = "4"
    parts[32] = "2"
    parts[35] = "OM9"
    return "|".join(parts)


def test_parse_ticket_keeps_official_price_query_fields():
    ticket = railway_12306.parse_ticket(make_ticket_row(), {"VNP": "北京南", "AOH": "上海虹桥"})

    assert ticket.train_code == "G123"
    assert ticket.from_station == "北京南"
    assert ticket.to_station == "上海虹桥"
    assert ticket.from_station_no == "01"
    assert ticket.to_station_no == "03"
    assert ticket.seat_types == "OM9"


def test_include_prices_uses_official_price_endpoint_fields():
    client = railway_12306.RailwayClient()
    ticket = railway_12306.parse_ticket(make_ticket_row(), {"VNP": "北京南", "AOH": "上海虹桥"})
    calls = []

    def fake_get_json(path, params):
        calls.append((path, dict(params)))
        return {"data": {"O": "553.0", "M": "933.0", "A9": "1748.0"}}

    client._get_json = fake_get_json

    priced = client.enrich_ticket_prices([ticket], "2026-06-02")[0]

    assert calls == [
        (
            "/otn/leftTicket/queryTicketPrice",
            {
                "train_no": "550000G1230",
                "from_station_no": "01",
                "to_station_no": "03",
                "seat_types": "OM9",
                "train_date": "2026-06-02",
            },
        )
    ]
    assert priced.prices == {"O": "553.0", "M": "933.0", "A9": "1748.0"}
    assert ticket.prices == {}
