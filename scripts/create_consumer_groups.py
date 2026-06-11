# run once on first deployment to create all consumer groups. IDEMPOTENT

import redis

STREAMS = {
    "frames:test_camera":[
        "detection_group",
    ],

    "events:detections":[
        "recognition_group",
        "zone_monitor_group",
        "headcount_group",
        "pedestrian_group",
    ],

    "events:recognitions":[
        "restricted_entry_group",
        "intruder_group",
    ],

    "events:zone":[
        "intruder_group",
    ],

    "events:crowd_frames":[
        "crowd_density_group",
    ],

    "alerts:live":[
        "api_alert_group",
    ],
}

def create_groups():
    r = redis.Redis(host="localhost", port=6379, decode_responses=True)

    for stream, groups in STREAMS.items():
        for group in groups:
            try:
                r.xgroup_create(stream, group, id="0", mkstream=True)
                print(f"Created: {stream} / {group}")
            except redis.exceptions.ResponseError as e:
                if "BUSYGROUP" in str(e):
                    print(f"Exists: {stream} / {group}")
                else:
                    raise

if __name__ == "__main__":
    create_groups()