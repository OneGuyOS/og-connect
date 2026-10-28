import subprocess
import re


def get_battery_info(serial: str) -> dict:
    cmd = ["adb", "-s", serial, "shell", "dumpsys", "battery"]
    try:
        output = subprocess.run(cmd, capture_output=True, text=True, check=True)
        info = {}
        pattern = re.compile(r"(\w+):\s+(.*)")
        for line in output.stdout.split("\n"):
            match = pattern.match(line)
            if match:
                key, value = match.groups()
                if key == "level":
                    info[key] = int(value)
                elif key == "temperature":
                    info[key] = float(value) / 10
                elif key == "status":
                    status_map = {"2": "charging", "3": "discharging",
                                  "4": "not charging", "5": "full"}
                    info[key] = status_map.get(value, "unknown")
                elif key == "charging":
                    info[key] = value.lower() == "true"
        return info
    except subprocess.CalledProcessError:
        return {}
