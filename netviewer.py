#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Tom Salzer, KJ7T
# Full license text: see LICENSE-MIT in the repository root.
#
# This file is original work and does not import upstream KK7NQN code. It is
# distributed here as part of a GPLv3 project, but is additionally available
# under the MIT license for independent reuse.
"""
netviewer.py - local web UI for querying the `transcriptions` table by
time range. Binds to 0.0.0.0 so it's reachable over Tailscale.

Mostly read-only; the one write path is renaming a detected net
(updates net_data.net_name and transcription_analysis.detected_net_name).

Run:
    /opt/allstar-transcriber/venv/bin/python3 /opt/allstar-transcriber/web/netviewer.py

Env vars (same convention as transcribe_and_log.py):
    DB_HOST (default 127.0.0.1), DB_USER (default transcriber),
    DB_PASS (default ""), DB_NAME (default repeater)
    NETVIEWER_PORT (default 8088)
"""
import os
from flask import Flask, request, jsonify, Response
import mysql.connector

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "user": os.getenv("DB_USER", "transcriber"),
    "password": os.getenv("DB_PASS", ""),
    "database": os.getenv("DB_NAME", "repeater"),
}
PORT = int(os.getenv("NETVIEWER_PORT", "8088"))

app = Flask(__name__)

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>588416 Transcript Viewer</title>
<style>
  body { font-family: sans-serif; margin: 2em; background: #111; color: #eee; }
  h1 { font-size: 1.3em; }
  form { margin-bottom: 1.5em; }
  label { margin-right: 0.5em; }
  input[type=datetime-local] { margin-right: 1em; padding: 0.3em; }
  button { padding: 0.4em 1em; margin-right: 0.5em; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid #444; padding: 0.4em 0.6em; text-align: left; vertical-align: top; }
  th { background: #222; position: sticky; top: 0; }
  tr:nth-child(even) { background: #1a1a1a; }
  .net-yes { color: #6f6; font-weight: bold; }
  .empty { color: #888; font-style: italic; }
  #status { margin-bottom: 1em; color: #aaa; }
  #netPanel { margin-bottom: 1.5em; }
  .netrow { background: #1a1a1a; border: 1px solid #444; padding: 0.6em 0.8em;
            margin-bottom: 0.5em; border-radius: 4px; }
  .netrow label { color: #aaa; }
  .netrow input[type=text] { padding: 0.3em; width: 22em; margin: 0 0.5em;
                             background: #222; color: #eee; border: 1px solid #555; }
  .saved { color: #6f6; margin-left: 0.5em; }
</style>
</head>
<body>
<h1>Node 588416 &mdash; Transcript Viewer</h1>
<form id="queryForm">
  <label>Start: <input type="datetime-local" id="start" required></label>
  <label>End: <input type="datetime-local" id="end" required></label>
  <button type="submit">Query</button>
  <button type="button" id="downloadBtn">Download .txt</button>
</form>
<div id="status"></div>
<div id="netPanel"></div>
<table id="resultsTable" style="display:none">
  <thead>
    <tr><th>Time</th><th>Transcription</th><th>Net?</th><th>Net Name</th></tr>
  </thead>
  <tbody id="resultsBody"></tbody>
</table>

<script>
async function runQuery() {
  const start = document.getElementById('start').value;
  const end = document.getElementById('end').value;
  const statusDiv = document.getElementById('status');
  const table = document.getElementById('resultsTable');
  const body = document.getElementById('resultsBody');
  const panel = document.getElementById('netPanel');
  statusDiv.textContent = 'Querying...';
  table.style.display = 'none';
  body.innerHTML = '';
  panel.innerHTML = '';

  try {
    const resp = await fetch(`/api/transcripts?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({error: resp.statusText}));
      statusDiv.textContent = 'Error: ' + (err.error || resp.statusText);
      return;
    }
    const rows = await resp.json();
    if (rows.length === 0) {
      statusDiv.textContent = 'No transcripts found in that range.';
      return;
    }

    // Distinct nets present in this window, for the rename panel.
    const nets = new Map();
    for (const r of rows) {
      if (r.net_id && !nets.has(r.net_id)) {
        nets.set(r.net_id, r.detected_net_name || '');
      }
    }
    for (const [netId, name] of nets) {
      const div = document.createElement('div');
      div.className = 'netrow';
      div.innerHTML = `<label>Net #${netId} name:</label>` +
        `<input type="text" id="name-${netId}" value="${name.replace(/"/g, '&quot;')}">` +
        `<button type="button" onclick="saveName(${netId})">Save name</button>` +
        `<span class="saved" id="saved-${netId}"></span>`;
      panel.appendChild(div);
    }

    for (const r of rows) {
      const tr = document.createElement('tr');
      const text = r.transcription && r.transcription.trim()
        ? r.transcription
        : '<span class="empty">(empty)</span>';
      const netCell = r.is_net ? '<span class="net-yes">Yes</span>' : 'No';
      tr.innerHTML = `<td>${r.timestamp}</td><td>${text}</td><td>${netCell}</td><td>${r.detected_net_name || ''}</td>`;
      body.appendChild(tr);
    }
    statusDiv.textContent = `${rows.length} transcript(s) found.`;
    table.style.display = '';
  } catch (err) {
    statusDiv.textContent = 'Request failed: ' + err;
  }
}

async function saveName(netId) {
  const input = document.getElementById('name-' + netId);
  const flag = document.getElementById('saved-' + netId);
  const name = input.value.trim();
  if (!name) { flag.textContent = 'Name cannot be empty.'; return; }
  flag.textContent = 'Saving...';
  try {
    const resp = await fetch('/api/net_name', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({net_id: netId, name: name})
    });
    const data = await resp.json();
    if (!resp.ok) { flag.textContent = 'Error: ' + (data.error || resp.statusText); return; }
    flag.textContent = `Saved (${data.rows_updated} row(s)).`;
    await runQuery();
  } catch (err) {
    flag.textContent = 'Request failed: ' + err;
  }
}

document.getElementById('queryForm').addEventListener('submit', (e) => {
  e.preventDefault();
  runQuery();
});

document.getElementById('downloadBtn').addEventListener('click', () => {
  const start = document.getElementById('start').value;
  const end = document.getElementById('end').value;
  const statusDiv = document.getElementById('status');
  if (!start || !end) {
    statusDiv.textContent = 'Set a start and end time first.';
    return;
  }
  window.location = `/download?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;
});
</script>
</body>
</html>
"""

QUERY_SQL = """
    SELECT t.id, t.timestamp, t.transcription,
           ta.is_net, ta.net_id, ta.detected_net_name, ta.detected_club_name
    FROM transcriptions t
    LEFT JOIN transcription_analysis ta ON ta.transcription_id = t.id
    WHERE t.timestamp BETWEEN %s AND %s
    ORDER BY t.timestamp ASC
"""


def _query_rows(start_sql, end_sql):
    """Run the range query. Returns (rows, error_string)."""
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cur = conn.cursor(dictionary=True)
        cur.execute(QUERY_SQL, (start_sql, end_sql))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        for r in rows:
            r["timestamp"] = r["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
        return rows, None
    except Exception as e:
        return None, str(e)


def _rows_to_text(rows, start_sql, end_sql):
    """Render rows as plain text suitable for pasting into an LLM prompt."""
    nonempty = [r for r in rows if (r.get("transcription") or "").strip()]
    empty_count = len(rows) - len(nonempty)

    names = []
    for r in rows:
        n = r.get("detected_net_name")
        if n and n not in names:
            names.append(n)

    lines = [
        "Node 588416 transcript",
        f"Window: {start_sql} to {end_sql}",
        f"Transmissions with text: {len(nonempty)}",
    ]
    if empty_count:
        lines.append(f"Transmissions with no transcribed text (omitted below): {empty_count}")
    if names:
        lines.append("Detected net: " + ", ".join(names))
    lines.append("")

    # If the whole window falls on one date, drop the date from each line.
    dates = {r["timestamp"][:10] for r in nonempty}
    single_day = len(dates) <= 1

    for r in nonempty:
        ts = r["timestamp"][11:] if single_day else r["timestamp"]
        text = " ".join((r["transcription"] or "").split())
        lines.append(f"{ts}  {text}")

    return "\n".join(lines) + "\n"


def _download_filename(start, end):
    """Build transcript_YYYYMMDD_HHMM-HHMM.txt from the datetime-local values."""
    try:
        day = start[:10].replace("-", "")
        t1 = start[11:16].replace(":", "")
        t2 = end[11:16].replace(":", "")
        return f"transcript_{day}_{t1}-{t2}.txt"
    except Exception:
        return "transcript.txt"


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.route("/api/transcripts")
def api_transcripts():
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    if not start or not end:
        return jsonify({"error": "start and end are required"}), 400

    # datetime-local gives "YYYY-MM-DDTHH:MM" -- convert to MySQL format
    start_sql = start.replace("T", " ") + ":00"
    end_sql = end.replace("T", " ") + ":00"

    rows, err = _query_rows(start_sql, end_sql)
    if err:
        return jsonify({"error": err}), 500
    return jsonify(rows)


@app.route("/api/net_name", methods=["POST"])
def api_net_name():
    """Rename a detected net in both net_data and transcription_analysis."""
    data = request.get_json(silent=True) or {}
    net_id = data.get("net_id")
    name = (data.get("name") or "").strip()

    if not isinstance(net_id, int):
        return jsonify({"error": "net_id must be an integer"}), 400
    if not name:
        return jsonify({"error": "name is required"}), 400
    if len(name) > 255:
        return jsonify({"error": "name too long (max 255)"}), 400

    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("UPDATE net_data SET net_name = %s WHERE id = %s", (name, net_id))
        n1 = cur.rowcount
        cur.execute(
            "UPDATE transcription_analysis SET detected_net_name = %s WHERE net_id = %s",
            (name, net_id),
        )
        n2 = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"ok": True, "net_id": net_id, "name": name,
                        "rows_updated": n1 + n2})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/download")
def download():
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    if not start or not end:
        return Response("start and end are required\n", status=400, mimetype="text/plain")

    start_sql = start.replace("T", " ") + ":00"
    end_sql = end.replace("T", " ") + ":00"

    rows, err = _query_rows(start_sql, end_sql)
    if err:
        return Response(f"Error: {err}\n", status=500, mimetype="text/plain")
    if not rows:
        return Response("No transcripts found in that range.\n", status=404, mimetype="text/plain")

    body = _rows_to_text(rows, start_sql, end_sql)
    fname = _download_filename(start, end)
    return Response(
        body,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
