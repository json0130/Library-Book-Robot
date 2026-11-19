# Voiceover (under 250 words)

Read it at a calm pace, about 140 words a minute, and save it as `video/voiceover.wav` (48 kHz WAV, mono or stereo, starting at 0:00 of
the video). Each line starts at the time on its left; stay silent in between, above all in the common-area visit (1:08 to 1:43), where Stuart
speaks. Until the file exists, `tools/video/edit.py` uses each segment's `vo` line from `video/storyboard.yaml` (the same words) as a placeholder.
The times are from `python tools/video/edit.py --plan`; they move if the footage changes.

| Starts | Over | Line |
|---|---|---|
| 0:00 | quiet library photo | Libraries are not one kind of space. Some rooms are silent, |
| 0:06 | lively library photo | others are lively, and people expect different behaviour in each. Service robots usually behave the same everywhere. Stuart doesn't. |
| 0:19 | the robot: concept, CAD, prototype | Stuart is a library robot that returns books and helps patrons, working alongside librarians. |
| 0:29 | common vs quiet interaction | It changes how it communicates, not what it does, depending on where it is. |
| 0:37 | system diagram | A Jetson handles the conversation and the screen. A TurtleBot base handles navigation and measures the sound level. |
| 0:46 | the parts, labelled | Books are read by an RFID reader, obstacles are seen by a LiDAR, and Stuart's face is shown on a small screen. |
| 0:55 | navigation map + sound map | As it maps the library, Stuart measures the sound level and builds a sound map. Quiet places become quiet zones, and lively places become common areas. |
| 1:08 | "Common area" card (3 s, then Stuart speaks: stay silent) | In the common area, Stuart greets people and talks. |
| 1:43 | driving to Fiction | When the patron leaves, Stuart drives the returned books to their section, |
| 2:03 | unloading | and unloads them into a tray for a librarian to shelve. |
| 2:08 | heading back | Then it heads back to its station. |
| 2:17 | the quiet zone: questions, answers as text (Stuart is silent) | On the way, Stuart meets someone in a quiet zone. It never starts the conversation, and its answers appear as text on the screen, with no sound, so students nearby can keep working. |
| 2:48 | end card | Stuart: a library robot that reads the room. |

Notes: the RFID line from the first draft is out until the reader and its clip are in the cut; the quiet-zone line comes while the
person asks questions (the quiet visit has no spoken words, so the voiceover can run over it).

## The whole script

Libraries are not one kind of space. Some rooms are silent,

others are lively, and people expect different behaviour in each. Service robots usually behave the same everywhere. Stuart doesn't.

Stuart is a library robot that returns books and helps patrons, working alongside librarians.

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
