#!/usr/bin/env python3
"""
Builds Visor's F1 data from F1DB (https://github.com/f1db/f1db, CC BY 4.0).

Reads F1DB's source YAML (src/data from a checkout of its main branch, which gains each
session's results about half a day after it runs, ahead of the tagged releases) and writes
small static JSON files the app fetches one at a time:

  index.json                    seasons, each season's champion, build stamp, source commit
  seasons/<year>.json           the season's races (schedule and every classification) and
                                both championship tables
  drivers/<id>.json             one driver's record and every race they started
  circuits/<id>.json            every race at a circuit: winners and fastest laps
  constructors/<id>.json        a constructor's totals, seasons and championship places

The JSON keeps the shape the app already decodes (Ergast/Jolpica field names), so the app's
F1 mappers don't change. Before writing, it checks the data (see check()) and refuses to
publish a build that fails, so a half-entered weekend never reaches the app.

  python3 tools/f1db/build_f1db.py --src f1db/src/data --out build/f1 [--commit SHA]
"""
import argparse
import datetime as dt
import json
import os
import sys
from collections import defaultdict

import yaml

import re


class Loader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    """YAML 1.1 reads "18:30" as base-60 (1110) and "1:28.664" as 88.664: keep those as text."""


Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag not in ("tag:yaml.org,2002:int", "tag:yaml.org,2002:float")]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for tag, rx, first in (
    ("tag:yaml.org,2002:int", r"^[-+]?(?:0|[1-9][0-9]*)$", "-+0123456789"),
    ("tag:yaml.org,2002:float", r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?$", "-+0123456789."),
):
    Loader.add_implicit_resolver(tag, re.compile(tag and rx), list(first))


def load(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return yaml.load(f, Loader=Loader)


def load_dir(path):
    out = {}
    for name in sorted(os.listdir(path)):
        if name.endswith(".yml"):
            d = load(os.path.join(path, name))
            out[d["id"]] = d
    return out


def s(v):
    """Ergast-style strings: numbers as text, None stays None."""
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


class F1DB:
    def __init__(self, src):
        self.src = src
        self.countries = load_dir(os.path.join(src, "countries"))
        self.drivers = load_dir(os.path.join(src, "drivers"))
        self.constructors = load_dir(os.path.join(src, "constructors"))
        self.circuits = load_dir(os.path.join(src, "circuits"))
        self.grands_prix = load_dir(os.path.join(src, "grands-prix"))
        self.seasons = {}
        root = os.path.join(src, "seasons")
        for year in sorted(os.listdir(root), key=int):
            sdir = os.path.join(root, year)
            races = []
            rdir = os.path.join(sdir, "races")
            for rname in sorted(os.listdir(rdir)) if os.path.isdir(rdir) else []:
                d = os.path.join(rdir, rname)
                race = load(os.path.join(d, "race.yml"))
                if race is None:
                    continue
                files = {}
                for f in os.listdir(d):
                    if f.endswith(".yml") and f != "race.yml":
                        files[f[:-4]] = load(os.path.join(d, f)) or []
                race["_files"] = files
                races.append(race)
            races.sort(key=lambda r: r["round"])
            self.seasons[int(year)] = {
                "races": races,
                "driverStandings": load(os.path.join(sdir, "driver-standings.yml")) or [],
                "constructorStandings": load(os.path.join(sdir, "constructor-standings.yml")) or [],
            }

    # --- Shapes the app decodes (Jolpica field names) ---

    def country(self, cid):
        return self.countries.get(cid) or {}

    def driver(self, did):
        d = self.drivers.get(did) or {"id": did, "name": did}
        nat = self.country(d.get("nationalityCountryId"))
        return {
            "driverId": did,
            "permanentNumber": s(d.get("permanentNumber")),
            "code": d.get("abbreviation"),
            "givenName": d.get("firstName"),
            "familyName": d.get("lastName"),
            "dateOfBirth": s(d.get("dateOfBirth")),
            "nationality": nat.get("demonym"),
            "countryCode": nat.get("alpha2Code"),
        }

    def constructor(self, cid):
        c = self.constructors.get(cid) or {"name": cid}
        return {"constructorId": cid, "name": c.get("name"), "nationality": self.country(c.get("countryId")).get("demonym")}

    def circuit(self, cid):
        c = self.circuits.get(cid) or {"name": cid}
        country = self.country(c.get("countryId"))
        return {
            "circuitId": cid,
            "circuitName": c.get("fullName") or c.get("name"),
            "Location": {"locality": c.get("placeName"), "country": country.get("name"), "countryCode": country.get("alpha2Code")},
        }

    @staticmethod
    def when(date, time):
        if date is None:
            return None
        return {"date": s(date), "time": (s(time) + ":00Z") if time else None}

    def race_head(self, year, r):
        gp = self.grands_prix.get(r.get("grandPrixId")) or {}
        head = {
            "season": s(year),
            "round": s(r["round"]),
            "raceName": gp.get("fullName") or r.get("officialName"),
            "Circuit": self.circuit(r.get("circuitId")),
            "date": s(r.get("date")),
            "time": (s(r.get("time")) + ":00Z") if r.get("time") else None,
        }
        for key, field in (
            ("FirstPractice", "freePractice1"), ("SecondPractice", "freePractice2"), ("ThirdPractice", "freePractice3"),
            ("Qualifying", "qualifying"), ("Sprint", "sprintRace"), ("SprintQualifying", "sprintQualifying"),
        ):
            w = self.when(r.get(field + "Date"), r.get(field + "Time"))
            if w:
                head[key] = w
        return head

    @staticmethod
    def places(rows):
        """Each row's place: classified ones keep theirs, the rest follow in file order."""
        top = max([r.get("position") for r in rows if isinstance(r.get("position"), int)] or [0])
        out, n = [], top
        for r in rows:
            if isinstance(r.get("position"), int):
                out.append(r["position"])
            else:
                n += 1
                out.append(n)
        return out

    def result(self, row, i, fastest_rank, place=None):
        pos = row.get("position")
        classified = isinstance(pos, int)
        gap = row.get("gap")
        laps_down = gap if isinstance(gap, str) and "lap" in gap.lower() else None
        status = None
        if laps_down:
            n = laps_down.strip("+").split()[0]
            status = "+%s %s" % (n, "Lap" if n == "1" else "Laps")
        elif not classified:
            status = row.get("reasonRetired") or pos
        elif row.get("time") or gap:
            status = "Finished"
        time = row.get("time") if pos == 1 else (gap if gap and not laps_down else None)
        out = {
            "number": s(row.get("driverNumber")),
            "position": s(pos if classified else (place or i + 1)),
            "positionText": s(pos) if classified else {"DNF": "R", "DSQ": "D", "DNS": "W", "DNQ": "F", "DNPQ": "F", "NC": "N", "EX": "E", "DNP": "W"}.get(str(pos), "R"),
            "points": s(row.get("points")) or "0",
            "Driver": self.driver(row["driverId"]),
            "Constructor": self.constructor(row["constructorId"]),
            "grid": s(row.get("gridPosition")),
            "laps": s(row.get("laps")),
            "status": status,
        }
        if time:
            out["Time"] = {"time": s(time)}
        rank = fastest_rank.get(row["driverId"])
        if rank:
            out["FastestLap"] = {"rank": s(rank[0]), "Time": {"time": s(rank[1])}}
        return out

    def qualifying(self, row, i):
        pos = row.get("position")
        q1 = row.get("q1") or row.get("time")
        return {
            "number": s(row.get("driverNumber")),
            "position": s(pos if isinstance(pos, int) else i + 1),
            "Driver": self.driver(row["driverId"]),
            "Constructor": self.constructor(row["constructorId"]),
            "Q1": s(q1), "Q2": s(row.get("q2")), "Q3": s(row.get("q3")),
        }

    def practice(self, row, i):
        pos = row.get("position")
        return {
            "number": s(row.get("driverNumber")),
            "position": s(pos if isinstance(pos, int) else i + 1),
            "Driver": self.driver(row["driverId"]),
            "Constructor": self.constructor(row["constructorId"]),
            "Q1": s(row.get("time")),
            "laps": s(row.get("laps")),
        }

    def race(self, year, r):
        f = r["_files"]
        out = self.race_head(year, r)
        fl = {x["driverId"]: (x.get("position"), x.get("time")) for x in f.get("fastest-laps", []) if x.get("time")}
        if f.get("race-results"):
            pl = self.places(f["race-results"])
            out["Results"] = [self.result(x, i, fl, pl[i]) for i, x in enumerate(f["race-results"])]
        if f.get("sprint-race-results"):
            pl = self.places(f["sprint-race-results"])
            out["SprintResults"] = [self.result(x, i, {}, pl[i]) for i, x in enumerate(f["sprint-race-results"])]
        if f.get("qualifying-results"):
            out["QualifyingResults"] = [self.qualifying(x, i) for i, x in enumerate(f["qualifying-results"])]
        if f.get("sprint-qualifying-results"):
            out["SprintQualifyingResults"] = [self.qualifying(x, i) for i, x in enumerate(f["sprint-qualifying-results"])]
        for n in (1, 2, 3, 4):
            rows = f.get("free-practice-%d-results" % n)
            if rows:
                out["Practice%dResults" % n] = [self.practice(x, i) for i, x in enumerate(rows)]
        return out

    # --- Files ---

    def season_file(self, year):
        season = self.seasons[year]
        races = [self.race(year, r) for r in season["races"]]
        # Each driver's team: the one they last raced for this season.
        last_team, wins = {}, defaultdict(int)
        for r in races:
            for x in r.get("Results", []):
                last_team[x["Driver"]["driverId"]] = x["Constructor"]
                if x["position"] == "1" and x["positionText"] == "1":
                    wins[x["Driver"]["driverId"]] += 1
        standings = []
        for row in season["driverStandings"]:
            did = row["driverId"]
            standings.append({
                "position": s(row.get("position")), "positionText": s(row.get("position")),
                "points": s(row.get("points")), "wins": s(wins.get(did, 0)),
                "Driver": self.driver(did),
                "Constructors": [last_team[did]] if did in last_team else [],
            })
        cstand = [{
            "position": s(row.get("position")), "points": s(row.get("points")),
            "constructorId": row.get("constructorId"), "name": self.constructor(row.get("constructorId"))["name"],
        } for row in season["constructorStandings"]]
        return {"races": races, "driverStandings": standings, "constructorStandings": cstand}


def finished(race, today):
    return race.get("date") and dt.date.fromisoformat(race["date"]) < today


def check(db, files, today):
    """Refuse a build that would show wrong numbers. Returns a list of problems."""
    problems = []
    for year, f in files["seasons"].items():
        races = f["races"]
        rounds = [int(r["round"]) for r in races]
        if rounds != list(range(1, len(rounds) + 1)):
            problems.append("%d: rounds aren't 1..n: %s" % (year, rounds))
        for r in races:
            res = r.get("Results", [])
            positions = [x["position"] for x in res]
            if year >= 1966 and len(set(positions)) != len(positions):
                problems.append("%d R%s: duplicate positions" % (year, r["round"]))
            if year >= 2000 and finished(r, today - dt.timedelta(days=3)) and not res:
                problems.append("%d R%s: no race result three days after the race" % (year, r["round"]))
        # The table should add up: race + sprint points per driver, where the season's points are simple.
        if year >= 2010 and f["driverStandings"]:
            total = defaultdict(float)
            for r in races:
                for x in r.get("Results", []) + r.get("SprintResults", []):
                    total[x["Driver"]["driverId"]] += float(x["points"] or 0)
            for row in f["driverStandings"]:
                did = row["Driver"]["driverId"]
                if abs(total[did] - float(row["points"] or 0)) > 0.01 and year not in (2014,):
                    problems.append("%d: %s standings %s but results add to %s" % (year, did, row["points"], total[did]))
    return problems


def build(src, out, commit=None, today=None):
    today = today or dt.date.today()
    db = F1DB(src)
    seasons = {y: db.season_file(y) for y in db.seasons}

    drivers = defaultdict(list)
    circuits = defaultdict(lambda: {"winners": [], "fastest": []})
    teams = defaultdict(lambda: {"entries": 0, "first": None, "wins": 0, "second": 0, "third": 0, "poles": 0, "fastest": 0, "seasons": set(), "standings": {}})
    for year, f in seasons.items():
        for r in f["races"]:
            head = {k: r[k] for k in ("season", "round", "raceName", "Circuit", "date")}
            cid = r["Circuit"]["circuitId"]
            entered = set()
            for x in r.get("Results", []):
                did = x["Driver"]["driverId"]
                drivers[did].append(dict(head, Results=[x]))
                con = x["Constructor"]["constructorId"]
                t = teams[con]
                t["seasons"].add(year)
                if con not in entered:
                    entered.add(con)
                    t["entries"] += 1
                    t["first"] = t["first"] or {"season": head["season"], "raceName": head["raceName"]}
                if x["positionText"] == x["position"]:
                    if x["position"] == "1":
                        t["wins"] += 1
                    elif x["position"] == "2":
                        t["second"] += 1
                    elif x["position"] == "3":
                        t["third"] += 1
                if x.get("grid") == "1":
                    t["poles"] += 1
                if x.get("FastestLap", {}).get("rank") == "1":
                    t["fastest"] += 1
                    circuits[cid]["fastest"].append(dict(head, Results=[x]))
                if x["position"] == "1":
                    circuits[cid]["winners"].append(dict(head, Results=[x]))
        for row in f["constructorStandings"]:
            teams[row["constructorId"]]["standings"][str(year)] = {"position": row["position"], "points": row["points"]}

    files = {"seasons": seasons}
    problems = check(db, files, today)
    if problems:
        print("Refusing to publish:\n  " + "\n  ".join(problems[:80]), file=sys.stderr)
        return 1

    def write(path, data):
        p = os.path.join(out, path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))

    champions = {}
    for year, f in seasons.items():
        top = next((x for x in f["driverStandings"] if x["position"] == "1"), None)
        if top:
            champions[str(year)] = top["Driver"]["driverId"]
    write("index.json", {
        "seasons": sorted(seasons),
        "champions": champions,
        "built": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "F1DB (https://github.com/f1db/f1db), CC BY 4.0",
        "commit": commit,
    })
    for year, f in seasons.items():
        write("seasons/%d.json" % year, f)
    for did, races in drivers.items():
        write("drivers/%s.json" % did, {"driver": db.driver(did), "races": races})
    for cid, c in circuits.items():
        write("circuits/%s.json" % cid, c)
    for con, t in teams.items():
        write("constructors/%s.json" % con, dict(t, seasons=sorted(t["seasons"])))
    print("Wrote %d seasons, %d drivers, %d circuits, %d constructors to %s" % (len(seasons), len(drivers), len(circuits), len(teams), out))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="F1DB's src/data directory")
    ap.add_argument("--out", required=True)
    ap.add_argument("--commit")
    a = ap.parse_args()
    sys.exit(build(a.src, a.out, a.commit))
