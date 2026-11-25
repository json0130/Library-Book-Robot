# LiDAR maps

`../run_lidar_mapping.sh [name]` saves its maps here (`map_saver` output: `<name>.pgm` and `<name>.yaml`, plus a `<name>.png` to look
at; an older map it replaced is kept as `<name>.bak-<time>.*`). `../run_robot_link.sh --amcl` uses `lidar_map.yaml`, or another one
with `--map maps/<name>.yaml`.

The maps are not in git (see `../.gitignore`): they belong to the room and the robot, not to the code.

The map's frame is where the robot stood when mapping started: (0, 0), facing +x. Start mapping on the station, so the station is
`{x: 0.0, y: 0.0, yaw: 0.0}` in `../../data/floorplan.yaml`.

`python3 ../../tools/keepout_preview.py` draws the keep-out zones (`../config/keepout.yaml`) on `lidar_map` as `lidar_map_keepout.png`
here, to check them before driving. The zones are in this map's frame too: make them again with a new map.
