# Extras - earlier experiments

These are the steps the project went through before the visual SLAM version in
the main folder. They are kept for reference and are **not maintained**.

Unlike the main scripts, these expect their files in the robot's **home folder**.
To try them, copy everything into `~` first:

```bash
cp extras/lidar_slam/* extras/early_noise_maps/* config/* nodes/* ~/
```

## `lidar_slam/` - LiDAR-only mapping and navigation

| File | What it does |
|---|---|
| `lidar_mapping.launch` | Hokuyo URG-04LX driver + gmapping (2D LiDAR SLAM) |
| `run_mapping.sh` | One command: robot base + LiDAR mapping + map viewer + keyboard; saves `~/lab_map` on Ctrl-C |
| `auto_explore.py` | Makes the robot wander by itself and avoid obstacles using the LiDAR |
| `noise_navigation.launch` | Saved LiDAR map + AMCL localization + move_base |
| `run_noise_nav.sh` | One command for click-to-go noise mapping on the LiDAR map |

**Note:** `config/hokuyo_costmap.yaml` now plans on the cleaned map
(`/map_clean`). When running these, also start `python3 ~/map_cleaner.py` in
another window (or delete the `static_layer:` lines from the yaml). The web
page's "Robot position" box stays red here because it listens for the camera
(RTAB-Map), not AMCL - that's expected.

Why we moved on: AMCL often lost track of where the robot was on this map, so
the planner failed or the robot spun on the spot. The camera-based
localization in the main version recognises the room by itself.

## `early_noise_maps/` - first noise maps (no SLAM)

| File | What it does |
|---|---|
| `plot_noise_map.py` | After a run, draws `noise_log.csv` as dots along the path (darker = louder) |
| `live_noise_map.py` | Same idea, drawn live in the browser (`/noise_map` topic) |
| `noise_cave_map.py` | A fixed 10 x 2 m area that fills in ("carves out") as the robot drives, shaded by loudness (`/noise_cave_map` topic) |

These use wheel odometry only, so the position drifts over longer drives.
