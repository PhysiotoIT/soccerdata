"""Offline unittests for the Sofascore match statistics readers.

The responses below mimic the structure of Sofascore's API, so these tests run
without network access.
"""

import io
import json
import math
from collections.abc import Iterator

import pandas as pd
import pytest

import soccerdata as sd
from soccerdata.sofascore import SOFASCORE_API, parse_event_statistics

T0 = 1753470000  # 2025-07-25 19:00 UTC


def _stat(key: str, name: str, home: float, away: float) -> dict:
    return {"key": key, "name": name, "homeValue": home, "awayValue": away}


STATS_GAME_1 = {
    "statistics": [
        {
            "period": "ALL",
            "groups": [
                {
                    "groupName": "Match overview",
                    "statisticsItems": [
                        _stat("ballPossession", "Ball possession", 58, 42),
                        _stat("expectedGoals", "Expected goals", 1.84, 0.62),
                        _stat("totalShotsOnGoal", "Total shots", 15, 7),
                        _stat("cornerKicks", "Corner kicks", 7, 3),
                        _stat("fouls", "Fouls", 11, 14),
                        _stat("yellowCards", "Yellow cards", 1, 3),
                    ],
                },
                {
                    "groupName": "Shots",
                    "statisticsItems": [
                        _stat("totalShotsOnGoal", "Total shots", 99, 99),  # duplicate: ignored
                        _stat("shotsOnGoal", "Shots on target", 6, 2),
                    ],
                },
            ],
        },
        {
            "period": "1ST",
            "groups": [{"statisticsItems": [_stat("expectedGoals", "Expected goals", 0.9, 0.2)]}],
        },
    ]
}

# No keys, only names and string values (older format).
STATS_GAME_2 = {
    "statistics": [
        {
            "period": "ALL",
            "groups": [
                {
                    "statisticsItems": [
                        {"name": "Expected goals", "home": "0.95", "away": "1.40"},
                        {"name": "Corner kicks", "home": "4", "away": "6"},
                        {"name": "Ball possession", "home": "47%", "away": "53%"},
                        {"name": "Red cards", "home": "0", "away": "1"},
                    ]
                }
            ],
        }
    ]
}


def _event(eid: int, home: str, away: str, ts: int, score: tuple | None, rnd: int) -> dict:
    ev = {
        "id": eid,
        "homeTeam": {"name": home},
        "awayTeam": {"name": away},
        "startTimestamp": ts,
        "roundInfo": {"round": rnd},
        "status": {"code": 100 if score else 0, "type": "finished" if score else "notstarted"},
        "homeScore": {"current": score[0]} if score else {},
        "awayScore": {"current": score[1]} if score else {},
    }
    return ev


RESPONSES = {
    "unique-tournament/202/seasons": {
        "seasons": [
            {"name": "Ekstraklasa 25/26", "year": "25/26", "id": 76477},
            {"name": "Ekstraklasa 24/25", "year": "24/25", "id": 61236},
        ]
    },
    "unique-tournament/202/season/76477/rounds": {"rounds": [{"round": 1}, {"round": 2}]},
    "unique-tournament/202/season/76477/events/round/1": {
        "events": [_event(1, "Lech Poznań", "Legia Warszawa", T0, (2, 0), 1)]
    },
    "unique-tournament/202/season/76477/events/round/2": {
        "events": [
            _event(2, "Legia Warszawa", "Raków Częstochowa", T0 + 7 * 86400, (1, 1), 2),
            _event(3, "Raków Częstochowa", "Lech Poznań", T0 + 8 * 86400, (0, 3), 2),
            _event(4, "Lech Poznań", "Raków Częstochowa", T0 + 400 * 86400, None, 2),
        ]
    },
    "event/1/statistics": STATS_GAME_1,
    "event/2/statistics": STATS_GAME_2,
    # event 3 has no statistics -> the download fails
    "event/4": {
        "event": {
            "id": 4,
            "startTimestamp": T0 + 400 * 86400,
            "homeTeam": {"name": "Lech Poznań"},
            "awayTeam": {"name": "Raków Częstochowa"},
            "status": {"type": "notstarted"},
            "venue": {"name": "Enea Stadion"},
            "referee": {
                "name": "Szymon Marciniak",
                "games": 400,
                "yellowCards": 1700,
                "redCards": 60,
                "yellowRedCards": 40,
            },
        }
    },
    "event/4/lineups": {
        "confirmed": False,
        "home": {
            "players": [],
            "missingPlayers": [
                {
                    "player": {"name": "Mikael Ishak", "position": "F"},
                    "type": "missing",
                    "reason": 1,
                    "description": "Hamstring injury",
                    "expectedEndDate": T0 + 410 * 86400,
                }
            ],
        },
        "away": {"players": []},
    },
}


def _drop_league_cache() -> None:
    for cls in sd.Sofascore.__mro__:
        if "_all_leagues_dict" in vars(cls):
            delattr(cls, "_all_leagues_dict")


