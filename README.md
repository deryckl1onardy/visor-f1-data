# visor-f1-data

Formula 1 data for the Visor app, built from [F1DB](https://github.com/f1db/f1db) by
`build_f1db.py` and served as static JSON from `f1/`:

- `f1/index.json` — seasons, champions, build time, and the F1DB commit built from
- `f1/seasons/<year>.json` — a season's races, every classification (practice, qualifying,
  sprint, race) and both championship tables
- `f1/drivers/<id>.json`, `f1/circuits/<id>.json`, `f1/constructors/<id>.json`

The workflow in `.github/workflows/f1-data.yml` checks F1DB's `main` branch every 15 minutes and
rebuilds when it has moved. A build that fails its checks (rounds in order, no duplicate places,
a result for every finished race, championship points that add up) is not published.

## Licence and credit

The data is F1DB's, licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
(see `LICENSE`): <https://github.com/f1db/f1db>. It is reshaped here (field names, one file per
season, driver, circuit and constructor, places and gaps worked out); nothing is added. No
endorsement by F1DB or its contributors is implied. Formula 1 and related marks belong to their
owners; this is not an official data set.
