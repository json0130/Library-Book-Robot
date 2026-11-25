"""The Dell's side of the Jetson <-> Dell link (docs/robot_link.md, api_version 1).

Plain Python 3.8, standard library only, no ROS in here: the logic is in these modules so it can be tested anywhere, and
nodes/robot_link_api.py is the thin layer that connects it to ROS.

    core.py         LinkCore: goals, nav status, heartbeat watchdog, localised rule, speaking, noise (the whole API's behaviour)
    server.py       the HTTP server (token, JSON, errors) in front of a LinkCore
    rules.py        small pure functions (the localised rule, why a goal failed, distance left along a plan, battery)
    activity.py     is the robot making noise itself (speaking or moving)? Used by the API, the noise logger and the noise map
    calibration.py  ~/noise_calibration.json: dBFS -> dB offsets per microphone, and the fit made by calibrate_noise.py
    netaddr.py      the IPv4 address of a network interface (the API binds to it)
    dev_server.py   the real server over a simulated base, to test without ROS: python3 -m robot_link.dev_server
"""

API_VERSION = 1
