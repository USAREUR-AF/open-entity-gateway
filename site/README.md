# site

The landing page published to GitHub Pages by `.github/workflows/pages.yml`.
One static page: no framework, no external requests, no web fonts.

## Files

| File | What it is |
|---|---|
| `index.html` | the page |
| `assets/site.css` | its styles, light and dark (`prefers-color-scheme`) |
| `assets/hero.js` | the copy buttons, and the hero: lays out and marks `assets/from-tree/hero.json` |
| `assets/icon.svg` | the page icon |
| `assets/from-tree/` | git-ignored; written by `build.py`: `hero.json` and the two figures from `docs/img/` |
| `build.py` | gathers `assets/from-tree/` and checks the page against the tree |
| `fitcheck.py` | run by `build.py`: loads the page in headless Chrome at 1440 and 390 px and fails when a command box is cut off |

## Command and output boxes

Every `<pre>` on the page is marked `data-box="command"` or `data-box="output"`. A command box shows the whole command at any width: it wraps at spaces, each line of it a `<span class="ln">` with a hanging indent, and Copy copies the exact text. An output box fits at 1440 px with no sideways scroll. `fitcheck.py` checks both; it needs Chrome (`$CHROME`, or `google-chrome` / `chromium` on the path) and fails without one under CI.

## Preview

With the core and `vendors/lattice` installed (see the README's "Run a bridge"):

```sh
python site/build.py
python -m http.server 8000 --directory site    # then open http://127.0.0.1:8000/
```

## The hero

`build.py` runs, for each of the `lattice`, `cot` and `asterix-cat062` adapters:

```sh
open-entity-gateway print --mapping mappings --adapter <adapter> examples/records/weather-station.records.jsonl
```

and stores what it printed in `hero.json`, beside the Position record it came
from and `mappings/weather-station.position.yml`. The page shows those bytes:
the Lattice entity as one field path per line, the CoT event broken at its
elements, and the CAT062 record split into its data items by the field
specification (`build.py` fails if the items do not add up to the record's
length octets). The record's values are marked where they appear. The Lattice
`expiryTime` is the build time plus `stale_minutes`. Without JavaScript the
page shows the CoT event, which `build.py` checks is the one the gateway prints.

## Where each number comes from

Every number is in an element with `data-measure="<key>"`; `build.py` measures
it and fails if the page says something else.

| Key | On the page | Measured as |
|---|---|---|
| `bridges` | vendor bridges | directories under `vendors/` with a `pyproject.toml` |
| `inputs` | inputs | modules in `src/open_entity_gateway/inputs/` other than `__init__.py` |
| `cot-vectors` | CoT events identical to first-bite's | lines in `tests/vectors/cotify.jsonl`; `tests/test_cot.py` checks each byte for byte |
| `sdk` | vendor SDKs installed by core CI | lines in `requirements/*.txt` naming `anduril` or `picogrid`; the CI job "no vendor SDK is installed" checks the same at run time |
