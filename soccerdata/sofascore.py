"""Scraper for https://www.sofascore.com/."""

import itertools
import json
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ._common import BaseRequestsReader, make_game_id
from ._config import DATA_DIR, NOCACHE, NOSTORE, TEAMNAME_REPLACEMENTS, logger

SOFASCORE_DATADIR = DATA_DIR / "Sofascore"
SOFASCORE_API = "https://api.sofascore.com/api/v1/"

# Statistic items of ``event/{id}/statistics`` mapped to column names. Items are
# matched on their ``key`` first and on their English ``name`` as a fallback.
STATISTICS_KEYS = {
    "expectedGoals": "xg",
    "cornerKicks": "corners",
    "yellowCards": "yellow_cards",
    "redCards": "red_cards",
    "totalShotsOnGoal": "shots",
    "shotsOnGoal": "shots_on_target",
    "fouls": "fouls",
    "offsides": "offsides",
    "ballPossession": "possession",
    "bigChanceCreated": "big_chances",
}
STATISTICS_NAMES = {
    "expected goals": "xg",
    "corner kicks": "corners",
    "yellow cards": "yellow_cards",
    "red cards": "red_cards",
    "total shots": "shots",
    "shots on target": "shots_on_target",
    "fouls": "fouls",
    "offsides": "offsides",
    "ball possession": "possession",
    "big chances": "big_chances",
}
# Sofascore leaves these items out when both teams have zero of them.
STATISTICS_ZERO_IF_MISSING = ("yellow_cards", "red_cards", "offsides")
STATISTICS_COLUMNS = tuple(dict.fromkeys(STATISTICS_KEYS.values()))


def _is_tournament_id(value: Any) -> bool:
    """Return True if a league mapping value is a numeric unique-tournament ID."""
    return isinstance(value, int) or (isinstance(value, str) and value.isdigit())


def _to_float(value: Any) -> float:
    """Convert a Sofascore statistic value ("1.23", "55%", 4) to float (NaN if unknown)."""
    if value is None:
        return float("nan")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().rstrip("%").strip()
    if "/" in text:  # e.g. "12/20 (60%)" -> keep the first number
        text = text.split("/")[0]
    text = text.split(" ")[0]
    try:
        return float(text)
    except ValueError:
        return float("nan")


def parse_event_statistics(data: dict) -> dict[str, tuple[float, float]]:
    """Parse the JSON of ``event/{id}/statistics`` into {column: (home, away)}.

    Only the full-match period ("ALL") is used. Returns an empty dict when the
    match has no statistics.
    """
    periods = data.get("statistics") or []
    full = next((p for p in periods if p.get("period") == "ALL"), None)
    if full is None:
        return {}
    out: dict[str, tuple[float, float]] = {}
    for group in full.get("groups") or []:
        for item in group.get("statisticsItems") or []:
            col = STATISTICS_KEYS.get(item.get("key", "")) or STATISTICS_NAMES.get(
                str(item.get("name", "")).strip().lower()
            )
            if col is None or col in out:
                continue
            home = item["homeValue"] if "homeValue" in item else item.get("home")
            away = item["awayValue"] if "awayValue" in item else item.get("away")
            out[col] = (_to_float(home), _to_float(away))
    if out:
        for col in STATISTICS_ZERO_IF_MISSING:
            out.setdefault(col, (0.0, 0.0))
    return out


