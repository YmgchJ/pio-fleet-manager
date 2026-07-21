"""Chunk CSV builder — ported from Volvocine_Pico/ChunkProcessor.py (logic unchanged)."""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd


def build_dataframe_for_chunk(
    agent_id: int,
    chunk_data: list,
    chunk_send_micros: list,
    chunk_recv_times: list,
    save_dir: Path | None = None,
):
    if not chunk_data:
        return None, None

    save_dir = save_dir or Path("saved_chunks")
    save_dir.mkdir(parents=True, exist_ok=True)

    wrapped_send_secs = [(((s >> 8) % 16777216) << 8) / 1e6 for s in chunk_send_micros]
    offsets = [recv - send for send, recv in zip(wrapped_send_secs, chunk_recv_times)]
    offset = sum(offsets) / len(offsets)

    df = pd.DataFrame(chunk_data, columns=["micros24", "a0", "a1", "a2", "a3"])

    micros_list = df["micros24"].tolist()
    extended = [0] * len(micros_list)
    wrap_offset = 0
    prev = micros_list[0]
    extended[0] = prev
    for i in range(1, len(micros_list)):
        curr = micros_list[i]
        if curr < prev:
            wrap_offset += 16777216
        extended[i] = curr + wrap_offset
        prev = curr

    df["micros32"] = extended
    df["micros32_raw"] = [val << 8 for val in extended]
    df["time_local_sec"] = [val / 1e6 for val in df["micros32_raw"]]
    df["time_pc_sec_abs"] = df["time_local_sec"] + offset

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    chunk_id = timestamp
    df["agent_id"] = agent_id
    df["chunk_id"] = chunk_id

    filename = save_dir / f"chunk_agent_{agent_id}_{timestamp}.csv"
    save_columns = [
        "time_pc_sec_abs",
        "micros32",
        "micros32_raw",
        "time_local_sec",
        "a0",
        "a1",
        "a2",
        "a3",
        "agent_id",
        "chunk_id",
    ]
    df.to_csv(filename, index=False, columns=save_columns)
    print(f"[INFO] Agent={agent_id}, chunk size={len(df)} -> Saved to {filename}")

    return df[["agent_id", "chunk_id", "time_pc_sec_abs", "a0", "a1", "a2", "a3"]], str(filename)


def merge_and_save_chunks(
    chunk_files: list[str],
    output_dir: Path | None = None,
    archive_dir: Path | None = None,
    merged_filename: str | None = None,
    delete_chunks: bool = True,
) -> str | None:
    if not chunk_files:
        print("[INFO] No chunk files provided. Skipping merge process.")
        return None

    output_dir = output_dir or Path("merged_chunks")
    output_dir.mkdir(parents=True, exist_ok=True)

    merged_data = pd.DataFrame()
    for file in chunk_files:
        df = pd.read_csv(file)
        merged_data = pd.concat([merged_data, df], ignore_index=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    merged_file = output_dir / (merged_filename or f"merged_{timestamp}.csv")
    merged_data.to_csv(merged_file, index=False)
    print(f"[INFO] Merged data saved to {merged_file}")

    for file in chunk_files:
        src = Path(file)
        try:
            if archive_dir is not None:
                archive_dir.mkdir(parents=True, exist_ok=True)
                dest = archive_dir / src.name
                shutil.move(str(src), str(dest))
                print(f"[INFO] Archived chunk file: {dest}")
            elif delete_chunks:
                os.remove(file)
                print(f"[INFO] Deleted chunk file: {file}")
        except OSError as e:
            print(f"[ERROR] Could not process chunk file {file}: {e}")

    return str(merged_file)
