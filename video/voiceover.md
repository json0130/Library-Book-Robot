# Voiceover (under 250 words)

Read it at a calm pace, about 140 words a minute, and save it as `video/voiceover.wav` (48 kHz WAV, mono or stereo, starting at 0:00 of
the video). Each paragraph starts at the time on its left; leave the gaps silent, because Stuart speaks in the common-area shot.
Until the file exists, `tools/video/edit.py` uses each segment's `vo` line from `video/storyboard.yaml` (the same words) as a placeholder.

| Starts | Over | Line |
|---|---|---|
| 0:00 | quiet study area | Libraries are not one kind of space. Some rooms are silent, others are lively, and people expect different behaviour in each. |
| 0:05 | lively area | Service robots usually behave the same everywhere. Stuart doesn't. |
| 0:15 | system diagram | Stuart is a library robot that returns books and helps patrons, working alongside librarians. It changes how it communicates, not what it does, depending on where it is. |
| 0:26 | robot driving + sound map | As it maps the library, Stuart measures the sound level and builds a sound map. Quiet places become quiet zones, and lively places become common areas. The map keeps updating as Stuart moves. |
| 0:50 | "Common area" card (3 s, then Stuart speaks: leave it silent) | In the common area, Stuart greets people and talks. |
| 1:45 | quiet zone | In the quiet zone, the same task looks different. Stuart doesn't start the conversation. It waits for the patron, and its answers appear as text on the screen, with no sound, so students nearby can keep working. |
| 2:19 | returning trip | When the patron leaves, Stuart carries the books to their sections and unloads them into the return tray for a librarian to shelve. |
| 2:51 | closing stills | Same task. Different style. Stuart: a library robot that reads the room. |

Changed from the first draft: the common-area line is short and comes before Stuart speaks (the voiceover must not talk over
him), and it no longer says "by placing it on the RFID pad". The RFID reader is not fitted yet, so
the book in the video is read by the camera (or simulated), and the screen carries a badge saying so. Put the words back once the reader works.

## The whole script

Libraries are not one kind of space. Some rooms are silent, others are lively, and people expect different behaviour in each. Service robots
usually behave the same everywhere. Stuart doesn't.

Stuart is a library robot that returns books and helps patrons, working alongside librarians. It changes how it communicates, not what it does,
depending on where it is.

As it maps the library, Stuart measures the sound level and builds a sound map. Quiet places become quiet zones, and lively places become
common areas. The map keeps updating as Stuart moves.

In the common area, Stuart greets people and talks.

In the quiet zone, the same task looks different. Stuart doesn't start the conversation. It waits for the patron, and its answers appear as
text on the screen, with no sound, so students nearby can keep working.

When the patron leaves, Stuart carries the books to their sections and unloads them into the return tray for a librarian to shelve.

Same task. Different style. Stuart: a library robot that reads the room.
