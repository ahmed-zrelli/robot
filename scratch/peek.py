import json

d = json.load(open("backend/data/left_front_feedback.json"))
print(type(d))
if isinstance(d, dict):
    print(list(d.keys()))
    print(str(d)[:400])
else:
    print(len(d))
    print(json.dumps(d[0], indent=1)[:400])
    print("t range:", d[0].get("t"), d[-1].get("t"))
