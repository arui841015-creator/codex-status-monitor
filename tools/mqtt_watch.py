"""Subscribe to one device topic and print every payload (test harness).

    .venv\\Scripts\\python.exe tools\\mqtt_watch.py sdg-doubao-probe01 [seconds]
"""
from __future__ import annotations

import json
import sys
import time

import paho.mqtt.client as mqtt

device_id = sys.argv[1] if len(sys.argv) > 1 else "sdg-doubao-probe01"
duration = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0
topic = f"sdgoods/codex/v1/devices/{device_id}/status"


def on_connect(client, _userdata, _flags, rc, *_args):
    print(f"[connect rc={rc}] {topic}", flush=True)
    client.subscribe(topic, qos=1)


def on_message(_client, _userdata, message):
    try:
        payload = json.loads(message.payload.decode("utf-8"))
    except ValueError:
        print("[raw]", message.payload[:200], flush=True)
        return
    print(f"[msg] {payload.get('message')}  state={payload.get('detail', {}).get('status')}  "
          f"raw={payload.get('detail', {}).get('project_raw')}", flush=True)


client = mqtt.Client(client_id=f"mqtt-watch-{int(time.time())}", protocol=mqtt.MQTTv311)
client.on_connect = on_connect
client.on_message = on_message
client.connect("117.72.43.16", 1883, 30)
client.loop_start()
time.sleep(duration)
client.loop_stop()
client.disconnect()
print("done", flush=True)
