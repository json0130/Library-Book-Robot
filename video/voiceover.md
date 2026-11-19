# Voiceover script

13 lines, 208 words (the limit is 250). Read each at a calm pace, about 140 words a minute, starting at the time on
its left. Stay silent in between, above all in the common-area visit, where Stuart speaks. Save each line as the file named in the table
(`python tools/video/voiceover.py record` does that for you); a line you record replaces the TTS narration for that line, the others keep
the TTS (a backup copy of the narration is in `video/voiceover_tts/`). The times come from `python tools/video/edit.py --plan` and move
if the footage changes: `python tools/video/voiceover.py cues --write` makes this page again.

| Starts | File (video/voiceover/) | Time it has | About | Line |
|---|---|---|---|---|
| 0:00.0 | `hook-quiet.wav` | 5.5 s | 4.8 s | Libraries are not one kind of space. Some rooms are silent, |
| 0:05.5 | `hook-lively.wav` | 12.0 s | 8.3 s | others are lively, and people expect different behaviour in each. Service robots usually behave the same everywhere. Stuart doesn't. |
| 0:17.5 | `robot-concept.wav` | 8.5 s | 6.1 s | Meet Stuart, a library robot that returns books and helps patrons, working alongside librarians. |
| 0:26.0 | `styles.wav` | 7.0 s | 6.1 s | It changes how it communicates, not what it does, depending on where it is. |
| 0:33.0 | `system.wav` | 8.5 s | 7.8 s | A Jetson handles the conversation and the screen. A TurtleBot base handles navigation and measures the sound level. |
| 0:41.5 | `parts.wav` | 9.0 s | 7.0 s | An RFID reader identifies books, a LiDAR sees obstacles, and a small screen shows Stuart's face. |
| 0:50.5 | `maps.wav` | 12.0 s | 10.9 s | As it drives around, Stuart measures the sound level and builds a sound map. Quiet places become quiet zones, and lively places become common areas. |
| 1:02.5 | `common-card.wav` | 68.0 s | 3.9 s | In the common area, Stuart greets people and talks. |
| 2:10.5 | `trip-out.wav` | 9.4 s | 6.1 s | When the patron leaves, Stuart carries the returned books to their section, here Fiction, |
| 2:19.9 | `trip-back.wav` | 5.3 s | 3.0 s | and then heads back to its station. |
| 2:25.2 | `quiet.wav` | 25.0 s | 22.2 s | On the way, Stuart enters a quiet zone, which it knows from its sound map. Here it never calls out. It waits for someone to come to it, then greets them and answers in text on the screen, with no sound, so students nearby can keep working. Same task, different style. |
| 2:50.2 | `end.wav` | 4.0 s | 3.5 s | Stuart: a library robot that reads the room. |
| 2:54.2 | `thanks.wav` | 2.5 s | 0.9 s | Thank you. |

## The whole script

Libraries are not one kind of space. Some rooms are silent,

others are lively, and people expect different behaviour in each. Service robots usually behave the same everywhere. Stuart doesn't.

Meet Stuart, a library robot that returns books and helps patrons, working alongside librarians.

It changes how it communicates, not what it does, depending on where it is.

A Jetson handles the conversation and the screen. A TurtleBot base handles navigation and measures the sound level.

An RFID reader identifies books, a LiDAR sees obstacles, and a small screen shows Stuart's face.

As it drives around, Stuart measures the sound level and builds a sound map. Quiet places become quiet zones, and lively places become common areas.

In the common area, Stuart greets people and talks.

When the patron leaves, Stuart carries the returned books to their section, here Fiction,

and then heads back to its station.

On the way, Stuart enters a quiet zone, which it knows from its sound map. Here it never calls out. It waits for someone to come to it, then greets them and answers in text on the screen, with no sound, so students nearby can keep working. Same task, different style.

Stuart: a library robot that reads the room.

Thank you.
