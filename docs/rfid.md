# RFID book reading

A book can be identified two ways, and both give the robot exactly the same thing (BookPlaced, then BookIdentified with the catalogue record: section,
shelf, map pose), so the visit goes the same either way:

| | how | code |
|---|---|---|
| Camera | hold the ISBN barcode up to the scan-pad camera | `robot/perception/book_scanner.py`, `book_watch.py` |
| RFID | hold the book's tag to the reader | `robot/perception/rfid.py`, `rfid/rfid_uid.ino` |

`books.reader` in `config/robot.yaml` chooses: `camera`, `rfid` or `both` (the default). With `both` neither gets in the other's way: a tag read
makes the camera ignore that BookPlaced, and a book is only taken when the robot is expecting one (the same rule the barcode watcher uses).
If the tag reader is not plugged in, the robot says so once in the log and tries again every 10 s; the camera keeps working.

## Hardware

An Arduino Uno with an MFRC522 reader on USB serial (the Jetson sees it as `/dev/ttyACM0`; `books.rfid.port` changes that).

```
MFRC522   SDA D10   SCK D13   MOSI D11   MISO D12   RST D9   3.3V   GND
```

Put `rfid/rfid_uid.ino` on the Arduino (library "MFRC522"). It sends one line per tag held to it: `UID 04 65 B2 42 CD 21 90`.
`rfid/rfid_write.ino` and `rfid_read.py` are the first version from the rfid branch (titles written into the sketch); the robot understands its
lines too (`DUNE`), by matching the catalogue title, but registering tags in a file is easier than editing the sketch for every book.

## Register a tag to a book

The tag only carries a number. `data/rfid_tags.csv` says which book it is (`uid,isbn,title`). It already has the two tags named in `rfid_write.ino`.

```
python tools/rfid_register.py --listen            # see what the reader sends and how it is read (stop the robot first: one program per port)
python tools/rfid_register.py                     # hold a tag to the reader, then type the ISBN or the title: saved
python tools/rfid_register.py --uid "1D 5C 7B 36" --isbn 9780441172719      # no reader needed
python tools/rfid_register.py --list
```

The robot reads the file again when it changes, so a new tag works without a restart.

## Try it without the reader

In the keyboard simulator (`python -m robot sim`), `engage` and then:

```
tag UID 04 65 B2 42 CD 21 90        # a registered tag
tag ISBN 9780345391803              # an ISBN on the tag
tag Dune                            # a title, as rfid_write.ino sends
```

A tag that is not registered, or a title that is not in the catalogue, is a failed scan with the reason (the robot asks for the book again, as it does
when a barcode cannot be read).

## What the lines mean

| line | read as |
|---|---|
| `UID 04 65 B2 42 CD 21 90`, `04 65 B2 42 CD 21 90` | tag number (4, 7 or 10 bytes), looked up in `rfid_tags.csv` |
| `ISBN 978-0-13-235088-4`, a bare ISBN-13 or ISBN-10 | the ISBN, used as it is |
| `DUNE` | a title, matched against the catalogue |
| anything else | ignored |

A tag that keeps being sent while it stays on the reader counts as one book (`books.rfid.gap_s`, 2.5 s); taken away and held again it is a new one.
