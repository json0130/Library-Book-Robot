# The book data

The robot's knowledge of the library's books lives in four plain files in `data/`, plus the floor plan and the covers
described at the end. Edit them in a spreadsheet or a text editor; no code changes are needed. All four ship with **sample data** (12 books, 7 shelves) that you replace with the real library.

| File | What it holds | Edit it when |
|---|---|---|
| `catalog.csv` | one row per book: who wrote it, what it is about, where it lives | you add or change a book |
| `topics.yaml` | the vocabulary of topics: synonyms and "broader" topics | a subject isn't recognised, or you want books under one heading to relate |
| `shelves.csv` | one row per shelf: section, floor, quiet or common zone, and the robot's map position | you add a shelf, or survey the real map positions |
| `curated_links.csv` | hand-made links between books | a librarian knows two books belong together |
| `floorplan.yaml` | floors, sections as rectangles, landmarks, the robot's station (for the directions on the screen) | the building's layout changes, or you survey the real map |
| `covers/<isbn>.jpg` | book covers for the screen (downloaded, gitignored) | you add books: `python tools/import_books.py --covers-only` |

The robot reads them once at startup and builds a small graph from them (`robot/services/book_graph.py`). Paths are set under `books:` in `config/robot.yaml`. After editing, restart the robot, or type `reload` in the simulator, to pick up the changes.

**Check your work** after every edit:

```bash
python tools/catalog_check.py        # errors (exit code 1) and warnings, plus statistics
python tools/graph_view.py           # then open data/graph.html in a browser to look at the graph
```

## catalog.csv

Lines starting with `#` and blank lines are ignored. The first other line is the header. The columns below must all be in the header (the cell can be empty where it says so); the optional ones may be left out of the file altogether. Column order doesn't matter. Put a cell containing a comma in double quotes.

