import json
import time
import os
import requests

session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0'})

def photon_geocode(query: str):
    url = 'https://photon.komoot.io/api/'
    try:
        r = session.get(url, params={'q': query, 'limit': 1}, timeout=15)
        if r.status_code == 200:
            data = r.json()
            if data.get('features') and len(data['features']) > 0:
                coords = data['features'][0]['geometry']['coordinates']
                return float(coords[1]), float(coords[0])
    except Exception:
        pass
    return None

def main():
    json_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'shipyards_data.json')
    json_path = os.path.abspath(json_path)

    with open(json_path, 'r', encoding='utf-8') as f:
        shipyards = json.load(f)

    to_refine = [s for s in shipyards if s.get('geo_level') == 'location']
    print(f"需要精确化的船厂: {len(to_refine)} 个")

    improved = 0
    failed = []

    for i, sy in enumerate(to_refine, 1):
        name = sy['name']
        location = sy['location']
        queries = [f"{name}, {location}", f"{name}"]
        coords = None
        for q in queries:
            coords = photon_geocode(q)
            if coords:
                break
            time.sleep(0.15)

        if coords:
            old_lat, old_lng = sy['lat'], sy['lng']
            if abs(coords[0] - old_lat) < 0.02 and abs(coords[1] - old_lng) < 0.02:
                print(f"[{i}/{len(to_refine)}] {name} -> 相近跳过")
            else:
                sy['lat'] = coords[0]
                sy['lng'] = coords[1]
                sy['geo_level'] = 'precise'
                improved += 1
                print(f"[{i}/{len(to_refine)}] {name} -> {coords[0]:.6f}, {coords[1]:.6f}")
        else:
            failed.append(name)
            print(f"[{i}/{len(to_refine)}] {name} -> FAILED")

        time.sleep(0.15)

    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(shipyards, f, ensure_ascii=False, indent=2)

    print(f"\n完成: 精确化了 {improved}/{len(to_refine)} 个")
    if failed:
        print(f"失败 {len(failed)} 个:")
        for n in failed:
            print(f"  - {n}")

if __name__ == '__main__':
    main()
