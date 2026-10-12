"""
Shipyard map data API endpoints.
"""
import os
import json
import re
import shutil
import tempfile
import mysql.connector
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List as _List
from urllib.parse import quote
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.models import User
from app.core.security import get_current_user
from app.core.config import get_settings
from app.api.auth import _is_admin, _is_core
from app.api.admin import get_current_core
from app.shipyard_ai.exporter import export_to_excel, export_to_word
from app.shipyard_ai.deduplicator import (
    _extract_names_from_entity,
    find_db_match,
    find_matching_entity,
    load_shipyard_entities,
)
from app.shipyard_ai.name_matcher import ai_match_shipyard_name
from app.services.shipyard_import import import_shipyard_rows


# Columns excluded from raw export display (id kept for linking)
DISPLAY_EXCLUDED_COLS = {"introduce", "url", "crawled_at", "ai_processed"}

router = APIRouter(prefix="/shipyards", tags=["Shipyards"])
settings = get_settings()

# ---------------------------------------------------------------------------
# Imagery acquisition date (Esri World Imagery per-pixel identify), cached.
# ---------------------------------------------------------------------------
import asyncio
import time as _time
import urllib.parse
import urllib.request

_IMAGERY_CACHE_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", ".imagery_date_cache.json")
)
_IMAGERY_CACHE_TTL = 7 * 86400  # imagery updates infrequently; 7-day cache


