# Seed data (HE-020)

Doc 05 §4 names `seeds/*.csv`. They live in `services/seeds/` because the image only
copies `services/`. Load them with `docker compose exec api python -m seeds.load`.
The loader is idempotent: it upserts by `(lower(name), kind)`.

Columns: `name, kind, region, tier, rank_source, website`. `website` is the official
site, checked over HTTPS on 2026-11-14. It is left empty when the site could not be
confirmed, so nothing is guessed.

## Sources

| File | Rows | List used |
|---|---|---|
| `companies.csv` | 150 `global` | [Fortune Global 500 2026](https://fortune.com/ranking/global500/2026/), a hand-picked subset of tech-heavy companies and others likely to run hackathons or developer programmes. Indian companies are taken from the next list instead. |
| `companies.csv` | 150 `india` | [Fortune India 500 2025](https://www.fortuneindia.com/fortune-500/2025), a hand-picked subset: IT/product/internet companies, telecom, banks, auto, engineering/defence PSUs and large conglomerates. Indian subsidiaries of global parents already listed are skipped. |
| `institutions.csv` | 300 `india` | [NIRF 2025 Engineering](https://www.nirfindia.org/Rankings/2025/EngineeringRanking.html), the full published list: ranks 1–100 plus bands 101–150, 151–200 and 201–300. |
| `sources.csv` | 0 | Header only. Aggregator sources (Devpost, Unstop, MLH) arrive with their adapters (HE-024 onwards). The loader rejects rows here until then. |

`rank_source` records the list, the year and the position, for example
`Fortune Global 500 2026 #28` or `NIRF 2025 Engineering band 101-150`. Names follow the
list as published, with three exceptions:

- Whitespace and quotes are normalised.
- Obvious NIRF typos are fixed.
- NIRF names that occur more than once get a campus suffix, such as `Amity University, Noida` and the three `Jawaharlal Nehru Technological University` campuses.

## Tier rule

The tier comes from the position on the source list. For a band, the band's lower
bound counts as the position.

| Position on list | Tier | Crawl cadence (PRD §4.1) |
|---|---|---|
| 1–50 | 1 | daily |
| 51–150 | 2 | every 3 days |
| 151+ | 3 | weekly |

Re-running the loader overwrites tier, region, rank_source and website with the CSV
values. To change a tier for good, edit the CSV. `is_active` is only set on insert.