| Column | Required | What goes in it |
|---|---|---|
| `isbn13` | yes | The book's ISBN-13, or an ISBN-10; hyphens and spaces are fine. It is checked (the checksum must be right) and stored as ISBN-13. One row per ISBN: a duplicate is an error. This is what the barcode scanner looks up. |
| `title` | yes | The title as people say it. `where is dune` is matched against it loosely (case, punctuation and a leading "The" don't matter). |
| `author` | | The author's name. Separate several authors with a semicolon: `Carl Sagan; Ann Druyan`. Books sharing an author are related. |
| `genre` | | One broad genre, such as `science fiction`. It is treated like another subject. |
| `subjects` | | What the book is about, separated by semicolons: `space;cosmos;popular science`. They are folded into topics through `topics.yaml` (see below). An older file with a `tags` column instead still loads. Give each book at least two topics (genre counts) or it is hard to match. |
| `summary` | | One or two sentences of what the book is about, in your own words. The robot reads it aloud when someone asks "what is it about?" and compares summaries when ranking recommendations. Keep it on one line. |
| `shelf` | | The shelf's id from `shelves.csv`, e.g. `3-SCI-A`. This is how the robot knows the section, floor, zone and map position. Leave it empty if you don't know yet. |
| `slot` | | The storage-ring slot, `0` to `7`, that a returned copy prefers. Empty if unknown. Several books may share a preferred slot: the state machine gives the book this slot if it is free and otherwise the lowest free one (`robot/core/storage.py`), and says which slot it used. |
| `call_number` | optional | The call number, Dewey (`520.1 SAG`) or Library of Congress (`QB981 .S24`). Shelf codes such as `F HER` or `FIC-FIT` work too. See below. |
| `section` | optional | The section name, e.g. `Science`. Only needed if the book has no shelf; otherwise the shelf's section is used (and a mismatch is a warning). |
| `floor` | optional | The floor number, e.g. `3`. Same rule as `section`. |
| `series` | optional | The series name, exactly the same for every book in it: `Dune Chronicles`. |
| `series_index` | optional | The book's place in its series: `1`, `2`, ... The book with the next number is recommended first. |
| `year` | optional | The publication year. Informational (the importer fills it in); it doesn't affect recommendations. |

### Call numbers

The robot understands three kinds of call number and uses them to tell how close two books sit on the shelves:

| Kind | Example | How it is cut up (broadest first) |
|---|---|---|
| Dewey Decimal | `520.1 SAG` | `500` > `520` > `520.1` (hundreds, tens, units, then each decimal digit) |
| Library of Congress | `QB981 .S24` | `Q` > `QB` > `QB981` (class letter, subclass, number; the cutter `.S24` is ignored) |
| Shelf code (fiction) | `F HER`, `FIC-FIT` | just the prefix `F` / `FIC` |

Sharing a class earns a little (the same Dewey hundred, the same LC class letter, then the same Dewey ten or LC subclass). Inside one numbering scope (a Dewey hundred, an LC subclass) the rest comes from how far apart the numbers are, fading quickly: `QB981` and `QB982` are next to each other and count as close by, while `QB44.2` and `QB981` are in the same subclass but nowhere near each other and do not. `523.8` is closer to `523.9` than to `530`, and `520` is closer to `523` than to `599`. Two shelf codes only share a prefix, so they count as only loosely close. Numbers of different kinds, empty ones, and anything unreadable score zero: they are never an error.

When the robot explains a recommendation it never quotes a call number. It names the area ("both shelved in astronomy") from small built-in tables of Dewey hundreds and tens and LC classes and subclasses in `robot/services/call_numbers.py`; a number the tables don't cover is described as "the same part of the library". Add a name there if you want your library's areas spoken better. A library normally uses one system; mixing them in the sample is only to show all three.

### Adding a book

1. **Draft it.** Put the ISBNs, one per line, in a text file (or collect them by scanning a pile of books: `python tools/book_scan_test.py --live --append isbns.txt` adds each newly scanned ISBN to the file, once, with a beep and the title from Open Library) and run `python tools/import_books.py isbns.txt`. It asks Open Library for the title, authors, subjects, description and year and writes `data/catalog_draft.csv`. It never touches `catalog.csv`. It prints the subjects it couldn't match to your vocabulary: add those as aliases in `topics.yaml` and run it again (answers are cached, so a re-run is quick). You can also skip this and type rows by hand.
2. **Review** the draft: fix the title and author, check the subjects, and fill in what Open Library can't know: `genre`, `shelf`, `slot`, `call_number`, `section`, `floor`, `series`, `series_index`.
3. **Paste** the rows into `catalog.csv`.
4. **Run `python tools/catalog_check.py`.** Fix every error. Read the warnings: a book with fewer than two topics, or with no neighbour above the threshold, will rarely be recommended, which usually means its subjects need work.
5. **Test one**: `python -m robot sim`, then `book <isbn>` (or `where <title>`), then `recommend`. `python tools/make_test_barcodes.py` makes a printable barcode for every catalogue book if you want to try the camera.

## topics.yaml

A controlled vocabulary, so that "space", "cosmos" and "astronomy" are one topic, and so that related topics can meet under a broader one.

```yaml
science:
  aliases: [sciences, natural science]
astronomy:
  broader: science                    # astronomy is a kind of science
  aliases: [space, cosmos, universe]  # these subjects all mean "astronomy"
physics:
  broader: science
```

- **Name** (the key): the canonical topic, as a visitor would say it. Lower case.
- **`aliases`**: other words and spellings that mean the same. Matching ignores case, hyphens, underscores and extra spaces, so `Outer-Space`, `outer space` and `OUTER_SPACE` are the same alias.
- **`broader`**: the more general topic, one name (or a list). A book about astronomy and a book about physics share no topic, but both fall under `science`, which earns partial credit. A topic that no book uses but that is somebody's `broader` is a "broader topic" in the graph.
- **General and specific topics.** A topic that some other topic names as its `broader` (like `science` or `fiction`) is *general*; one with nothing narrower under it (`astronomy`, `dystopia`) is *specific*. Sharing a general topic counts for less in scoring (`books.similarity.general_topics`, 0.4 of the normal weight), and the robot never gives a general topic as the spoken reason when the two books share a specific one. So give every real subject area its own narrower topic rather than leaving books under `science`.
- **`description`**: a note for yourself; never used.

A subject in `catalog.csv` that matches nothing is **kept as typed** (so nothing is lost) but listed by the checker as unknown. Add it to the vocabulary, as an alias of an existing topic or as a new topic, whichever it is. Mistakes in this file (a broader topic that doesn't exist, an alias used for two topics, a loop of broader topics) are errors.

## shelves.csv

One row per shelf. `catalog.csv` refers to a shelf by its `shelf` id.

| Column | What goes in it |
|---|---|
| `shelf` | The id, unique (case doesn't matter), e.g. `3-SCI-A`. Whatever you use in `catalog.csv`. |
| `section` | The section's name, e.g. `Science`. Spoken to visitors: "in the Science section". |
| `floor` | The floor number, a whole number. Spoken as "on level 3". |
| `zone` | `quiet` or `common`: what kind of area the shelf is in. |
| `map_x`, `map_y` | The shelf's position in metres in the map of that floor. Where the robot drives when it guides a visitor. |
| `map_yaw` | The direction the robot should face on arrival, in radians. |
| `description` | A note: which authors or numbers the shelf holds, how to find it. Never spoken. |

The map columns may be left empty. The robot can then still tell a visitor where the shelf is, but it cannot drive there and says so. **The poses in the sample file are placeholders**: each description says TODO. Replace them once the real map exists. The coordinate frame is whatever the navigation system will use, which hasn't been built yet; treat these numbers as provisional until it is.

### Adding a shelf

1. Add a row to `shelves.csv` with a new id and its section, floor and zone. Fill in the map pose if you have it.
2. Put the id in the `shelf` column of the books that live there.
3. Run `python tools/catalog_check.py`: a book whose shelf isn't defined is an error, and a shelf with no pose is a warning.

## curated_links.csv

A librarian's connections that subjects and text alone wouldn't find.

| Column | What goes in it |
|---|---|
| `from_isbn`, `to_isbn` | The two books (both must be in `catalog.csv`). |
| `relation` | `series_next`: `to_isbn` comes right after `from_isbn` (use it when `series` and `series_index` can't say so, for example across two series). `librarian_pick`: "if you liked this, read that", the strongest hand-made link. `related`: a weaker connection. |
| `note` | A short reason in plain words. It is shown to the language model as the reason for the recommendation, so write something you'd be happy to hear spoken: `After Cosmos, go deeper into the physics of the universe`. |

Links work in both directions for scoring, except that `series_next` has a direction: the next book is recommended after the earlier one.

## How a recommendation is scored

For the book a visitor holds, every other book gets a score from 0 to 1 built from seven parts, each with a short reason in words. The reasons go to the language model, so what the robot says is grounded in them ("both about astronomy and popular science; librarian's pick: ...").

| Part | Weight | It measures |
|---|---|---|
| `topics` | 0.30 | Topics in common, weighted by rarity: sharing `volcano` counts for more than sharing `fiction`; general topics (with a narrower child) count for 0.4 of that. |
| `broader` | 0.10 | Topics that meet under a broader one, at partial credit. |
| `author` | 0.12 | The same author. |
| `series` | 0.15 | The same series. The next book in a series is always ranked first. |
| `call_number` | 0.08 | Same class, and how near the numbers are within it (`QB981`/`QB982` yes, `QB44.2`/`QB981` no). |
| `curated` | 0.10 | A `librarian_pick` or `related` row. |
| `text` | 0.15 | How alike the summaries and subjects are as text (TF-IDF, standard library only; `robot/services/text_similarity.py` explains how to plug in a better model). |

Books scoring under `min_score` (0.08) are not recommended; ties are broken by title, so the answer is always the same. All nine numbers (the seven weights, `min_score` and `general_topics`) are under `books.similarity` in `config/robot.yaml`; the total is divided by the weights' sum, so only their proportions matter.

To see how well it works on your books:

```bash
python tools/similarity_report.py               # each book's neighbours by the graph vs text alone, and where they disagree
python tools/similarity_report.py --make-pairs  # writes data/eval/similarity_pairs.csv: about 20 pairs, label column empty
# open that file and put 1 (a reader of one would want the other) or 0 in the label column, then:
python tools/similarity_report.py --eval        # mean score of similar vs not-similar pairs, and ranking accuracy, per method
```

### What the robot says

Two kinds of reason come out of the scoring. The **full list** (every component that applies, strongest first) goes to the language model as background and to the log (`candidate A Brief History of Time (0.30): both about ...`). What the visitor hears is **one natural reason**, the most specific thing the two books share, chosen in this order: series ("because it's the next book in Asimov's Future History"), same author ("because it's also by Carl Sagan"), a curated link ("because the librarians suggest it after Cosmos"; a librarian pick says "after" only in the direction you wrote it), the most specific shared topic ("because, like Cosmos, it's about astronomy"), the shelf area ("because, like Cosmos, it's shelved with astronomy"), then a general topic, a broader topic, and last "because it's similar to Cosmos". It never lists several topics: the catalogue wording is built from that one sentence, and the language model is told to give one reason per pick, taken from it.

How many books are named depends on the style (`recommendations` in `config/styles.yaml`): the top pick only in a quiet zone, up to three in a common area.

A disagreement in the report usually means a missing or wrong topic: fix the book's `subjects` or add an alias. Label the pairs yourself, from what a reader would want; the file leaves the machine's scores out so they don't sway you.

## Where books live, and guiding

The scanner puts the book's section, floor, zone and map pose on the `BookScanned` event. Returning a book says the storage slot and where it belongs ("on shelf 3-SCI-A, in the Science section on level 3"). After a return or a recommendation the robot offers to guide the visitor to that shelf, and "guide me" publishes a `NavigationRequested` event with the shelf's pose. Visitors can also ask "where is Dune?". In the simulator, `where <title or isbn>` and `guide` do the same, and the fake base just logs the `navigate` command.

The graph holds books and the library's layout only. It never holds visitors, face identities or anything about people.

## floorplan.yaml

The screen draws directions on this plan: the target section highlighted, the robot ("You are here"), an approximate
path and the distance. Coordinates are **metres in the same map frame as `shelves.csv` (`map_x`, `map_y`)**, one frame
per floor, x to the right and y down the plan. The sample has three floors because the sample shelves are on levels 1,
2 and 3; every rectangle in it is a placeholder until the real building is surveyed.

```yaml
floors:
  - level: 3                 # the floor number used in shelves.csv
    name: Level 3            # shown on the screen
    width: 12                # the floor's size in metres
    height: 8
    sections:                # one rectangle per section: x, y = top-left corner, w, h = size (metres)
      - {name: Science, x: 3.1, y: 0.3, w: 1.9, h: 2.7}
      - {name: History, x: 5.1, y: 0.3, w: 1.9, h: 2.7}
    landmarks:               # kind: lift, help_desk, entrance, stairs, toilets or other
      - {kind: lift, name: Lift, x: 10.8, y: 6.8}
station:                     # where the robot waits for visitors: its "you are here" (and where it drives home to)
  level: 1
  x: 9.0
  y: 5.5
  yaw: 3.14
```

- A section's `name` must match `section` in `shelves.csv` exactly (case aside) and be unique across floors.
- Give every floor a `lift` landmark: directions to another floor start with "Take the lift to Level N" and draw the
  path from the lift.
- The path is **approximate** (straight or L-shaped, first along x then y); it does not avoid shelves or walls.
- `tools/catalog_check.py` loads the plan and reports, as errors, a rectangle outside its floor, a section name used
  twice, a landmark of an unknown kind, a shelf whose section is not on its floor, and a shelf whose `map_x`/`map_y`
  lies outside its section's rectangle.

## Covers

The book card and the list show each book's cover from `data/covers/<isbn>.jpg` (gitignored). Download them from Open
Library's covers service:

```bash
python tools/import_books.py --covers-only             # every book in catalog.csv
python tools/import_books.py isbns.txt --covers        # while drafting new books
```

It asks `https://covers.openlibrary.org/b/isbn/<isbn>-L.jpg?default=false` (with `default=false` Open Library answers
404 when it has no cover instead of a blank image), skips covers already downloaded, and waits at least 3 s between
requests (Open Library allows 100 cover look-ups by ISBN per 5 minutes per address). A book without a cover gets a
generated placeholder: its title on a colour derived from its ISBN. `catalog_check.py` lists books without a cover as
a warning.

