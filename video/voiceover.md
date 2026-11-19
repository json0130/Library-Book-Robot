# Voiceover script

13 lines, 206 words (the limit is 250). Read each at a calm pace, about 140 words a minute, starting at the time on
its left. Stay silent in between, above all in the common-area visit, where Stuart speaks. Save each line as the file named in the table
(`python tools/video/voiceover.py record` does that for you); a line you record replaces the TTS narration for that line, the others keep
the TTS (a backup copy of the narration is in `video/voiceover_tts/`). The times come from `python tools/video/edit.py --plan` and move
if the footage changes: `python tools/video/voiceover.py cues --write` makes this page again.

| Starts | File (video/voiceover/) | Time it has | About | Line |
|---|---|---|---|---|
| 0:00.0 | `hook-quiet.wav` | 5.5 s | 4.8 s | Libraries are not one kind of space. Some rooms are silent, |
| 0:05.5 | `hook-lively.wav` | 12.0 s | 8.3 s | others are lively, and people expect different behaviour in each. Service robots usually behave the same everywhere. Stuart doesn't. |
| 0:17.5 | `robot-concept.wav` | 10.0 s | 7.0 s | So we present Stuart, the library robot that returns books and helps patrons, working alongside librarians. |
| 0:27.5 | `styles.wav` | 7.0 s | 6.1 s | It changes how it communicates, not what it does, depending on where it is. |
| 0:34.5 | `system.wav` | 8.5 s | 7.8 s | A Jetson handles the conversation and the screen. A TurtleBot base handles navigation and measures the sound level. |
| 0:43.0 | `parts.wav` | 9.0 s | 9.6 s (tight) | Books are read by an RFID reader, obstacles are seen by a LiDAR, and Stuart's face is shown on a small screen. |
| 0:52.0 | `maps.wav` | 12.0 s | 11.3 s | As it maps the library, Stuart measures the sound level and builds a sound map. Quiet places become quiet zones, and lively places become common areas. |
| 1:04.0 | `common-card.wav` | 50.7 s | 3.9 s | In the common area, Stuart greets people and talks. |
| 1:54.7 | `trip-out.wav` | 18.8 s | 5.2 s | When the patron leaves, Stuart drives the returned books to their section, |
| 2:13.5 | `unload.wav` | 5.0 s | 4.8 s (tight) | and unloads them into a tray for a librarian to shelve. |
| 2:18.5 | `trip-back.wav` | 8.8 s | 3.0 s | Then it heads back to its station. |
| 2:27.3 | `quiet.wav` | 25.5 s | 14.3 s | On the way, Stuart meets someone in a quiet zone. It never starts the conversation, and its answers appear as text on the screen, with no sound, so students nearby can keep working. |
| 2:52.8 | `end.wav` | 4.0 s | 3.5 s | Stuart: a library robot that reads the room. |

## The whole script

Libraries are not one kind of space. Some rooms are silent,

others are lively, and people expect different behaviour in each. Service robots usually behave the same everywhere. Stuart doesn't.

So we present Stuart, the library robot that returns books and helps patrons, working alongside librarians.

It changes how it communicates, not what it does, depending on where it is.

A Jetson handles the conversation and the screen. A TurtleBot base handles navigation and measures the sound level.

Books are read by an RFID reader, obstacles are seen by a LiDAR, and Stuart's face is shown on a small screen.

As it maps the library, Stuart measures the sound level and builds a sound map. Quiet places become quiet zones, and lively places become common areas.

In the common area, Stuart greets people and talks.

When the patron leaves, Stuart drives the returned books to their section,

and unloads them into a tray for a librarian to shelve.

Then it heads back to its station.

On the way, Stuart meets someone in a quiet zone. It never starts the conversation, and its answers appear as text on the screen, with no sound, so students nearby can keep working.

Stuart: a library robot that reads the room.
