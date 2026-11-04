"""Offline unittests for the Sofascore match statistics readers.

The responses below mimic the structure of Sofascore's API, so these tests run
without network access.
"""

import io
import json
import math
import os
import time
from collections.abc import Iterator
from pathlib import Path

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
    "event/5/lineups": {
        "away": {
            "missingPlayers": [
                {
                    "player": {"name": "Josue", "position": "M"},
                    "type": "missing",
                    "reason": 3,
                    "expectedEndDateTimestamp": T0 + 420 * 86400,
                }
            ]
        }
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
    missing = ekstraklasa.read_missing_players(5)
    assert missing.iloc[0]["side"] == "away"
    assert missing.iloc[0]["reason"] == 3
    assert str(missing.iloc[0]["expected_return"]) == "2026-09-18"


# ---------- caching on disk (current season) ----------

NOW = int(time.time())
DAY = 86400
ROUND = "unique-tournament/202/season/90001/events/round/{}"


def _disk_responses() -> dict:
    old = [
        _event(11, "Lech Poznań", "Legia Warszawa", NOW - 10 * DAY, (2, 0), 1),
        _event(14, "Raków Częstochowa", "Pogoń Szczecin", NOW - 9 * DAY, (1, 0), 1),
        _event(15, "Pogoń Szczecin", "Lech Poznań", NOW - 8 * DAY, (0, 0), 1),
        _event(16, "Legia Warszawa", "Raków Częstochowa", NOW - 7 * DAY, (3, 1), 1),
    ]
    recent = [
        _event(12, "Legia Warszawa", "Lech Poznań", NOW - DAY, (1, 1), 2),
        _event(13, "Raków Częstochowa", "Lech Poznań", NOW + 5 * DAY, None, 2),
    ]
    responses = {
        "unique-tournament/202/seasons": {
            "seasons": [{"year": "26/27", "id": 90001}, {"year": "25/26", "id": 76477}]
        },
        "unique-tournament/202/season/90001/rounds": {"rounds": [{"round": 1}, {"round": 2}]},
        ROUND.format(1): {"events": old},
        ROUND.format(2): {"events": recent},
    }
    for gid in (11, 12, 14, 15, 16):
        responses[f"event/{gid}/statistics"] = STATS_GAME_1
    return responses


@pytest.fixture
def ekstraklasa_disk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> sd.Sofascore:
    """Current-season reader with a real cache on disk; downloads come from a dict."""
    monkeypatch.setattr(sd.Sofascore, "_init_session", lambda self, headers=None: None)
    reader = sd.Sofascore("POL-Ekstraklasa", "26-27", data_dir=tmp_path)
    reader.responses = _disk_responses()  # type: ignore[attr-defined]
    reader.requested = []  # type: ignore[attr-defined]

    def fake_download(url, filepath=None, var=None):
        path = url[len(SOFASCORE_API) :]
        reader.requested.append(path)  # type: ignore[attr-defined]
        if path not in reader.responses:  # type: ignore[attr-defined]
            raise ConnectionError(f"Could not download {url}.")
        payload = json.dumps(reader.responses[path]).encode("utf-8")  # type: ignore[attr-defined]
        if filepath is not None:
            filepath.write_bytes(payload)
        return io.BytesIO(payload)

    monkeypatch.setattr(reader, "_download_and_save", fake_download)
    return reader


def test_stale_season_list_is_refreshed(ekstraklasa_disk: sd.Sofascore) -> None:
    cached = ekstraklasa_disk.data_dir / "leagues" / "POL-Ekstraklasa.json"
    cached.write_text(json.dumps({"seasons": [{"year": "25/26", "id": 76477}]}))
    seasons = ekstraklasa_disk.read_seasons()
    assert list(seasons.index) == [("POL-Ekstraklasa", "2627")]
    assert ekstraklasa_disk.requested == ["unique-tournament/202/seasons"]  # type: ignore[attr-defined]
    ekstraklasa_disk.requested.clear()  # type: ignore[attr-defined]
    ekstraklasa_disk.read_seasons()
    assert ekstraklasa_disk.requested == []  # type: ignore[attr-defined]


def test_finished_rounds_are_cached(ekstraklasa_disk: sd.Sofascore) -> None:
    ekstraklasa_disk.read_schedule()
    ekstraklasa_disk.requested.clear()  # type: ignore[attr-defined]
    schedule = ekstraklasa_disk.read_schedule()
    assert len(schedule) == 6
    requested = ekstraklasa_disk.requested  # type: ignore[attr-defined]
    assert ROUND.format(1) not in requested  # all games ended more than two days ago
    assert ROUND.format(2) in requested  # a recent and an upcoming game: may still change
    assert "unique-tournament/202/season/90001/rounds" in requested


def test_statistics_refreshed_until_settled(ekstraklasa_disk: sd.Sofascore) -> None:
    schedule = ekstraklasa_disk.read_schedule()
    requested = ekstraklasa_disk.requested  # type: ignore[attr-defined]
    requested.clear()
    stats = ekstraklasa_disk.read_team_match_stats(schedule=schedule)
    assert len(stats) == 10
    assert sorted(requested) == [f"event/{g}/statistics" for g in (11, 12, 14, 15, 16)]
    # settled games come from the cache; the game from yesterday is downloaded again,
    # and passing the schedule avoids downloading it a second time
    requested.clear()
    ekstraklasa_disk.read_team_match_stats(schedule=schedule)
    assert requested == ["event/12/statistics"]
    # statistics saved shortly after kick-off are downloaded again once they settled
    saved = NOW - 10 * DAY + 3600
    os.utime(ekstraklasa_disk.data_dir / "matches" / "statistics_11.json", (saved, saved))
    requested.clear()
    ekstraklasa_disk.read_team_match_stats(schedule=schedule)
    assert sorted(requested) == ["event/11/statistics", "event/12/statistics"]


def test_statistics_stop_after_failures(ekstraklasa_disk: sd.Sofascore) -> None:
    schedule = ekstraklasa_disk.read_schedule()
    ekstraklasa_disk.read_team_match_stats(schedule=schedule)  # everything cached
    for gid in (12, 14, 15, 16):
        ekstraklasa_disk.responses.pop(f"event/{gid}/statistics")  # type: ignore[attr-defined]
    for gid in (14, 15, 16):  # pretend these were saved right after kick-off
        (ekstraklasa_disk.data_dir / "matches" / f"statistics_{gid}.json").unlink()
    requested = ekstraklasa_disk.requested  # type: ignore[attr-defined]
    requested.clear()
    stats = ekstraklasa_disk.read_team_match_stats(schedule=schedule)
    # three failures in a row: game 12 is not requested, its cached statistics are used
    assert requested == [f"event/{g}/statistics" for g in (14, 15, 16)]
    by_game = stats.reset_index().set_index("game_id")
    assert math.isnan(by_game.loc[14, "xg"].iloc[0])
    assert sorted(by_game.loc[12, "xg"]) == [0.62, 1.84]
