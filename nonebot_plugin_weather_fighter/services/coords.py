from __future__ import annotations


def valid_coords(coords: tuple[float, float]) -> bool:
    lat, lon = coords
    return -90 <= lat <= 90 and -180 <= lon <= 180


def coords_query(coords: tuple[float, float]) -> dict[str, str]:
    if not valid_coords(coords):
        raise ValueError(f"坐标无效: {coords!r}")
    lat, lon = coords
    return {"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}"}
