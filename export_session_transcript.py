#!/usr/bin/env python3
"""
Exports the complete omp session transcript from ~/.omp/agent/sessions/-/ to
projects/survey-pipeline/docs/session/CONVERSATION_TRANSCRIPT.md and updates
survey-pipeline-session.jsonl.
"""
import json
import os
import shutil
from datetime import datetime

SESSION_SRC = "/home/ebylxn/.omp/agent/sessions/-/2026-10-30T15-22-29-730Z_01a18d67-c1e2-7109-8dab-44997f7b040f.jsonl"
SESSION_DST = "/home/ebylxn/projects/survey-pipeline/docs/session/survey-pipeline-session.jsonl"
TRANSCRIPT_DST = "/home/ebylxn/projects/survey-pipeline/docs/session/CONVERSATION_TRANSCRIPT.md"

def extract_text_from_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "tool_use":
                    parts.append(f"\n*Tool Call: `{item.get('name')}`*\n")
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content)

def main():
    # 1. Copy raw JSONL
    print(f"Copying {SESSION_SRC} -> {SESSION_DST}...")
    shutil.copyfile(SESSION_SRC, SESSION_DST)
    src_size = os.path.getsize(SESSION_DST)
    print(f"Copied {src_size / 1024 / 1024:.2f} MB.")

    # 2. Parse turns
    print("Parsing session turns...")
    turns = []
    current_turn = None

    with open(SESSION_SRC, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue

            if data.get("type") != "message":
                continue

            msg = data.get("message", {})
            role = data.get("role") or msg.get("role")
            ts = data.get("timestamp") or data.get("id", "")

            content = msg.get("content", "")
            text = extract_text_from_content(content)

            if role == "user":
                if current_turn:
                    turns.append(current_turn)
                current_turn = {
                    "turn_number": len(turns) + 1,
                    "timestamp": ts,
                    "user_text": text,
                    "assistant_text": []
                }
            elif role == "assistant" and current_turn:
                current_turn["assistant_text"].append(text)

    if current_turn:
        turns.append(current_turn)

    print(f"Extracted {len(turns)} total turns.")

    # 3. Format Markdown Transcript
    lines = [
        "# Survey Processing Pipeline Architecture & Implementation Transcript",
        "",
        f"**Archived Session File:** `survey-pipeline-session.jsonl`  ",
        f"**Total Turns:** {len(turns)}  ",
        f"**Last Updated:** `{datetime.utcnow().isoformat()}Z`  ",
        f"**Workspace:** `/home/ebylxn`  ",
        "**Scope:** End-to-end design, audit, implementation, Vietnamese template localization, vision AI benchmarking, adversarial testing, financial estimation (VND), and infrastructure deployment for the Survey Processing Pipeline.",
        "",
        "---",
        ""
    ]

    for turn in turns:
        t_num = turn["turn_number"]
        t_ts = turn["timestamp"]
        u_text = turn["user_text"].strip()
        a_combined = "\n\n".join(turn["assistant_text"]).strip()

        lines.append(f"## Turn {t_num}")
        lines.append("")
        lines.append(f"**Timestamp:** `{t_ts}`  ")
        lines.append("")
        lines.append("**User:**")
        lines.append("")
        lines.append(u_text)
        lines.append("")
        lines.append("**Assistant:**")
        lines.append("")
        lines.append(a_combined if a_combined else "*(Tool execution / background task)*")
        lines.append("")
        lines.append("---")
        lines.append("")

    with open(TRANSCRIPT_DST, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    out_size = os.path.getsize(TRANSCRIPT_DST)
    print(f"Successfully generated {TRANSCRIPT_DST} ({out_size / 1024 / 1024:.2f} MB, {len(turns)} turns).")

if __name__ == "__main__":
    main()