@pytest.fixture(autouse=True)
def _fresh_league_cache() -> Iterator[None]:
    """Drop the cached league mapping, which other tests may have filled with patched leagues."""
    _drop_league_cache()
    yield
    _drop_league_cache()


@pytest.fixture
def ekstraklasa(monkeypatch: pytest.MonkeyPatch, tmp_path) -> sd.Sofascore:
    """Ekstraklasa reader whose downloads are served from RESPONSES."""
    monkeypatch.setattr(sd.Sofascore, "_init_session", lambda self, headers=None: None)
    reader = sd.Sofascore("POL-Ekstraklasa", "25-26", no_store=True, data_dir=tmp_path)
    requested = []

    def fake_get(url, filepath=None, max_age=None, no_cache=False, var=None):
        assert url.startswith(SOFASCORE_API)
        path = url[len(SOFASCORE_API) :]
        requested.append(path)
        if path not in RESPONSES:
            raise ConnectionError(f"Could not download {url}.")
        return io.BytesIO(json.dumps(RESPONSES[path]).encode("utf-8"))

    monkeypatch.setattr(reader, "get", fake_get)
    reader.requested = requested  # type: ignore[attr-defined]
    return reader


def test_ekstraklasa_is_available() -> None:
    assert "POL-Ekstraklasa" in sd.Sofascore.available_leagues()


def test_read_leagues_by_tournament_id(ekstraklasa: sd.Sofascore) -> None:
    leagues = ekstraklasa.read_leagues()
    assert leagues.loc["POL-Ekstraklasa", "league_id"] == 202
    assert leagues.loc["POL-Ekstraklasa", "region"] == "POL"
    # a numeric ID needs no lookup in the list of default tournaments
    assert ekstraklasa.requested == []  # type: ignore[attr-defined]


def test_read_seasons(ekstraklasa: sd.Sofascore) -> None:
    seasons = ekstraklasa.read_seasons()
    assert list(seasons.index) == [("POL-Ekstraklasa", "2526")]
    assert seasons.iloc[0]["season_id"] == 76477


def test_parse_event_statistics_keys() -> None:
    stats = parse_event_statistics(STATS_GAME_1)
    assert stats["xg"] == (1.84, 0.62)
    assert stats["corners"] == (7, 3)
    assert stats["shots"] == (15, 7)  # first occurrence wins
    assert stats["shots_on_target"] == (6, 2)
    assert stats["yellow_cards"] == (1, 3)
    assert stats["red_cards"] == (0, 0)  # missing card items mean zero
    assert "big_chances" not in stats


def test_parse_event_statistics_names_and_strings() -> None:
    stats = parse_event_statistics(STATS_GAME_2)
    assert stats["xg"] == (0.95, 1.40)
    assert stats["possession"] == (47, 53)
    assert stats["red_cards"] == (0, 1)
    assert stats["yellow_cards"] == (0, 0)


def test_parse_event_statistics_empty() -> None:
    assert parse_event_statistics({}) == {}
    assert parse_event_statistics({"statistics": [{"period": "1ST", "groups": []}]}) == {}


def test_read_team_match_stats(ekstraklasa: sd.Sofascore) -> None:
    stats = ekstraklasa.read_team_match_stats()
    assert isinstance(stats, pd.DataFrame)
    assert len(stats) == 6  # 3 played games x 2 teams; the upcoming game is skipped
    lech_home = stats.xs("Lech Poznań", level="team").query("venue == 'home'").iloc[0]
    assert lech_home["opponent"] == "Legia Warszawa"
    assert (lech_home["goals"], lech_home["goals_against"]) == (2, 0)
    assert (lech_home["xg"], lech_home["xg_against"]) == (1.84, 0.62)
    assert (lech_home["corners"], lech_home["corners_against"]) == (7, 3)
    legia_away = stats.xs("Legia Warszawa", level="team").query("venue == 'away'").iloc[0]
    assert (legia_away["xg"], legia_away["xg_against"]) == (0.62, 1.84)
    assert legia_away["yellow_cards"] == 3
    # game without statistics: goals are known, statistics are NaN
    rakow_home = stats.xs("Raków Częstochowa", level="team").query("venue == 'home'").iloc[0]
    assert rakow_home["goals_against"] == 3
    assert math.isnan(rakow_home["xg"])


def test_read_event_details(ekstraklasa: sd.Sofascore) -> None:
    details = ekstraklasa.read_event_details(4)
    row = details.loc[4]
    assert row["referee"] == "Szymon Marciniak"
    assert row["referee_games"] == 400
    assert row["venue"] == "Enea Stadion"


def test_read_missing_players(ekstraklasa: sd.Sofascore) -> None:
    missing = ekstraklasa.read_missing_players(4)
    assert list(missing["player"]) == ["Mikael Ishak"]
    assert missing.iloc[0]["side"] == "home"
    assert missing.iloc[0]["description"] == "Hamstring injury"
    assert ekstraklasa.read_missing_players(3).empty
