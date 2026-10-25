"""A stand-in for `open-airplay.exe -stdio` that speaks the control protocol."""

import json
import sys

PIN = "1234"
streams: dict[str, dict] = {}
errors: dict[str, dict] = {}


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def status():
    rows = sorted(streams.values(), key=lambda s: s["device_ip"])
    state = "idle"
    for want in ("connecting", "streaming", "pin_required"):
        if any(s["state"] == want for s in rows):
            state = want
    return {"ok": True, "state": state, "streams": rows, "errors": list(errors.values())}


def respond(rid, **fields):
    out({"type": "response", "id": rid, "ok": True, **fields})


def main():
    if "--die" in sys.argv:
        sys.stderr.write("boom\n")
        sys.exit(3)
    out({"type": "hello", "protocol": 1, "capabilities": {"audio": "--no-audio" not in sys.argv}})
    out({"type": "state", **status()})
    sys.stderr.write("screen capture started at 1920x1080@30fps using h264\n")
    sys.stderr.flush()
    for line in sys.stdin:
        req = json.loads(line)
        rid, cmd, target = req.get("id"), req.get("cmd"), req.get("target")
        if cmd == "devices":
            dev = {"name": "TV", "model": "X", "ip": "10.0.0.5", "port": 34247, "device_id": "a"}
            respond(rid, devices=[dev])
        elif cmd == "displays":
            disp = {"index": 0, "name": "D0", "x": 0, "y": 0, "width": 1920, "height": 1080}
            respond(rid, displays=[{**disp, "primary": True}])
        elif cmd == "connect" and "pin" in req:
            respond(rid, state="connecting")
            if req["pin"] == PIN:
                streams[target]["state"] = "streaming"
                streams[target].pop("credential_kind", None)
            else:
                streams.pop(target, None)
                errors[target] = {
                    "target": target,
                    "device": "TV",
                    "kind": "wrong_code",
                    "message": "secret technical detail",
                }
            out({"type": "state", **status()})
        elif cmd == "connect":
            errors.pop(target, None)
            streams[target] = {
                "device": "TV",
                "device_ip": target,
                "state": "pin_required",
                "credential_kind": "pin",
            }
            respond(
                rid, state="connecting", echo_port=req.get("port"), echo_opts=req.get("options")
            )
            out({"type": "state", **status()})
        elif cmd == "disconnect":
            streams.clear()
            respond(rid, state="idle")
            out({"type": "state", **status()})
        else:
            respond(rid, **status())
    sys.exit(0)


if __name__ == "__main__":
    main()