def _read_imagery_cache() -> dict:
    try:
        with open(_IMAGERY_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _query_esri_imagery_date(lng: float, lat: float) -> dict:
    """Ask Esri's World Imagery identify API for the capture date at a point."""
    params = {
        "f": "json",
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "sr": 4326,
        "layers": "all",
        "tolerance": 0.00001,
        "mapExtent": f"{lng - 0.01},{lat - 0.01},{lng + 0.01},{lat + 0.01}",
        "imageDisplay": "800,600,96",
    }
    url = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/identify?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        for res in data.get("results", []):
            a = res.get("attributes", {}) or {}
            raw = a.get("DATE (YYYYMMDD)") or a.get("SRC_DATE")
            if raw and str(raw) not in ("Null", "None"):
                s = str(raw).strip()
                date = f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s
                return {
                    "date": date,
                    "resolution": a.get("RESOLUTION (M)"),
                    "source": a.get("SOURCE_INFO") or a.get("SOURCE") or a.get("DESCRIPTION"),
                }
    except Exception:  # noqa: BLE001
        pass
    return {"date": None, "resolution": None, "source": None}


@router.get("/imagery-date")
async def get_imagery_date(lng: float, lat: float, current_user: User = Depends(get_current_user)):
    """Satellite imagery capture date at a location (Esri World Imagery).

    Cached for 7 days on disk — imagery mosaics change slowly and the panel
    opens this for every viewed shipyard.
    """
    key = f"{round(lng, 3)},{round(lat, 3)}"
    cache = _read_imagery_cache()
    hit = cache.get(key)
    # Positive results cache for 7 days; negative results (transient Esri
    # failures / throttling) only 15 minutes so they self-heal quickly.
    ttl = _IMAGERY_CACHE_TTL if (hit or {}).get("date") else 900
    if hit and _time.time() - hit.get("fetched_at", 0) < ttl:
        return {"cached": True, **{k: v for k, v in hit.items() if k != "fetched_at"}}
    result = await asyncio.to_thread(_query_esri_imagery_date, lng, lat)
    cache[key] = {**result, "fetched_at": _time.time()}
    try:
        with open(_IMAGERY_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass
    return {"cached": False, **result}


async def get_current_manager_or_above(
    current_user: User = Depends(get_current_user),
) -> User:
    """Require admin or core admin."""
    if not _is_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user


def _get_data_path() -> str:
    """Return absolute path to shipyards_data.json in backend/data."""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(project_root, "data", "shipyards_data.json")


def _load_shiptype_alias_map(cursor) -> dict:
    """English/alias ship-type name -> Chinese canonical name, from the vocabulary table."""
    alias_map: dict[str, str] = {}
    cursor.execute("SELECT canonical_name, aliases FROM normalized_shiptype_vocabulary")
    for r in cursor.fetchall():
        canon = (r.get("canonical_name") or "").strip()
        if not canon:
            continue
        alias_map[_norm_name(canon)] = canon
        for a in _parse_json_safe(r.get("aliases")) or []:
            if isinstance(a, str) and a.strip():
                alias_map[_norm_name(a)] = canon
    return alias_map


def _enrich_recent_main_types(rows: list) -> None:
    """Attach recent_main_types (top-3 ship types by order quantity within 1 year,
    Chinese canonical names) to each shipyard row, keyed by row id.

    Source: Time_Series.shipyard_data.newbuilding_transactions (JSON per yard),
    English short type names mapped to Chinese via normalized_shiptype_vocabulary.
    Rows without recent orders get an empty list.
    """
    alias_map: dict[str, str] = {}
    yard_txs: list[dict] = []
    try:
        conn = mysql.connector.connect(**settings.NEWS_DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        try:
            alias_map = _load_shiptype_alias_map(cursor)
            cursor.execute(
                "SELECT id, newbuilding_transactions FROM shipyard_data "
                "WHERE newbuilding_transactions IS NOT NULL AND newbuilding_transactions NOT IN ('[]', '')"
            )
            yard_txs = cursor.fetchall()
        finally:
            cursor.close()
            conn.close()
    except Exception:
        return

    cutoff = datetime.now().date() - timedelta(days=365)
    by_id: dict[int, list] = {}
    for row in yard_txs:
        txs = _parse_json_safe(row.get("newbuilding_transactions"))
        if not isinstance(txs, list):
            continue
        counts: dict[str, int] = {}
        for t in txs:
            if not isinstance(t, dict):
                continue
            raw_date = str(t.get("date") or "")[:10]
            try:
                d = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                continue
            if d < cutoff:
                continue
            m = re.match(r"\d+", str(t.get("quantity") or "").strip())
            qty = int(m.group()) if m else 1
            if qty <= 0:
                qty = 1
            st_raw = str(t.get("ship_type") or "").strip()
            st = _norm_name(st_raw)
            canon = alias_map.get(st) if st else None
            label = canon or st_raw or "其他"
            counts[label] = counts.get(label, 0) + qty
        if counts:
            top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
            by_id[row["id"]] = [{"type": k, "count": v} for k, v in top]

    for r in rows:
        try:
            rid = int(r.get("id"))
        except (TypeError, ValueError):
            rid = None
        r["recent_main_types"] = by_id.get(rid, [])


@router.get("")
async def get_shipyards(current_user: User = Depends(get_current_user)):
    """Return shipyard map data (JWT protected)."""
    data_path = _get_data_path()
    if not os.path.exists(data_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shipyard data not found",
        )
    try:
        with open(data_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _enrich_recent_main_types(data)
        return {"success": True, "data": data, "count": len(data)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load shipyard data: {e}",
        )


def _get_map_config_path() -> str:
    """Return absolute path to shipyard_map_config.json in backend/data."""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(project_root, "data", "shipyard_map_config.json")


def _read_map_config() -> dict:
    path = _get_map_config_path()
    if not os.path.exists(path):
        return {"hidden_ids": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg.get("hidden_ids"), list):
            cfg["hidden_ids"] = []
        return cfg
    except Exception:
        return {"hidden_ids": []}


def _write_map_config(cfg: dict) -> None:
    path = _get_map_config_path()
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        shutil.move(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


class MapConfigUpdate(BaseModel):
    hidden_ids: list[int] = Field(default_factory=list)


@router.get("/map-config")
async def get_map_config(current_user: User = Depends(get_current_user)):
    """Shipyard map visibility config (hidden ids). Readable by any logged-in user.

    no-store: every viewer must see the latest saved selection immediately.
    """
    cfg = _read_map_config()
    return JSONResponse(
        {
            "success": True,
            "hidden_ids": cfg.get("hidden_ids", []),
            "updated_by": cfg.get("updated_by"),
            "updated_at": cfg.get("updated_at"),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.put("/map-config")
async def put_map_config(
    payload: MapConfigUpdate,
    current_user: User = Depends(get_current_core),
):
    """Update shipyard map visibility config. Core admin only."""
    data_path = _get_data_path()
    known_ids = set()
    if os.path.exists(data_path):
        try:
            with open(data_path, "r", encoding="utf-8") as f:
                known_ids = {x["id"] for x in json.load(f)}
        except Exception:
            known_ids = set()
    hidden = sorted({int(i) for i in payload.hidden_ids})
    skipped = [i for i in hidden if known_ids and i not in known_ids]
    if known_ids:
        hidden = [i for i in hidden if i in known_ids]
    cfg = _read_map_config()
    cfg["hidden_ids"] = hidden
    cfg["updated_by"] = current_user.username
    cfg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_map_config(cfg)
    return {
        "success": True,
        "hidden_ids": hidden,
        "skipped_ids": skipped,
        "updated_by": cfg["updated_by"],
        "updated_at": cfg["updated_at"],
    }


@router.post("/update")
async def update_shipyards(current_user: User = Depends(get_current_user)):
    """Synchronize shipyard map JSON with DB (JWT protected).

    Compares database records with backend/data/shipyards_data.json:
    - Adds shipyards present in DB but missing from JSON.
    - Removes shipyards present in JSON but missing from DB.
    - Updates changed fields for existing shipyards while preserving
      manually edited coordinates.
    """
    try:
        from app.scripts.fetch_shipyards import sync_shipyards
        shipyards, summary = sync_shipyards()
        return {
            "success": True,
            "message": "Shipyard data synchronized successfully",
            "summary": summary,
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update shipyard data: {e}",
        )


@router.get("/raw")
async def get_shipyards_raw(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return raw shipyard data from DB for display (authenticated users)."""
    try:
        from app.db.database import async_session
        import mysql.connector

        DB_CONFIG = {
            'host': 'your-db-host.example.com',
            'user': 'your-db-user',
            'password': 'your-db-password',
            'database': 'Time_Series',
            'port': 3306
        }
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM shipyard_data WHERE location IS NOT NULL AND location != ''")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        # Convert timestamps and filter excluded columns
        clean_rows = []
        for row in rows:
            clean = {}
            for k, v in row.items():
                if k in DISPLAY_EXCLUDED_COLS:
                    continue
                # JSON serialize complex types
                if k == "production_lines" and v is not None:
                    if isinstance(v, (dict, list)):
                        clean[k] = v
                    else:
                        try:
                            clean[k] = json.loads(v)
                        except Exception:
                            clean[k] = v
                elif k == "annual_production_capacity" and v is not None:
                    if isinstance(v, (dict, list)):
                        clean[k] = v
                    else:
                        try:
                            clean[k] = json.loads(v)
                        except Exception:
                            clean[k] = v
                elif k in ("shipbuilding_transactions", "newbuilding_transactions") and v is not None:
                    if isinstance(v, (dict, list)):
                        clean[k] = v
                    else:
                        try:
                            clean[k] = json.loads(v)
                        except Exception:
                            clean[k] = v
                elif k == "branch_base" and v is not None:
                    if isinstance(v, (dict, list)):
                        clean[k] = v
                    else:
                        try:
                            clean[k] = json.loads(v)
                        except Exception:
                            clean[k] = v
                elif k == "create_info" and v is not None:
                    try:
                        clean[k] = json.loads(v) if isinstance(v, str) else v
                    except Exception:
                        clean[k] = v
                elif k in ("establishment_date", "processed_at", "update_time") and v is not None:
                    clean[k] = str(v) if v else None
                elif isinstance(v, bytes):
                    clean[k] = v.decode("utf-8", errors="ignore")
                else:
                    clean[k] = v
            clean_rows.append(clean)

        # Enrich each row with the latest create_info entry (fallback for legacy data)
        for clean in clean_rows:
            create_info = clean.get("create_info")
            latest = None
            if isinstance(create_info, list) and create_info:
                latest = create_info[-1]
            if isinstance(latest, dict):
                person = latest.get("update_person") or latest.get("account_name") or latest.get("account_id")
                time_val = latest.get("update_time") or latest.get("modify_time")
                if not clean.get("update_person") and person:
                    clean["update_person"] = person
                if not clean.get("update_time") and time_val:
                    clean["update_time"] = time_val

        return {"success": True, "data": clean_rows, "count": len(clean_rows)}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load raw shipyard data: {e}",
        )


class _ShipyardExportRequest(BaseModel):
    format: str = Field(..., pattern="^(excel|word)$", description="Export format: excel or word")
    records: _List[dict] = Field(..., min_length=1, description="Records to export")


class _CheckNameRequest(BaseModel):
    local_name: str = Field(..., min_length=1, description="Shipyard local/Chinese name")
    english_name: str | None = Field(default=None, description="Shipyard English name (optional)")


class _Crane(BaseModel):
    no: int = Field(default=1, description="Crane sequence number")
    type: str = Field(default="crane", description="Crane type")
    capacity: float | int | None = Field(default=None, description="Crane capacity in tons")


class _DockGeometry(BaseModel):
    lineNo: int = Field(..., description="Production line number")
    boundary: list[list[float]] | None = Field(default=None, description="Dock boundary ring [[lng,lat],...]; null removes the boundary")
    type: str | None = Field(default="dry dock", description="Dock type for newly created lines: 'dry dock' | 'berth' | ...")
    lengthM: float | int | None = Field(default=None, description="Dock length in meters")
    widthM: float | int | None = Field(default=None, description="Dock width in meters")
    cranes: list[_Crane] | None = Field(default=None, description="List of cranes serving this dock")


class _DecimalEncoder(json.JSONEncoder):
    """Serialize Decimal values (from MySQL rows) as floats for JSON storage."""

    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


class _BranchGeometry(BaseModel):
    """Branch base (分基地): center, boundary and dock geometry, stored like the main base."""

    coord: list[float] | None = Field(default=None, description="Branch base center [lng, lat]")
    boundary: list[list[list[float]]] | None = Field(default=None, description="Branch base boundary polygon; empty/null deletes it")
    docks: list[_DockGeometry] | None = Field(default=None, description="Branch dock boundaries [{lineNo, boundary, type, lengthM, widthM}]")
    deletedDockLineNos: list[int] | None = Field(default=None, description="Branch dock line numbers to remove entirely")


class _GeometryRequest(BaseModel):
    coord: list[float] | None = Field(default=None, description="Shipyard center [lng, lat]")
    boundary: list[list[list[float]]] | None = Field(default=None, description="Shipyard boundary polygon [ring of [lng,lat]]; empty/null deletes it")
    docks: list[_DockGeometry] | None = Field(default=None, description="Dock boundaries [{lineNo, boundary, type, lengthM, widthM}]")
    deletedDockLineNos: list[int] | None = Field(default=None, description="Production line numbers to remove entirely")
    hasBranch: str | None = Field(default=None, description="Whether the yard has a branch base: 'Y' | 'N'")
    branch: _BranchGeometry | None = Field(default=None, description="Branch base geometry (coord/boundary/docks)")


DB_CONFIG = {
    'host': 'your-db-host.example.com',
    'user': 'your-db-user',
    'password': 'your-db-password',
    'database': 'Time_Series',
    'port': 3306
}


def _sanitize_filename(name: str) -> str:
    """Replace filesystem-unsafe characters with underscores."""
    sanitized = re.sub(r'[\\/:*?"<>|\s]+', "_", name)
    return sanitized.strip("_") or "船厂"


@router.post("/export")
async def export_shipyards(
    request: _ShipyardExportRequest,
    background_tasks: BackgroundTasks,
    manager: User = Depends(get_current_manager_or_above),
):
    """Export selected shipyard records to Excel or Word as a downloadable file."""
    records = request.records
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    count = len(records)

    if count == 1:
        name = str(records[0].get("Local_name") or records[0].get("English_name") or "船厂")
        name = _sanitize_filename(name)[:30]
        suffix = f"{name}_{timestamp}"
    else:
        suffix = f"导出_{count}条_{timestamp}"

    if request.format == "excel":
        filename = f"船厂数据_{suffix}.xlsx"
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        filename = f"船厂数据_{suffix}.docx"
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    tmpdir = tempfile.mkdtemp()
    output_path = os.path.join(tmpdir, filename)

    if request.format == "excel":
        export_to_excel(records, output_path)
    else:
        export_to_word(records, output_path)

    # Clean up the temporary directory after the response is sent.
    background_tasks.add_task(shutil.rmtree, tmpdir, True)

    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    return FileResponse(
        output_path,
        headers=headers,
        media_type=media_type,
        filename=filename,
    )


@router.post("/{shipyard_id}/geometry")
async def save_shipyard_geometry(
    shipyard_id: int,
    request: _GeometryRequest,
    manager: User = Depends(get_current_manager_or_above),
):
    """Save shipyard center coordinates, yard boundary, and dock boundaries.
    Coordinates and yard boundary are stored in shipyard_data (lat, lng, boundary_geo).
    Dock boundaries are attached to the matching production_lines entry by line no.
    The map JSON file is updated in place to keep reads consistent.
    """
    import mysql.connector

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    coord = request.coord
    boundary = request.boundary
    docks = request.docks or []

    # Validate basic shape
    if coord is not None and len(coord) != 2:
        raise HTTPException(status_code=400, detail="coord 必须是 [lng, lat]")
    if boundary is not None and boundary and (not boundary[0] or len(boundary[0]) < 4):
        raise HTTPException(status_code=400, detail="boundary 必须包含至少 4 个顶点")

    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM shipyard_data WHERE id = %s", (shipyard_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="船厂不存在")

        # Build updates for shipyard_data
        fields = []
        values = []
        changes = []

        if coord is not None:
            lng, lat = float(coord[0]), float(coord[1])
            fields.append("`lat` = %s")
            values.append(lat)
            fields.append("`lng` = %s")
            values.append(lng)
            changes.append({"field": "lat/lng", "old": [row.get("lng"), row.get("lat")], "new": [lng, lat]})

        if boundary is not None:
            if boundary and boundary[0] and len(boundary[0]) >= 4:
                fields.append("`boundary_geo` = %s")
                values.append(json.dumps(boundary, ensure_ascii=False))
                changes.append({"field": "boundary_geo", "old": row.get("boundary_geo"), "new": boundary})
            else:
                fields.append("`boundary_geo` = NULL")
                changes.append({"field": "boundary_geo", "old": row.get("boundary_geo"), "new": None})

        # Branch base flag (分基地 Y/N)
        if request.hasBranch is not None:
            hb = "Y" if str(request.hasBranch).upper().startswith("Y") else "N"
            fields.append("`has_branch` = %s")
            values.append(hb)
            changes.append({"field": "has_branch", "old": row.get("has_branch"), "new": hb})

        # Branch base geometry (分基地坐标/范围/船坞), stored like the main base
        branch = request.branch
        if branch is not None:
            branch_base = row.get("branch_base")
            if isinstance(branch_base, str):
                try:
                    branch_base = json.loads(branch_base)
                except Exception:
                    branch_base = None
            if not isinstance(branch_base, dict):
                branch_base = {}

            if branch.coord is not None:
                if len(branch.coord) != 2:
                    raise HTTPException(status_code=400, detail="branch.coord 必须是 [lng, lat]")
                branch_base["coord"] = [float(branch.coord[0]), float(branch.coord[1])]

            if branch.boundary is not None:
                if branch.boundary and branch.boundary[0] and len(branch.boundary[0]) >= 4:
                    branch_base["boundary"] = branch.boundary
                else:
                    branch_base.pop("boundary", None)

            branch_docks = branch_base.get("docks")
            if not isinstance(branch_docks, list):
                branch_docks = []

            branch_deleted = branch.deletedDockLineNos or []
            for line_no in branch_deleted:
                line = next((l for l in branch_docks if isinstance(l, dict) and l.get("no") == line_no), None)
                if line:
                    changes.append({"field": "branch_base.docks", "old": line, "new": None})
            branch_docks = [l for l in branch_docks if not (isinstance(l, dict) and l.get("no") in branch_deleted)]

            if branch.docks:
                for dock in branch.docks:
                    line_no = dock.lineNo
                    line = next((l for l in branch_docks if isinstance(l, dict) and l.get("no") == line_no), None)
                    if line:
                        if dock.boundary is not None:
                            line["boundary"] = dock.boundary
                            if dock.lengthM is not None and dock.widthM is not None:
                                line["dimensions"] = f"{dock.lengthM}×{dock.widthM}"
                        if dock.cranes is not None:
                            line["cranes"] = [c.model_dump(mode="json") for c in dock.cranes]
                        if dock.type is not None:
                            line["type"] = dock.type
                    else:
                        if dock.boundary is not None:
                            dimensions = ""
                            if dock.lengthM is not None and dock.widthM is not None:
                                dimensions = f"{dock.lengthM}×{dock.widthM}"
                            branch_docks.append({
                                "no": line_no,
                                "type": dock.type or "dry dock",
                                "dimensions": dimensions,
                                "cranes": [c.model_dump(mode="json") for c in (dock.cranes or [])],
                                "boundary": dock.boundary,
                            })
            if branch_docks or branch_base.get("coord") or branch_base.get("boundary"):
                branch_base["docks"] = branch_docks
            fields.append("`branch_base` = %s")
            values.append(json.dumps(branch_base, ensure_ascii=False))
            changes.append({"field": "branch_base", "old": row.get("branch_base"), "new": branch_base})

        # Update production_lines with dock boundaries
        production_lines = row.get("production_lines")
        if isinstance(production_lines, str):
            try:
                production_lines = json.loads(production_lines)
            except Exception:
                production_lines = []
        if not isinstance(production_lines, list):
            production_lines = []

        dock_changes = []

        # Remove explicitly deleted dock lines entirely.
        deleted_line_nos = request.deletedDockLineNos or []
        for line_no in deleted_line_nos:
            line = next((l for l in production_lines if isinstance(l, dict) and l.get("no") == line_no), None)
            if line:
                dock_changes.append({"lineNo": line_no, "old": line, "new": None})
        production_lines = [l for l in production_lines if not (isinstance(l, dict) and l.get("no") in deleted_line_nos)]

        if docks:
            for dock in docks:
                line_no = dock.lineNo
                line = next((l for l in production_lines if isinstance(l, dict) and l.get("no") == line_no), None)
                if line:
                    old = line.get("boundary")
                    updated = False
                    if dock.boundary is not None:
                        line["boundary"] = dock.boundary
                        if dock.lengthM is not None and dock.widthM is not None:
                            line["dimensions"] = f"{dock.lengthM}×{dock.widthM}"
                        updated = True
                    if dock.cranes is not None:
                        line["cranes"] = [c.model_dump(mode="json") for c in dock.cranes]
                        updated = True
                    if dock.type is not None:
                        line["type"] = dock.type
                        updated = True
                    if updated:
                        dock_changes.append({"lineNo": line_no, "old": old, "new": dock.boundary})
                else:
                    if dock.boundary is not None:
                        dimensions = ""
                        if dock.lengthM is not None and dock.widthM is not None:
                            dimensions = f"{dock.lengthM}×{dock.widthM}"
                        production_lines.append({
                            "no": line_no,
                            "type": dock.type or "dry dock",
                            "dimensions": dimensions,
                            "cranes": [c.model_dump(mode="json") for c in (dock.cranes or [])],
                            "boundary": dock.boundary,
                        })
                        dock_changes.append({"lineNo": line_no, "old": None, "new": dock.boundary})

        if dock_changes:
            fields.append("`production_lines` = %s")
            values.append(json.dumps(production_lines, ensure_ascii=False))
            changes.append({"field": "production_lines.boundary", "old": None, "new": dock_changes})

        if fields:
            fields.append("`update_time` = %s")
            values.append(now)
            fields.append("`update_person` = %s")
            values.append(manager.username)
            values.append(shipyard_id)
            sql = f"UPDATE shipyard_data SET {', '.join(fields)} WHERE id = %s"
            cursor.execute(sql, values)

        if changes:
            cursor.execute(
                "INSERT INTO shipyard_update_history (shipyard_id, update_time, update_person, changes) VALUES (%s, %s, %s, %s)",
                (shipyard_id, now, manager.username, json.dumps(changes, ensure_ascii=False, cls=_DecimalEncoder)),
            )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    # Update the JSON map file in place
    data_path = _get_data_path()
    updated_json = False
    if os.path.exists(data_path):
        try:
            with open(data_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            target = next((s for s in data if s.get("id") == shipyard_id), None)
            if target:
                if coord is not None:
                    target["lat"] = float(coord[1])
                    target["lng"] = float(coord[0])
                    target["geo_level"] = "manual"
                if boundary is not None:
                    if boundary and boundary[0] and len(boundary[0]) >= 4:
                        target["boundary"] = boundary
                    else:
                        target.pop("boundary", None)
                if request.hasBranch is not None:
                    target["has_branch"] = "Y" if str(request.hasBranch).upper().startswith("Y") else "N"
                if branch is not None:
                    target_branch = target.get("branch")
                    if not isinstance(target_branch, dict):
                        target_branch = {}
                    if branch.coord is not None:
                        target_branch["coord"] = [float(branch.coord[0]), float(branch.coord[1])]
                    if branch.boundary is not None:
                        if branch.boundary and branch.boundary[0] and len(branch.boundary[0]) >= 4:
                            target_branch["boundary"] = branch.boundary
                        else:
                            target_branch.pop("boundary", None)
                    branch_lines = target_branch.get("docks")
                    if not isinstance(branch_lines, list):
                        branch_lines = []
                    for line_no in branch.deletedDockLineNos or []:
                        branch_lines = [l for l in branch_lines if not (isinstance(l, dict) and l.get("no") == line_no)]
                    if branch.docks:
                        for dock in branch.docks:
                            line_no = dock.lineNo
                            line = next((l for l in branch_lines if isinstance(l, dict) and l.get("no") == line_no), None)
                            if line:
                                if dock.boundary is not None:
                                    line["boundary"] = dock.boundary
                                    if dock.lengthM is not None and dock.widthM is not None:
                                        line["dimensions"] = f"{dock.lengthM}×{dock.widthM}"
                                if dock.cranes is not None:
                                    line["cranes"] = [c.model_dump(mode="json") for c in dock.cranes]
                                if dock.type is not None:
                                    line["type"] = dock.type
                            else:
                                if dock.boundary is not None:
                                    dimensions = ""
                                    if dock.lengthM is not None and dock.widthM is not None:
                                        dimensions = f"{dock.lengthM}×{dock.widthM}"
                                    branch_lines.append({
                                        "no": line_no,
                                        "type": dock.type or "dry dock",
                                        "dimensions": dimensions,
                                        "cranes": [c.model_dump(mode="json") for c in (dock.cranes or [])],
                                        "boundary": dock.boundary,
                                    })
                    target_branch["docks"] = branch_lines
                    target["branch"] = target_branch
                lines = target.get("lines") or []
                if deleted_line_nos:
                    lines = [l for l in lines if not (isinstance(l, dict) and l.get("no") in deleted_line_nos)]
                if docks:
                    for dock in docks:
                        line_no = dock.lineNo
                        line = next((l for l in lines if isinstance(l, dict) and l.get("no") == line_no), None)
                        if line:
                            if dock.boundary is not None:
                                line["boundary"] = dock.boundary
                                if dock.lengthM is not None and dock.widthM is not None:
                                    line["dimensions"] = f"{dock.lengthM}×{dock.widthM}"
                            if dock.cranes is not None:
                                line["cranes"] = [c.model_dump(mode="json") for c in dock.cranes]
                            if dock.type is not None:
                                line["type"] = dock.type
                        else:
                            if dock.boundary is not None:
                                dimensions = ""
                                if dock.lengthM is not None and dock.widthM is not None:
                                    dimensions = f"{dock.lengthM}×{dock.widthM}"
                                lines.append({
                                    "no": line_no,
                                    "type": dock.type or "dry dock",
                                    "dimensions": dimensions,
                                    "cranes": [c.model_dump(mode="json") for c in (dock.cranes or [])],
                                    "boundary": dock.boundary,
                                })
                if deleted_line_nos or docks:
                    target["lines"] = lines
                with open(data_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                updated_json = True
        except Exception as e:
            # JSON sync failure should not fail the API, but log it
            print(f"[WARN] 几何保存后更新 JSON 失败: {e}")

    return {
        "success": True,
        "shipyard_id": shipyard_id,
        "updated_db": bool(fields),
        "updated_json": updated_json,
        "message": "几何数据已保存",
    }


@router.get("/{shipyard_id}")
async def get_shipyard_by_id(
    shipyard_id: int,
    current_user: User = Depends(get_current_user),
):
    """Return single shipyard map data by id."""
    data_path = _get_data_path()
    if not os.path.exists(data_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shipyard data not found",
        )
    try:
        with open(data_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        shipyard = next((s for s in data if s.get("id") == shipyard_id), None)
        if not shipyard:
            raise HTTPException(status_code=404, detail="Shipyard not found")
        return {"success": True, "data": shipyard}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load shipyard: {e}",
        )


@router.post("/check-name")
async def check_shipyard_name(
    request: _CheckNameRequest,
    manager: User = Depends(get_current_manager_or_above),
):
    """Check whether a shipyard name is similar to an existing registry/DB entry."""
    local_name = request.local_name.strip()
    english_name = (request.english_name or "").strip() or None

    entities = load_shipyard_entities()

    # 1. AI matching.
    canonical_name = ai_match_shipyard_name(local_name, english_name, entities)
    matched_entity = None
    if canonical_name:
        for entity in entities:
            if entity.get("canonical_name") == canonical_name:
                matched_entity = entity
                break

    # 2. Direct string matching fallback.
    if matched_entity is None:
        direct_name = find_matching_entity(local_name, entities)
        if not direct_name and english_name:
            direct_name = find_matching_entity(english_name, entities)
        if direct_name:
            canonical_name = direct_name
            for entity in entities:
                if entity.get("canonical_name") == direct_name:
                    matched_entity = entity
                    break

    base_response = {
        "input_local_name": local_name,
        "input_english_name": english_name,
    }

    # Helper to return a consistent similar response with the matched DB id.
    def _similar_response(name_record: dict):
        return {
            **base_response,
            "similar": True,
            "canonical_name": name_record.get("Local_name"),
            "english_name": name_record.get("English_name"),
            "abbreviation": name_record.get("Abreviation"),
            "existing_id": name_record.get("id"),
        }

    if matched_entity is not None:
        # Try to find the canonical record in MySQL to get its primary id.
        db_record = find_db_match(canonical_name) if canonical_name else None
        if db_record is None and english_name:
            db_record = find_db_match(english_name)
        if db_record is not None:
            return _similar_response(db_record)
        # Fall back to entity-only information.
        _, english, abbr = _extract_names_from_entity(matched_entity)
        return {
            **base_response,
            "similar": True,
            "canonical_name": canonical_name,
            "english_name": english,
            "abbreviation": abbr,
            "existing_id": None,
        }

    # 3. DB exact/LIKE fallback.
    db_record = find_db_match(local_name)
    if db_record is None and english_name:
        db_record = find_db_match(english_name)

    if db_record:
        return _similar_response(db_record)

    return {
        **base_response,
        "similar": False,
        "canonical_name": None,
        "english_name": None,
        "abbreviation": None,
        "existing_id": None,
    }


@router.post("/import")
async def import_shipyards(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    manager: User = Depends(get_current_manager_or_above),
):
    """Batch update shipyard raw data in DB (admin only).
    payload: { data: [ { id, field1, field2, ... }, ... ] }
    """
    rows = payload.get("data", [])
    if not isinstance(rows, list) or len(rows) == 0:
        raise HTTPException(status_code=400, detail="数据不能为空")
    if len(rows) > 500:
        raise HTTPException(status_code=400, detail="单次最多导入 500 条")

    try:
        result = import_shipyard_rows(rows, manager_username=manager.username)
        if not result.get("success"):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=result.get("message", "导入失败"),
            )
        # Keep the map JSON in sync with the DB so GET /shipyards reflects edits immediately
        try:
            from app.scripts.fetch_shipyards import sync_shipyards
            sync_shipyards()
        except Exception as sync_err:
            # JSON sync failure should not fail the import, but log it
            print(f"[WARN] 导入后同步 JSON 失败: {sync_err}")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"导入失败: {e}",
        )


@router.delete("/{shipyard_id}")
async def delete_shipyard(
    shipyard_id: int,
    manager: User = Depends(get_current_manager_or_above),
):
    """Delete a shipyard from DB and synchronize the map JSON (manager+ only)."""
    DB_CONFIG = {
        'host': 'your-db-host.example.com',
        'user': 'your-db-user',
        'password': 'your-db-password',
        'database': 'Time_Series',
        'port': 3306
    }
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, Local_name FROM shipyard_data WHERE id = %s", (shipyard_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="船厂不存在")

        # Remove related history first to keep referential cleanliness.
        cursor.execute("DELETE FROM shipyard_update_history WHERE shipyard_id = %s", (shipyard_id,))
        # Remove the shipyard itself.
        cursor.execute("DELETE FROM shipyard_data WHERE id = %s", (shipyard_id,))
        conn.commit()
        deleted = cursor.rowcount

        # Keep the map JSON in sync with the DB.
        try:
            from app.scripts.fetch_shipyards import sync_shipyards
            sync_shipyards()
        except Exception as sync_err:
            print(f"[WARN] 删除船厂后同步 JSON 失败: {sync_err}")

        return {
            "success": True,
            "deleted": deleted,
            "message": f"已删除船厂 [{row.get('Local_name') or shipyard_id}]",
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"删除船厂失败: {e}",
        )
    finally:
        cursor.close()
        conn.close()


@router.get("/{shipyard_id}/history")
async def get_shipyard_history(
    shipyard_id: int,
    current_user: User = Depends(get_current_user),
):
    """获取某船库的字段级修改历史（管理员可查看）。"""
    if not _is_admin(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")

    DB_CONFIG = {
        'host': 'your-db-host.example.com',
        'user': 'your-db-user',
        'password': 'your-db-password',
        'database': 'Time_Series',
        'port': 3306
    }
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT id, shipyard_id, update_time, update_person, changes
            FROM shipyard_update_history
            WHERE shipyard_id = %s
            ORDER BY update_time ASC, id ASC
            """,
            (shipyard_id,),
        )
        rows = cursor.fetchall()
        for r in rows:
            raw = r.get("changes")
            try:
                r["changes"] = json.loads(raw) if isinstance(raw, str) else (raw if isinstance(raw, list) else [])
            except Exception:
                r["changes"] = []
        return {"success": True, "data": rows}
    finally:
        cursor.close()
        conn.close()


@router.delete("/history/{history_id}")
async def delete_shipyard_history(
    history_id: int,
    current_user: User = Depends(get_current_manager_or_above),
):
    """删除某条修改历史（仅核心管理员）。"""
    if not _is_core(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅最高级管理员可删除修改记录")

    DB_CONFIG = {
        'host': 'your-db-host.example.com',
        'user': 'your-db-user',
        'password': 'your-db-password',
        'database': 'Time_Series',
        'port': 3306
    }
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM shipyard_update_history WHERE id = %s", (history_id,))
        conn.commit()
        return {"success": True, "deleted": cursor.rowcount}
    finally:
        cursor.close()
        conn.close()


@router.post("/coordinates")
async def update_coordinates(
    payload: dict,
    manager: User = Depends(get_current_manager_or_above),
):
    """Batch upsert shipyard coordinates in map JSON (admin only).
    payload: { data: [ { id, lat, lng, name, location, ... }, ... ] }
    For each row: update by id if found; otherwise match by name; otherwise
    append a new entry to the JSON file. This ensures manually edited
    coordinates are persisted even for shipyards not yet present in the map.
    """
    rows = payload.get("data", [])
    if not isinstance(rows, list) or len(rows) == 0:
        raise HTTPException(status_code=400, detail="数据不能为空")

    data_path = _get_data_path()
    if not os.path.exists(data_path):
        raise HTTPException(status_code=404, detail="地图数据未找到")

    try:
        with open(data_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        updated = 0
        added = 0
        id_map = {s["id"]: s for s in data}
        name_map = {s.get("name"): s for s in data if s.get("name")}

        for row in rows:
            sid = row.get("id")
            lat = row.get("lat")
            lng = row.get("lng")
            if lat is None or lng is None:
                continue
            try:
                lat = float(lat)
                lng = float(lng)
            except (ValueError, TypeError):
                continue

            target = None
            if sid is not None and sid in id_map:
                target = id_map[sid]
            else:
                name = row.get("name")
                if name and name in name_map:
                    target = name_map[name]

            if target is not None:
                target["lat"] = lat
                target["lng"] = lng
                updated += 1
            else:
                # Build a minimal new entry for the map JSON
                lines = row.get("lines") or row.get("production_lines") or []
                if isinstance(lines, str):
                    try:
                        lines = json.loads(lines)
                    except Exception:
                        lines = []
                new_entry = {
                    "id": sid,
                    "name": row.get("name") or "",
                    "lat": lat,
                    "lng": lng,
                    "location": row.get("location") or "",
                    "refund": row.get("refund") or row.get("refund_guarantee_capacity") or "未知",
                    "update_time": row.get("update_time") or "",
                    "person_in_charge": row.get("person_in_charge") or "",
                    "lines": lines,
                    "geo_level": "manual",
                    "is_active": row.get("is_active") or "Y",
                    "shipyard_type": row.get("shipyard_type") or "",
                }
                data.append(new_entry)
                # Keep maps in sync in case later rows reference the same name
                if sid is not None:
                    id_map[sid] = new_entry
                if new_entry.get("name"):
                    name_map[new_entry["name"]] = new_entry
                added += 1

        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        message_parts = []
        if updated:
            message_parts.append(f"成功更新 {updated} 个坐标")
        if added:
            message_parts.append(f"新增 {added} 个船厂")

        return {
            "success": True,
            "updated": updated,
            "added": added,
            "message": "；".join(message_parts) or "坐标保存完成",
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"坐标更新失败: {e}",
        )



# ---------- shipyard-specific news from Time_Series.ship_news ----------

def _norm_name(s: str | None) -> str:
    return re.sub(r"\s+", "", (s or "").lower()) if s else ""


def _parse_json_safe(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _extract_matching_transactions(data_text, candidates):
    """Return transactions whose shipyard field matches any candidate name."""
    outer = _parse_json_safe(data_text)
    if not isinstance(outer, list):
        return []
    matched = []
    candidates = [c for c in candidates if c and len(c.strip()) > 1]
    for item in outer:
        if not isinstance(item, dict):
            continue
        inner_text = item.get("data")
        inner = _parse_json_safe(inner_text)
        if not isinstance(inner, list):
            continue
        for group in inner:
            if not isinstance(group, dict):
                continue
            transactions = group.get("transactions")
            if isinstance(transactions, list):
                for tx in transactions:
                    if not isinstance(tx, dict):
                        continue
                    shipyard = str(tx.get("shipyard") or "")
                    if any(c in shipyard or shipyard in c for c in candidates):
                        matched.append(tx)
            elif transactions is None:
                shipyard = str(group.get("shipyard") or "")
                if any(c in shipyard or shipyard in c for c in candidates):
                    matched.append(group)
    return matched


@router.get("/{shipyard_id}/orders")
async def get_shipyard_orders(
    shipyard_id: int,
    current_user: User = Depends(get_current_user),
):
    """船厂在手订单，直连数据库实时读取（shipyard_data.newbuilding_transactions）。

    每次调用现场查询 MySQL，数据库更新后接口数据立即同步；船型英文名
    映射为中文规范名（normalized_shiptype_vocabulary）。
    """
    conn = mysql.connector.connect(**settings.NEWS_DB_CONFIG)
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, Local_name, newbuilding_transactions FROM shipyard_data WHERE id = %s",
            (shipyard_id,),
        )
        row = cursor.fetchone()
        alias_map = _load_shiptype_alias_map(cursor)
    finally:
        cursor.close()
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="船厂不存在")

    txs = _parse_json_safe(row.get("newbuilding_transactions")) or []
    orders = []
    for t in txs:
        if not isinstance(t, dict):
            continue
        st_raw = str(t.get("ship_type") or "").strip()
        orders.append({
            "date": str(t.get("date") or "")[:10],
            "trade_type": str(t.get("trade_type") or "").strip(),
            "shipowner": str(t.get("shipowner") or "").strip(),
            "ship_type": alias_map.get(_norm_name(st_raw), st_raw) if st_raw else "",
            "raw_type": st_raw,
            "specs": str(t.get("specs") or "").strip(),
            "quantity": str(t.get("quantity") or "").strip(),
            "price": str(t.get("price") or "").strip(),
            "delivery": str(t.get("delivery") or "").strip(),
            "dwt": str(t.get("dwt") or "").strip(),
            "gt": str(t.get("gt") or "").strip(),
            "hull_no": str(t.get("hull_no") or "").strip(),
        })
    orders.sort(key=lambda o: o["date"], reverse=True)

    return {
        "success": True,
        "shipyard_id": shipyard_id,
        "name": row.get("Local_name"),
        "count": len(orders),
        "data": orders,
    }


@router.get("/{shipyard_id}/build")
async def get_shipyard_build(
    shipyard_id: int,
    current_user: User = Depends(get_current_user),
):
    """船厂历史建造记录（clarkson_fleet 经 pmc_builder_map 对齐）。

    返回：建造总量、2015 年起年度聚合（艘数/DWT）、最近 100 条建造明细。
    数据每次调用现场查询 MySQL，与 orderbook 同一套 builder 映射对齐。
    """
    conn = mysql.connector.connect(**settings.NEWS_DB_CONFIG)
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, Local_name FROM shipyard_data WHERE id = %s",
            (shipyard_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="船厂不存在")
        alias_map = _load_shiptype_alias_map(cursor)

        cursor.execute(
            """
            SELECT f.built AS year, COUNT(*) AS count, COALESCE(SUM(f.dwt), 0) AS dwt
            FROM clarkson_fleet f
            JOIN pmc_builder_map m ON f.builder COLLATE utf8mb4_unicode_ci = m.builder_raw
            WHERE m.shipyard_id = %s AND f.built >= 2015
            GROUP BY f.built ORDER BY f.built DESC
            """,
            (shipyard_id,),
        )
        yearly = [
            {"year": r["year"], "count": r["count"], "dwt": float(r["dwt"] or 0)}
            for r in cursor.fetchall()
        ]

        cursor.execute(
            """
            SELECT COUNT(*) AS count, COALESCE(SUM(f.dwt), 0) AS dwt
            FROM clarkson_fleet f
            JOIN pmc_builder_map m ON f.builder COLLATE utf8mb4_unicode_ci = m.builder_raw
            WHERE m.shipyard_id = %s
            """,
            (shipyard_id,),
        )
        totals = cursor.fetchone()

        cursor.execute(
            """
            SELECT f.id AS fleet_id, f.name, f.type, f.built, f.month, f.dwt, f.gt,
                   f.flag, f.status, f.operator, f.company, f.group_company,
                   f.alternative_fuel_types, f.sox_scrubber_status, f.eco_electronic_engine
            FROM clarkson_fleet f
            JOIN pmc_builder_map m ON f.builder COLLATE utf8mb4_unicode_ci = m.builder_raw
            WHERE m.shipyard_id = %s
            ORDER BY f.built DESC, (f.month IS NULL) ASC, f.month DESC, f.id DESC
            LIMIT 100
            """,
            (shipyard_id,),
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    items = []
    for r in rows:
        built = r.get("built")
        month = r.get("month")
        date = f"{built:04d}-{month:02d}" if built and month else (f"{built:04d}" if built else "")
        st_raw = str(r.get("type") or "").strip()
        power_parts = [
            p for p in (
                str(r.get("alternative_fuel_types") or "").strip(),
                str(r.get("sox_scrubber_status") or "").strip(),
                str(r.get("eco_electronic_engine") or "").strip(),
            ) if p and p.lower() not in ("none", "no", "-")
        ]
        items.append({
            "fleet_id": r.get("fleet_id"),
            "date": date,
            "vessel_name": str(r.get("name") or "").strip(),
            "ship_type": alias_map.get(_norm_name(st_raw), st_raw) if st_raw else "",
            "raw_type": st_raw,
            "dwt": str(r.get("dwt") or ""),
            "gt": str(r.get("gt") or ""),
            "flag": str(r.get("flag") or "").strip(),
            "status": str(r.get("status") or "").strip(),
            "shipowner": str(r.get("operator") or r.get("company") or r.get("group_company") or "").strip(),
            "power": " / ".join(power_parts),
        })

    return {
        "success": True,
        "shipyard_id": shipyard_id,
        "name": row.get("Local_name"),
        "count": int(totals["count"] or 0) if totals else 0,
        "total_dwt": float(totals["dwt"] or 0) if totals else 0.0,
        "yearly": yearly,
        "data": items,
    }


def _get_shipyard_name_candidates(local_name: str | None, english_name: str | None, former_name: str | None):
    """Build a broad name list from the vocabulary table for matching news transactions."""
    conn = mysql.connector.connect(**settings.NEWS_DB_CONFIG)
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT canonical_name, alias_name FROM normalized_shipyard_vocabulary")
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    local_norm = _norm_name(local_name)
    english_norm = _norm_name(english_name)
    former_norm = _norm_name(former_name)
    canonical = None
    for r in rows:
        c_norm = _norm_name(r.get("canonical_name"))
        a_norm = _norm_name(r.get("alias_name"))
        if local_norm and (c_norm == local_norm or a_norm == local_norm):
            canonical = r.get("canonical_name")
            break
        if english_norm and (c_norm == english_norm or a_norm == english_norm):
            canonical = r.get("canonical_name")
            break
        if former_norm and (c_norm == former_norm or a_norm == former_norm):
            canonical = r.get("canonical_name")
            break

    names = set()
    for n in (local_name, english_name, former_name):
        if n and n.strip():
            names.add(n.strip())

    if canonical:
        names.add(canonical)
        for r in rows:
            if r.get("canonical_name") == canonical:
                alias = r.get("alias_name")
                if alias and alias.strip():
                    names.add(alias.strip())

    return list(names)


@router.get("/{shipyard_id}/news")
async def get_shipyard_news(
    shipyard_id: int,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
):
    """Return news items from ship_news whose transactions reference this shipyard.

    Parses the JSON `data` column, looks inside transactions[].shipyard, and returns
    the matching transactions together with the news report and original link.
    """
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit 必须在 1-200 之间")

    conn = mysql.connector.connect(**settings.NEWS_DB_CONFIG)
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, Local_name, English_name, former_name FROM shipyard_data WHERE id = %s",
            (shipyard_id,),
        )
        yard = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if not yard:
        raise HTTPException(status_code=404, detail="船厂不存在")

    candidates = _get_shipyard_name_candidates(
        yard.get("Local_name"),
        yard.get("English_name"),
        yard.get("former_name"),
    )
    if not candidates:
        return {"success": True, "shipyard_id": shipyard_id, "name": yard.get("Local_name"), "count": 0, "data": []}

    conn = mysql.connector.connect(**settings.NEWS_DB_CONFIG)
    cursor = conn.cursor(dictionary=True)
    try:
        likes = [f"%{c}%" for c in candidates]
        where = " OR ".join(["data LIKE %s"] * len(likes))
        query = (
            f"SELECT id, title, link, date, summary, news_report, data, source_platform, tags, broad_category "
            f"FROM {settings.NEWS_TABLE} WHERE ({where}) ORDER BY date DESC, id DESC LIMIT %s"
        )
        cursor.execute(query, (*likes, limit))
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    items = []
    for row in rows:
        transactions = _extract_matching_transactions(row.get("data"), candidates)
        if not transactions:
            continue
        items.append({
            "id": row["id"],
            "title": row["title"],
            "date": row["date"].isoformat() if row.get("date") else None,
            "source_platform": row.get("source_platform") or "unknown",
            "link": row.get("link") or "",
            "summary": row.get("summary") or "",
            "report": row.get("news_report") or "",
            "category": row.get("broad_category") or "",
            "tags": row.get("tags") or "",
            "matched_transactions": transactions,
        })

    return {
        "success": True,
        "shipyard_id": shipyard_id,
        "name": yard.get("Local_name"),
        "count": len(items),
        "data": items,
    }