class Sofascore(BaseRequestsReader):
    """Provides pd.DataFrames from data available at http://www.sofascore.com.

    Data will be downloaded as necessary and cached locally in
    ``~/soccerdata/data/Sofascore``.

    Parameters
    ----------
    leagues : string or iterable, optional
        IDs of Leagues to include.
    seasons : string, int or list, optional
        Seasons to include. Supports multiple formats.
        Examples: '16-17'; 2016; '2016-17'; [14, 15, 16]
    proxy : 'tor' or or dict or list(dict) or callable, optional
        Use a proxy to hide your IP address. Valid options are:
            - "tor": Uses the Tor network. Tor should be running in
              the background on port 9050.
            - str: The address of the proxy server to use.
            - list(str): A list of proxies to choose from. A different proxy will
              be selected from this list after failed requests, allowing rotating
              proxies.
            - callable: A function that returns a valid proxy. This function will
              be called after failed requests, allowing rotating proxies.
    no_cache : bool
        If True, will not use cached data.
    no_store : bool
        If True, will not store downloaded data.
    data_dir : Path
        Path to directory where data will be cached.
    """

    def __init__(
        self,
        leagues: str | list[str] | None = None,
        seasons: str | int | Iterable[str | int] | None = None,
        proxy: str | list[str] | Callable[[], str] | None = None,
        no_cache: bool = NOCACHE,
        no_store: bool = NOSTORE,
        data_dir: Path = SOFASCORE_DATADIR,
    ):
        """Initialize the Sofascore reader."""
        super().__init__(
            leagues=leagues,
            proxy=proxy,
            no_cache=no_cache,
            no_store=no_store,
            data_dir=data_dir,
        )
        self.seasons = seasons
        if not self.no_store:
            (self.data_dir / "leagues").mkdir(parents=True, exist_ok=True)
            (self.data_dir / "seasons").mkdir(parents=True, exist_ok=True)
            (self.data_dir / "matches").mkdir(parents=True, exist_ok=True)

    def read_leagues(self) -> pd.DataFrame:
        """Retrieve the selected leagues from the datasource.

        Returns
        -------
        pd.DataFrame
        """
        # Leagues configured with a numeric unique-tournament ID (e.g. Ekstraklasa,
        # which is not among Sofascore's default tournaments) need no lookup.
        by_id = {k: int(v) for k, v in self._selected_leagues.items() if _is_tournament_id(v)}
        leagues = [{"league_id": v, "league": k} for k, v in by_id.items()]
        if len(by_id) < len(self._selected_leagues):
            url = SOFASCORE_API + "config/default-unique-tournaments/EN/football"
            filepath = self.data_dir / "leagues.json"
            reader = self.get(url, filepath)
            data = json.load(reader)
            df_named = pd.DataFrame(
                [{"league_id": k["id"], "league": k["name"]} for k in data["uniqueTournaments"]]
            ).pipe(self._translate_league)
            df_named = df_named[df_named["league"].isin(self.leagues)]
            df_named = df_named[~df_named["league"].isin(by_id)]
            leagues += df_named.to_dict("records")
        df = (
            pd.DataFrame(leagues, columns=["league_id", "league"])
            .assign(region=lambda x: x["league"].str.split("-").str[0])
            .set_index("league")
            .sort_index()
        )
        return df[df.index.isin(self.leagues)]

    def read_seasons(self) -> pd.DataFrame:
        """Retrieve the selected seasons for the selected leagues.

        Returns
        -------
        pd.DataFrame
        """
        filemask = "leagues/{}.json"
        seasons = []
        df_leagues = self.read_leagues()
        for lkey, league in df_leagues.iterrows():
            url = SOFASCORE_API + "unique-tournament/{}/seasons"
            filepath = self.data_dir / filemask.format(lkey)
            reader = self.get(url.format(league.league_id), filepath)
            data = json.load(reader)["seasons"]
            for season in data:
                seasons.append(
                    {
                        "league": lkey,
                        "season": self._season_code.parse(season["year"]),
                        "league_id": league.league_id,
                        "season_id": season["id"],
                    }
                )
        df = pd.DataFrame(seasons).set_index(["league", "season"]).sort_index()

        return df.loc[df.index.isin(list(itertools.product(self.leagues, self.seasons)))]

    def read_league_table(self, force_cache: bool = False) -> pd.DataFrame:
        """Retrieve the league table for the selected leagues.

        Parameters
        ----------
        force_cache : bool
             By default no cached data is used for the current season.
             If True, will force the use of cached data anyway.

        Returns
        -------
        pd.DataFrame
        """
        filemask = "seasons/{}_{}.html"
        urlmask = SOFASCORE_API + "unique-tournament/{}/season/{}/standings/total"

        idx = ["league", "season"]
        cols = ["team", "MP", "W", "D", "L", "GF", "GA", "GD", "Pts"]

        seasons = self.read_seasons()
        # collect league tables
        mult_tables = []
        for (lkey, skey), season in seasons.iterrows():
            filepath = self.data_dir / filemask.format(lkey, skey)
            url = urlmask.format(season.league_id, season.season_id)
            current_season = not self._is_complete(lkey, skey)
            reader = self.get(url, filepath, no_cache=current_season and not force_cache)
            season_data = json.load(reader)
            for row in season_data["standings"][0]["rows"]:
                mult_tables.append(
                    {
                        "league": lkey,
                        "season": skey,
                        "team": row["team"]["name"],
                        "MP": row["matches"],
                        "W": row["wins"],
                        "D": row["draws"],
                        "L": row["losses"],
                        "GF": row["scoresFor"],
                        "GA": row["scoresAgainst"],
                        "GD": row["scoresFor"] - row["scoresAgainst"],
                        "Pts": row["points"],
                    }
                )
            df = (
                pd.DataFrame(mult_tables)
                .set_index(idx)
                .replace({"team": TEAMNAME_REPLACEMENTS})
                .sort_index()[cols]
            )
        return df

    def read_schedule(self, force_cache: bool = False) -> pd.DataFrame:
        """Retrieve the game schedule for the selected leagues and seasons.

        Parameters
        ----------
        force_cache : bool
             By default no cached data is used for the current season.
             If True, will force the use of cached data anyway.

        Returns
        -------
        pd.DataFrame
        """
        urlmask1 = SOFASCORE_API + "unique-tournament/{}/season/{}/rounds"
        urlmask2 = SOFASCORE_API + "unique-tournament/{}/season/{}/events/round/{}"
        filemask1 = "matches/rounds_{}_{}.json"
        filemask2 = "matches/round_matches_{}_{}_{}.json"

        cols = [
            "round",
            "week",
            "date",
            "home_team",
            "away_team",
            "home_score",
            "away_score",
            "game_id",
        ]

        df_seasons = self.read_seasons()
        all_schedules = []
        for (lkey, skey), season in df_seasons.iterrows():
            filepath1 = self.data_dir / filemask1.format(lkey, skey)
            url1 = urlmask1.format(season["league_id"], season["season_id"])
            current_season = not self._is_complete(lkey, skey)
            reader1 = self.get(url1, filepath1, no_cache=current_season and not force_cache)
            season_data = json.load(reader1)
            rounds = season_data["rounds"]

            for round in rounds:
                filepath2 = self.data_dir / filemask2.format(lkey, skey, round["round"])
                url2 = urlmask2.format(season["league_id"], season["season_id"], round["round"])
                reader2 = self.get(url2, filepath2, no_cache=current_season and not force_cache)
                match_data = json.load(reader2)
                for _match in match_data["events"]:
                    if _match["status"]["code"] == 100 or _match["status"]["code"] == 0:
                        if _match["status"]["code"] == 100:
                            home_score = int(_match["homeScore"]["current"])
                            away_score = int(_match["awayScore"]["current"])
                        else:
                            home_score = float("nan")  # type: ignore
                            away_score = float("nan")  # type: ignore

                        all_schedules.append(
                            {
                                "league": lkey,
                                "season": skey,
                                "round": round["round"],
                                "week": _match["roundInfo"]["round"],
                                "date": datetime.fromtimestamp(
                                    _match["startTimestamp"], tz=timezone.utc
                                ),
                                "home_team": _match["homeTeam"]["name"],
                                "away_team": _match["awayTeam"]["name"],
                                "home_score": home_score,
                                "away_score": away_score,
                                "game_id": _match["id"],
                            }
                        )

        df = pd.DataFrame(all_schedules).replace(
            {
                "home_team": TEAMNAME_REPLACEMENTS,
                "away_team": TEAMNAME_REPLACEMENTS,
            }
        )
        df["game"] = df.apply(make_game_id, axis=1)
        return df.set_index(["league", "season", "game"]).sort_index()[cols]

    def read_team_match_stats(self, force_cache: bool = False) -> pd.DataFrame:
        """Retrieve match statistics (xG, corners, cards, shots, ...) per team and game.

        Statistics are downloaded once per finished game and cached. Games that
        finished less than two days ago are refreshed, since Sofascore may still
        revise their statistics.

        Parameters
        ----------
        force_cache : bool
             Passed on to :meth:`read_schedule`.

        Returns
        -------
        pd.DataFrame
            One row per team and game, indexed by league, season, game and team.
            Columns ending in ``_against`` hold the opponent's value. A statistic
            Sofascore does not provide for a game is NaN.
        """
        urlmask = SOFASCORE_API + "event/{}/statistics"
        filemask = "matches/statistics_{}.json"
        schedule = self.read_schedule(force_cache=force_cache)
        played = schedule[schedule["home_score"].notna()]
        now = datetime.now(tz=timezone.utc)

        rows = []
        for (lkey, skey, game), match in played.iterrows():
            game_id = int(match["game_id"])
            recent = (now - match["date"]) < timedelta(days=2)
            try:
                reader = self.get(
                    urlmask.format(game_id),
                    self.data_dir / filemask.format(game_id),
                    no_cache=recent,
                )
                stats = parse_event_statistics(json.load(reader))
            except (ConnectionError, ValueError, KeyError, TypeError):
                logger.warning("No statistics available for game %s (%s).", game_id, game)
                stats = {}
            for side, team, opponent, scored, conceded in (
                ("home", match["home_team"], match["away_team"], "home_score", "away_score"),
                ("away", match["away_team"], match["home_team"], "away_score", "home_score"),
            ):
                own, other = (0, 1) if side == "home" else (1, 0)
                row = {
                    "league": lkey,
                    "season": skey,
                    "game": game,
                    "team": team,
                    "date": match["date"],
                    "game_id": game_id,
                    "venue": side,
                    "opponent": opponent,
                    "goals": match[scored],
                    "goals_against": match[conceded],
                }
                for col in STATISTICS_COLUMNS:
                    values = stats.get(col, (float("nan"), float("nan")))
                    row[col] = values[own]
                    row[f"{col}_against"] = values[other]
                rows.append(row)

        cols = ["date", "game_id", "venue", "opponent", "goals", "goals_against"] + [
            c for col in STATISTICS_COLUMNS for c in (col, f"{col}_against")
        ]
        if not rows:
            return pd.DataFrame(columns=["league", "season", "game", "team", *cols]).set_index(
                ["league", "season", "game", "team"]
            )
        return (
            pd.DataFrame(rows).set_index(["league", "season", "game", "team"]).sort_index()[cols]
        )

    def read_event_details(
        self, game_ids: int | Iterable[int], force_cache: bool = False
    ) -> pd.DataFrame:
        """Retrieve details of games, including the referee and their career statistics.

        Parameters
        ----------
        game_ids : int or iterable of int
            Sofascore game IDs (the ``game_id`` column of :meth:`read_schedule`).
        force_cache : bool
             By default game details are always downloaded again, because the
             referee and venue of upcoming games can change. If True, cached
             data is used.

        Returns
        -------
        pd.DataFrame
            One row per game, indexed by game_id. Referee statistics are career
            totals over all competitions Sofascore covers.
        """
        if isinstance(game_ids, (int, str)):
            game_ids = [game_ids]
        rows = []
        for game_id in game_ids:
            url = SOFASCORE_API + f"event/{int(game_id)}"
            filepath = self.data_dir / f"matches/event_{int(game_id)}.json"
            event = json.load(self.get(url, filepath, no_cache=not force_cache))["event"]
            referee = event.get("referee") or {}
            rows.append(
                {
                    "game_id": int(game_id),
                    "date": datetime.fromtimestamp(event["startTimestamp"], tz=timezone.utc),
                    "home_team": event["homeTeam"]["name"],
                    "away_team": event["awayTeam"]["name"],
                    "status": (event.get("status") or {}).get("type"),
                    "venue": ((event.get("venue") or {}).get("name"))
                    or ((event.get("venue") or {}).get("stadium") or {}).get("name"),
                    "referee": referee.get("name"),
                    "referee_games": referee.get("games"),
                    "referee_yellow_cards": referee.get("yellowCards"),
                    "referee_red_cards": referee.get("redCards"),
                    "referee_yellow_red_cards": referee.get("yellowRedCards"),
                }
            )
        df = pd.DataFrame(rows).replace(
            {"home_team": TEAMNAME_REPLACEMENTS, "away_team": TEAMNAME_REPLACEMENTS}
        )
        return df.set_index("game_id")

    def read_missing_players(self, game_id: int) -> pd.DataFrame:
        """Retrieve players reported as missing or doubtful for a game.

        Uses the lineups of the game, which Sofascore publishes before kick-off
        (predicted) and updates once the official line-ups are known.

        Parameters
        ----------
        game_id : int
            Sofascore game ID.

        Returns
        -------
        pd.DataFrame
            Columns: side, player, position, type, reason, description,
            expected_return. ``reason`` is Sofascore's raw reason code. Empty when
            Sofascore has no line-up information for the game yet.
        """
        cols = ["side", "player", "position", "type", "reason", "description", "expected_return"]
        url = SOFASCORE_API + f"event/{int(game_id)}/lineups"
        filepath = self.data_dir / f"matches/lineups_{int(game_id)}.json"
        try:
            data = json.load(self.get(url, filepath, no_cache=True))
        except (ConnectionError, ValueError):
            logger.warning("No line-up information available for game %s.", game_id)
            return pd.DataFrame(columns=cols)
        rows = []
        for side in ("home", "away"):
            for missing in (data.get(side) or {}).get("missingPlayers") or []:
                player = missing.get("player") or {}
                end = missing.get("expectedEndDate")
                rows.append(
                    {
                        "side": side,
                        "player": player.get("name"),
                        "position": player.get("position"),
                        "type": missing.get("type"),
                        "reason": missing.get("reason"),
                        "description": missing.get("description"),
                        "expected_return": (
                            datetime.fromtimestamp(end, tz=timezone.utc).date()
                            if isinstance(end, (int, float))
                            else end
                        ),
                    }
                )
        return pd.DataFrame(rows, columns=cols)
