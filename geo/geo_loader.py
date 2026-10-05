import json
import logging
import sys
from pathlib import Path
import requests

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import GEO_DIR, COUNTY_ALIASES

logger = logging.getLogger(__name__)

COUNTY_TOPO_URL = "https://raw.githubusercontent.com/g0v/twgeojson/master/json/twCounty2010merge.topo.json"
TOWN_TOPO_URL = "https://raw.githubusercontent.com/g0v/twgeojson/master/json/twTown1982.topo.json"

COUNTY_GEOJSON_PATH = GEO_DIR / "tw_counties.geojson"
TOWN_GEOJSON_PATH = GEO_DIR / "tw_towns.geojson"


def _decode_topojson(topo_dict, layer_key="layer1"):
    """
    Decodes TopoJSON arcs into GeoJSON FeatureCollection.
    Handles scaling, translation, and delta coordinates.
    """
    transform = topo_dict.get("transform", {})
    scale = transform.get("scale", [1.0, 1.0])
    trans = transform.get("translate", [0.0, 0.0])
    raw_arcs = topo_dict.get("arcs", [])

    decoded_arcs = []
    for arc in raw_arcs:
        coords = []
        x, y = 0, 0
        for dx, dy in arc:
            x += dx
            y += dy
            coords.append([
                round(x * scale[0] + trans[0], 6),
                round(y * scale[1] + trans[1], 6)
            ])
        decoded_arcs.append(coords)

    def resolve_ring(ring_indices):
        ring = []
        for idx in ring_indices:
            if idx >= 0:
                coords = decoded_arcs[idx]
            else:
                coords = list(reversed(decoded_arcs[~idx]))
            if not ring:
                ring.extend(coords)
            else:
                ring.extend(coords[1:])
        return ring

    geometries = topo_dict.get("objects", {}).get(layer_key, {}).get("geometries", [])
    features = []

    for geom in geometries:
        gtype = geom.get("type")
        props = geom.get("properties", {})
        arcs = geom.get("arcs", [])

        if gtype == "Polygon":
            coordinates = [resolve_ring(ring) for ring in arcs]
        elif gtype == "MultiPolygon":
            coordinates = [[resolve_ring(ring) for ring in poly] for poly in arcs]
        else:
            continue

        features.append({
            "type": "Feature",
            "properties": props,
            "geometry": {
                "type": gtype,
                "coordinates": coordinates
            }
        })

    return {"type": "FeatureCollection", "features": features}


def _calculate_centroid(feature):
    """Simple centroid calculation for label placement."""
    geom = feature.get("geometry", {})
    coords = geom.get("coordinates", [])
    gtype = geom.get("type")
    
    all_pts = []
    if gtype == "Polygon":
        for ring in coords:
            all_pts.extend(ring)
    elif gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                all_pts.extend(ring)
                
    if not all_pts:
        return [121.0, 23.5]
    
    avg_lng = sum(p[0] for p in all_pts) / len(all_pts)
    avg_lat = sum(p[1] for p in all_pts) / len(all_pts)
    return [round(avg_lng, 4), round(avg_lat, 4)]


def ensure_geojson_assets():
    """
    Downloads and caches Taiwan Counties and Towns GeoJSON files.
    Normalizes administrative division names.
    """
    if COUNTY_GEOJSON_PATH.exists() and TOWN_GEOJSON_PATH.exists():
        logger.info("GeoJSON files already exist.")
        return True

    print("Fetching and processing Taiwan GeoJSON map boundaries...")
    
    # 1. County boundaries
    if not COUNTY_GEOJSON_PATH.exists():
        try:
            resp = requests.get(COUNTY_TOPO_URL, timeout=15)
            resp.raise_for_status()
            topo = resp.json()
            geojson = _decode_topojson(topo, layer_key="layer1")
            
            # Normalize county names
            for feat in geojson["features"]:
                props = feat["properties"]
                name = props.get("name", props.get("COUNTYNAME", ""))
                # Handle aliases
                norm_name = COUNTY_ALIASES.get(name, name)
                norm_name = norm_name.replace("台", "臺")
                props["COUNTYNAME"] = norm_name
                props["name"] = norm_name
                props["centroid"] = _calculate_centroid(feat)
                
            with open(COUNTY_GEOJSON_PATH, "w", encoding="utf-8") as f:
                json.dump(geojson, f, ensure_ascii=False)
            print(f"Saved {len(geojson['features'])} counties to {COUNTY_GEOJSON_PATH}")
        except Exception as e:
            print(f"Failed to fetch county TopoJSON: {e}")
            return False

    # 2. Town boundaries
    SPECIAL_MUNIS = {'臺北市', '新北市', '桃園市', '臺中市', '臺南市', '高雄市'}
    COUNTY_MERGES = {
        '臺中縣': '臺中市',
        '臺南縣': '臺南市',
        '高雄縣': '高雄市',
        '桃園縣': '桃園市',
        '臺北縣': '新北市',
    }
    TOWN_UPGRADES = {
        ('苗栗縣', '頭份鎮'): '頭份市',
        ('彰化縣', '員林鎮'): '員林市',
        ('雲林縣', '臺西鄉'): '台西鄉',
        ('臺東縣', '臺東市'): '台東市',
        ('高雄市', '三民鄉'): '那瑪夏區',
        ('高雄縣', '三民鄉'): '那瑪夏區',
    }

    if not TOWN_GEOJSON_PATH.exists():
        try:
            resp = requests.get(TOWN_TOPO_URL, timeout=20)
            resp.raise_for_status()
            topo = resp.json()
            geojson = _decode_topojson(topo, layer_key="layer1")
            
            valid_features = []
            for feat in geojson["features"]:
                props = feat["properties"]
                cname = props.get("COUNTYNAME", "")
                tname = props.get("TOWNNAME", "")
                
                # Exclude purely territorial maritime/port polygons
                if "(海)" in tname:
                    continue

                cname = COUNTY_ALIASES.get(cname, cname).replace("台", "臺")
                cname = COUNTY_MERGES.get(cname, cname)

                tname = tname.replace("台", "臺")
                if (cname, tname) in TOWN_UPGRADES:
                    tname = TOWN_UPGRADES[(cname, tname)]

                # All subdivisions in special municipalities are Districts (區)
                if cname in SPECIAL_MUNIS:
                    if tname.endswith(('市', '鎮', '鄉')):
                        tname = tname[:-1] + '區'
                
                props["COUNTYNAME"] = cname
                props["TOWNNAME"] = tname
                props["FULLNAME"] = f"{cname}{tname}"
                props["name"] = tname
                props["centroid"] = _calculate_centroid(feat)
                valid_features.append(feat)

            geojson["features"] = valid_features
                
            with open(TOWN_GEOJSON_PATH, "w", encoding="utf-8") as f:
                json.dump(geojson, f, ensure_ascii=False)
            print(f"Saved {len(valid_features)} normalized towns to {TOWN_GEOJSON_PATH}")
        except Exception as e:
            print(f"Failed to fetch town TopoJSON: {e}")
            return False

    return True


def load_county_geojson():
    ensure_geojson_assets()
    with open(COUNTY_GEOJSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_town_geojson():
    ensure_geojson_assets()
    with open(TOWN_GEOJSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    ensure_geojson_assets()
