"""
Shipyard AI API endpoints.

Provides extraction from text/files for shipyard data.
All endpoints require manager-or-above permissions.
"""

import os
import shutil
import tempfile
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from pydantic import BaseModel, Field

from app.api.shipyards import get_current_manager_or_above
from app.db.models import User
from app.shipyard_ai.extractor import (
    extract_from_texts,
    extract_from_files,
)
from app.shipyard_ai.deduplicator import deduplicate_shipyard_record

router = APIRouter(prefix="/shipyard-ai", tags=["Shipyard AI"])


class ExtractTextRequest(BaseModel):
    texts: List[str] = Field(..., min_length=1, description="List of shipyard description texts")


@router.post("/extract-text")
async def extract_text(
    request: ExtractTextRequest,
    current_user: User = Depends(get_current_manager_or_above),
):
    """Extract structured shipyard information from one or more text strings."""
    try:
        data = extract_from_texts(request.texts)
        deduped = [deduplicate_shipyard_record(r, add_new=True) for r in data]
        return {"success": True, "data": deduped}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Extraction failed: {e}",
        )


@router.post("/extract-file")
async def extract_file(
    files: List[UploadFile] = File(..., description="One or more files to extract"),
    current_user: User = Depends(get_current_manager_or_above),
):
    """Upload one or more files, extract shipyard information, and clean up temp files."""
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No files provided",
        )

    temp_dir = tempfile.mkdtemp(prefix="shipyard_ai_")
    file_paths = []

    try:
        for upload in files:
            # Sanitize filename
            safe_name = os.path.basename(upload.filename or "upload")
            _, ext = os.path.splitext(safe_name)
            # Use a UUID name to avoid collisions while preserving extension
            dest_name = f"{uuid.uuid4().hex}{ext}"
            dest_path = os.path.join(temp_dir, dest_name)

            with open(dest_path, "wb") as buffer:
                shutil.copyfileobj(upload.file, buffer)
            file_paths.append(dest_path)

        data = extract_from_files(file_paths)
        deduped = [deduplicate_shipyard_record(r, add_new=True) for r in data]
        return {"success": True, "data": deduped}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"File extraction failed: {e}",
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        for upload in files:
            upload.file.close()
