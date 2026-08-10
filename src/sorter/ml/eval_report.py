"""HTML evaluation report — a verbatim port of the legacy app's batch image
report so this app produces the exact same self-contained, interactive report
file.

Thumbnails are embedded as base64 data URIs, so the report is a single portable
file. Everything here is cross-platform (pathlib + PIL only).
"""

from __future__ import annotations

import base64
import importlib
import io
import json
from pathlib import Path
from typing import Any

# Routed through `importlib.import_module` (rather than a plain `from PIL
# import Image`) so the checker infers an ordinary `<module> | None` union
# from the two assignment branches below, instead of treating a successful
# `import` statement as a fixed module-type declaration that the `except`
# branch's `None` then conflicts with.
try:  # Pillow is a hard dependency, but degrade gracefully if absent.
    Image = importlib.import_module("PIL.Image")
except Exception:  # pragma: no cover
    Image = None


def create_thumbnail_b64(image_path: Path, size: int = 128) -> str:
    """Base64 JPEG thumbnail data URI for embedding in the report, or ''."""
    if Image is None:
        return ""
    try:
        img = Image.open(image_path)
        # JPEG can't hold alpha/palette modes — normalise so non-JPEG sources
        # (PNG/BMP) still thumbnail instead of erroring out.
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((size, size), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        img_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        return f"data:image/jpeg;base64,{img_b64}"
    except Exception:
        return ""


# The HTML template is split exactly where the original embeds the results JSON
# (``const data = <json>;``). Verbatim from the legacy app's report.
_HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Batch Image Classification Report</title>
    <style>
        * {
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        h1 {
            margin-top: 0;
            color: #333;
        }
        
        .controls {
            margin-bottom: 20px;
            padding: 15px;
            background: #f9f9f9;
            border-radius: 4px;
            display: flex;
            gap: 15px;
            flex-wrap: wrap;
            align-items: center;
        }
        
        .control-group {
            display: flex;
            flex-direction: column;
            gap: 5px;
        }
        
        .control-group label {
            font-size: 12px;
            font-weight: 600;
            color: #666;
            text-transform: uppercase;
        }
        
        input[type="text"], select {
            padding: 8px 12px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
            min-width: 200px;
        }
        
        .stats {
            margin-bottom: 20px;
            padding: 15px;
            background: #e8f4f8;
            border-radius: 4px;
            display: flex;
            gap: 30px;
            flex-wrap: wrap;
        }
        
        .stat-item {
            display: flex;
            flex-direction: column;
        }
        
        .stat-label {
            font-size: 12px;
            color: #666;
            text-transform: uppercase;
            font-weight: 600;
        }
        
        .stat-value {
            font-size: 24px;
            font-weight: bold;
            color: #333;
        }
        
        .stat-value.small {
            font-size: 18px;
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 10px;
        }
        
        thead {
            background-color: #f0f0f0;
            position: sticky;
            top: 0;
            z-index: 10;
        }
        
        th {
            padding: 12px;
            text-align: left;
            font-weight: 600;
            color: #333;
            cursor: pointer;
            user-select: none;
            border-bottom: 2px solid #ddd;
        }
        
        th:hover {
            background-color: #e0e0e0;
        }
        
        th.sortable::after {
            content: ' ⇅';
            color: #999;
        }
        
        th.sort-asc::after {
            content: ' ▲';
            color: #333;
        }
        
        th.sort-desc::after {
            content: ' ▼';
            color: #333;
        }
        
        td {
            padding: 12px;
            border-bottom: 1px solid #eee;
        }
        
        tr:hover {
            background-color: #f9f9f9;
        }
        
        .thumbnail {
            width: 128px;
            height: 128px;
            object-fit: contain;
            border: 1px solid #ddd;
            border-radius: 4px;
        }
        
        .confidence {
            font-weight: 600;
        }
        
        .confidence-high {
            color: #28a745;
        }
        
        .confidence-medium {
            color: #ffc107;
        }
        
        .confidence-low {
            color: #dc3545;
        }
        
        .match {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 3px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }
        
        .match-yes {
            background-color: #d4edda;
            color: #155724;
        }
        
        .match-no {
            background-color: #f8d7da;
            color: #721c24;
        }
        
        .match-unknown {
            background-color: #e2e3e5;
            color: #383d41;
        }
        
        .mapping-status {
            display: inline-block;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 10px;
            font-weight: 600;
            text-transform: uppercase;
            margin-left: 5px;
        }
        
        .mapping-yes {
            background-color: #d1ecf1;
            color: #0c5460;
        }
        
        .mapping-no {
            background-color: #fff3cd;
            color: #856404;
        }
        
        .no-results {
            text-align: center;
            padding: 40px;
            color: #999;
            font-size: 18px;
        }
        
        .summary-section {
            margin-bottom: 20px;
            border: 1px solid #ddd;
            border-radius: 4px;
            overflow: hidden;
        }
        
        .summary-header {
            padding: 15px;
            background: #f9f9f9;
            cursor: pointer;
            user-select: none;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 10px;
            transition: background-color 0.2s;
        }
        
        .summary-header:hover {
            background: #f0f0f0;
        }
        
        .toggle-icon {
            font-size: 12px;
            transition: transform 0.2s;
            display: inline-block;
        }
        
        .toggle-icon.expanded {
            transform: rotate(90deg);
        }
        
        .summary-content {
            padding: 15px;
            background: white;
        }
        
        .summary-table {
            width: 100%;
            border-collapse: collapse;
        }
        
        .summary-table th {
            background-color: #f5f5f5;
            padding: 10px;
            text-align: left;
            font-weight: 600;
            border-bottom: 2px solid #ddd;
            position: static;
        }
        
        .summary-table td {
            padding: 10px;
            border-bottom: 1px solid #eee;
        }
        
        .summary-table tr:hover {
            background-color: #f9f9f9;
        }
        
        .summary-table .class-name {
            font-weight: 600;
            color: #333;
        }
        
        .summary-table .count {
            color: #666;
        }
        
        .summary-table .conf-avg {
            font-weight: 600;
        }
        
        .summary-table .conf-high {
            color: #28a745;
        }
        
        .summary-table .conf-low {
            color: #dc3545;
        }
        
        .summary-table .mismatch-count {
            color: #dc3545;
            font-weight: 600;
            cursor: pointer;
            text-decoration: underline;
            text-decoration-style: dotted;
        }
        
        .summary-table .mismatch-count:hover {
            background-color: #fff3cd;
            text-decoration-style: solid;
        }
        
        .summary-table .mismatch-zero {
            color: #999;
            font-weight: normal;
        }
        
        .raw-classification {
            font-size: 11px;
            color: #999;
            font-style: italic;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Batch Inference Report</h1>
        
        <div class="stats">
            <div class="stat-item">
                <span class="stat-label">Total Images</span>
                <span class="stat-value" id="total-count">0</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">Showing</span>
                <span class="stat-value" id="visible-count">0</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">Avg Confidence</span>
                <span class="stat-value" id="avg-confidence">0%</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">Total Accuracy</span>
                <span class="stat-value small" id="total-accuracy">N/A</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">Known Accuracy</span>
                <span class="stat-value small" id="known-accuracy">N/A</span>
            </div>
        </div>
        
        <div class="summary-section">
            <div class="summary-header" id="summary-toggle">
                <span class="toggle-icon">▶</span>
                <span>Classification Summary</span>
            </div>
            <div class="summary-content" id="summary-content" style="display: none;">
                <table class="summary-table">
                    <thead>
                        <tr>
                            <th>Classification</th>
                            <th># of Images</th>
                            <th>Avg Confidence</th>
                            <th>High</th>
                            <th>Low</th>
                            <th>Mismatched</th>
                        </tr>
                    </thead>
                    <tbody id="summary-body">
                    </tbody>
                </table>
            </div>
        </div>
        
        <div class="controls">
            <div class="control-group">
                <label for="search">Search Filename</label>
                <input type="text" id="search" placeholder="Type to filter...">
            </div>
            
            <div class="control-group">
                <label for="class-filter">Classification</label>
                <select id="class-filter">
                    <option value="">All Classifications</option>
                </select>
            </div>
            
            <div class="control-group">
                <label for="match-filter">Match Status</label>
                <select id="match-filter">
                    <option value="">All Matches</option>
                    <option value="match">Matches Original</option>
                    <option value="mismatch">Doesn't Match</option>
                    <option value="unknown">No Original</option>
                </select>
            </div>
            
            <div class="control-group">
                <label for="mapping-filter">Mapping Status</label>
                <select id="mapping-filter">
                    <option value="">All Images</option>
                    <option value="mapped">Has Mapping</option>
                    <option value="unmapped">No Mapping</option>
                </select>
            </div>
            
            <div class="control-group">
                <label for="confidence-filter">Min Confidence</label>
                <select id="confidence-filter">
                    <option value="0">All (0%+)</option>
                    <option value="0.5">Medium (50%+)</option>
                    <option value="0.7">High (70%+)</option>
                    <option value="0.9">Very High (90%+)</option>
                </select>
            </div>
        </div>
        
        <table id="results-table">
            <thead>
                <tr>
                    <th class="sortable" data-column="thumbnail">Preview</th>
                    <th class="sortable" data-column="classification">Classification</th>
                    <th class="sortable" data-column="confidence">Confidence</th>
                    <th class="sortable" data-column="original_classification">Original Class</th>
                    <th class="sortable" data-column="match">Match</th>
                    <th class="sortable" data-column="filename">Filename</th>
                </tr>
            </thead>
            <tbody id="results-body">
            </tbody>
        </table>
        
        <div id="no-results" class="no-results" style="display: none;">
            No results match your filters
        </div>
    </div>
    
    <script>
        const data = """

_HTML_TAIL = """;
        
        let currentSort = { column: null, direction: 'asc' };
        
        function getConfidenceClass(conf) {
            if (conf >= 0.7) return 'confidence-high';
            if (conf >= 0.4) return 'confidence-medium';
            return 'confidence-low';
        }
        
        function getMatchStatus(row) {
            if (!row.original_classification) return 'unknown';
            return row.classification === row.original_classification ? 'match' : 'mismatch';
        }
        
        function getMatchLabel(status) {
            if (status === 'match') return '<span class="match match-yes">Match</span>';
            if (status === 'mismatch') return '<span class="match match-no">Mismatch</span>';
            return '<span class="match match-unknown">N/A</span>';
        }
        
        function getMappingBadge(hasMapping) {
            if (hasMapping) {
                return '<span class="mapping-status mapping-yes">Mapped</span>';
            } else {
                return '<span class="mapping-status mapping-no">Unmapped</span>';
            }
        }
        
        function renderTable(filteredData) {
            const tbody = document.getElementById('results-body');
            const noResults = document.getElementById('no-results');
            
            if (filteredData.length === 0) {
                tbody.innerHTML = '';
                noResults.style.display = 'block';
                return;
            }
            
            noResults.style.display = 'none';
            
            tbody.innerHTML = filteredData.map(row => {
                const confClass = getConfidenceClass(row.confidence);
                const matchStatus = getMatchStatus(row);
                const matchLabel = getMatchLabel(matchStatus);
                const confPercent = (row.confidence * 100).toFixed(1);
                const mappingBadge = row.original_classification ? getMappingBadge(row.has_mapping) : '';
                
                // Show raw classification if different from mapped
                const originalDisplay = row.original_classification 
                    ? (row.raw_original_classification && row.raw_original_classification !== row.original_classification
                        ? `${row.original_classification} ${mappingBadge}<br><span class="raw-classification">(${row.raw_original_classification})</span>`
                        : `${row.original_classification} ${mappingBadge}`)
                    : '-';
                
                return `
                    <tr>
                        <td><img src="${row.thumbnail}" class="thumbnail" alt="${row.filename}" onerror="this.style.display='none'"></td>
                        <td><strong>${row.classification}</strong></td>
                        <td><span class="confidence ${confClass}">${confPercent}%</span></td>
                        <td>${originalDisplay}</td>
                        <td>${matchLabel}</td>
                        <td><small>${row.filename}</small></td>
                    </tr>
                `;
            }).join('');
        }
        
        function filterData() {
            const searchTerm = document.getElementById('search').value.toLowerCase();
            const classFilter = document.getElementById('class-filter').value;
            const matchFilter = document.getElementById('match-filter').value;
            const mappingFilter = document.getElementById('mapping-filter').value;
            const confidenceFilter = parseFloat(document.getElementById('confidence-filter').value);
            
            let filtered = data.filter(row => {
                // Search filter
                if (searchTerm && !row.filename.toLowerCase().includes(searchTerm)) {
                    return false;
                }
                
                // Classification filter
                if (classFilter && row.classification !== classFilter) {
                    return false;
                }
                
                // Match filter
                if (matchFilter) {
                    const matchStatus = getMatchStatus(row);
                    if (matchFilter === 'match' && matchStatus !== 'match') return false;
                    if (matchFilter === 'mismatch' && matchStatus !== 'mismatch') return false;
                    if (matchFilter === 'unknown' && matchStatus !== 'unknown') return false;
                }
                
                // Mapping filter
                if (mappingFilter === 'mapped' && !row.has_mapping) return false;
                if (mappingFilter === 'unmapped' && row.has_mapping) return false;
                
                // Confidence filter
                if (row.confidence < confidenceFilter) {
                    return false;
                }
                
                return true;
            });
            
            // Apply sorting
            if (currentSort.column) {
                filtered = sortData(filtered, currentSort.column, currentSort.direction);
            }
            
            updateStats(filtered);
            renderTable(filtered);
        }
        
        function sortData(dataToSort, column, direction) {
            return [...dataToSort].sort((a, b) => {
                let aVal = a[column];
                let bVal = b[column];
                
                // Handle special case for match status
                if (column === 'match') {
                    aVal = getMatchStatus(a);
                    bVal = getMatchStatus(b);
                }
                
                // Handle null/empty values
                if (!aVal && !bVal) return 0;
                if (!aVal) return 1;
                if (!bVal) return -1;
                
                // Compare
                let comparison = 0;
                if (typeof aVal === 'number' && typeof bVal === 'number') {
                    comparison = aVal - bVal;
                } else {
                    comparison = String(aVal).localeCompare(String(bVal));
                }
                
                return direction === 'asc' ? comparison : -comparison;
            });
        }
        
        function updateStats(filteredData) {
            document.getElementById('total-count').textContent = data.length;
            document.getElementById('visible-count').textContent = filteredData.length;
            
            // Total Accuracy: all images with any original classification
            const imagesWithAnyOriginal = data.filter(row => 
                row.original_classification && row.original_classification.trim() !== ''
            );
            
            if (imagesWithAnyOriginal.length > 0) {
                const totalMatches = imagesWithAnyOriginal.filter(row => 
                    row.classification === row.original_classification
                ).length;
                const totalAccuracy = (totalMatches / imagesWithAnyOriginal.length) * 100;
                document.getElementById('total-accuracy').textContent = 
                    `${totalAccuracy.toFixed(1)}% (${totalMatches}/${imagesWithAnyOriginal.length})`;
            } else {
                document.getElementById('total-accuracy').textContent = 'N/A';
            }
            
            // Known Accuracy: only images with mapped classifications
            const imagesWithMapping = data.filter(row => row.has_mapping);
            
            if (imagesWithMapping.length > 0) {
                const knownMatches = imagesWithMapping.filter(row => 
                    row.classification === row.original_classification
                ).length;
                const knownAccuracy = (knownMatches / imagesWithMapping.length) * 100;
                document.getElementById('known-accuracy').textContent = 
                    `${knownAccuracy.toFixed(1)}% (${knownMatches}/${imagesWithMapping.length})`;
            } else {
                document.getElementById('known-accuracy').textContent = 'N/A';
            }

            if (filteredData.length > 0) {
                const avgConf = filteredData.reduce((sum, row) => sum + row.confidence, 0) / filteredData.length;
                document.getElementById('avg-confidence').textContent = (avgConf * 100).toFixed(1) + '%';
            } else {
                document.getElementById('avg-confidence').textContent = '0%';
            }
            
            // Update classification summary
            updateClassificationSummary();
        }
        
        function updateClassificationSummary() {
            const summaryBody = document.getElementById('summary-body');
            
            // Group by classification
            const classSummary = {};
            data.forEach(row => {
                const cls = row.classification;
                if (!classSummary[cls]) {
                    classSummary[cls] = {
                        count: 0,
                        confidences: [],
                        mismatches: 0
                    };
                }
                classSummary[cls].count++;
                classSummary[cls].confidences.push(row.confidence);
                
                // Count mismatches (only if original classification exists and doesn't match)
                if (row.original_classification && row.classification !== row.original_classification) {
                    classSummary[cls].mismatches++;
                }
            });
            
            // Calculate stats and sort by classification name
            const summaryRows = Object.entries(classSummary)
                .map(([cls, stats]) => {
                    const avg = stats.confidences.reduce((sum, c) => sum + c, 0) / stats.confidences.length;
                    const high = Math.max(...stats.confidences);
                    const low = Math.min(...stats.confidences);
                    return { 
                        cls, 
                        count: stats.count, 
                        avg, 
                        high, 
                        low,
                        mismatches: stats.mismatches
                    };
                })
                .sort((a, b) => a.cls.localeCompare(b.cls));
            
            // Render summary table
            summaryBody.innerHTML = summaryRows.map(row => {
                const avgClass = getConfidenceClass(row.avg);
                const highClass = getConfidenceClass(row.high);
                const lowClass = getConfidenceClass(row.low);
                const mismatchClass = row.mismatches > 0 ? 'mismatch-count' : 'mismatch-zero';
                const mismatchAttr = row.mismatches > 0 ? `data-classification="${row.cls}" title="Click to filter mismatches"` : '';
                
                return `
                    <tr>
                        <td class="class-name">${row.cls}</td>
                        <td class="count">${row.count}</td>
                        <td class="conf-avg ${avgClass}">${(row.avg * 100).toFixed(1)}%</td>
                        <td class="conf-high ${highClass}">${(row.high * 100).toFixed(1)}%</td>
                        <td class="conf-low ${lowClass}">${(row.low * 100).toFixed(1)}%</td>
                        <td class="${mismatchClass}" ${mismatchAttr}>${row.mismatches}</td>
                    </tr>
                `;
            }).join('');
            
            // Add click handlers to mismatch counts
            document.querySelectorAll('.mismatch-count').forEach(el => {
                el.addEventListener('click', () => {
                    const classification = el.getAttribute('data-classification');
                    filterToMismatches(classification);
                });
            });
        }
        
        function filterToMismatches(classification) {
            // Set filters to show mismatches for this classification
            document.getElementById('class-filter').value = classification;
            document.getElementById('match-filter').value = 'mismatch';
            
            // Clear other filters
            document.getElementById('search').value = '';
            document.getElementById('confidence-filter').value = '0';
            document.getElementById('mapping-filter').value = '';
            
            // Apply filters
            filterData();
            
            // Scroll to the table
            document.getElementById('results-table').scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
        
        function populateClassificationFilter() {
            const classes = [...new Set(data.map(row => row.classification))].sort();
            const select = document.getElementById('class-filter');
            
            classes.forEach(cls => {
                const option = document.createElement('option');
                option.value = cls;
                option.textContent = cls;
                select.appendChild(option);
            });
        }
        
        // Event listeners
        document.getElementById('search').addEventListener('input', filterData);
        document.getElementById('class-filter').addEventListener('change', filterData);
        document.getElementById('match-filter').addEventListener('change', filterData);
        document.getElementById('mapping-filter').addEventListener('change', filterData);
        document.getElementById('confidence-filter').addEventListener('change', filterData);
        
        // Summary toggle
        document.getElementById('summary-toggle').addEventListener('click', () => {
            const content = document.getElementById('summary-content');
            const icon = document.querySelector('.toggle-icon');
            
            if (content.style.display === 'none') {
                content.style.display = 'block';
                icon.classList.add('expanded');
            } else {
                content.style.display = 'none';
                icon.classList.remove('expanded');
            }
        });
        
        // Sorting
        document.querySelectorAll('th.sortable').forEach(th => {
            th.addEventListener('click', () => {
                const column = th.dataset.column;
                
                // Toggle direction
                if (currentSort.column === column) {
                    currentSort.direction = currentSort.direction === 'asc' ? 'desc' : 'asc';
                } else {
                    currentSort.column = column;
                    currentSort.direction = 'asc';
                }
                
                // Update UI
                document.querySelectorAll('th.sortable').forEach(h => {
                    h.classList.remove('sort-asc', 'sort-desc');
                });
                th.classList.add(currentSort.direction === 'asc' ? 'sort-asc' : 'sort-desc');
                
                filterData();
            });
        });
        
        // Initialize
        populateClassificationFilter();
        filterData();
    </script>
</body>
</html>
"""


def render_html(rows: list[dict[str, Any]]) -> str:
    """The exact report HTML for a list of report-shaped rows."""
    return _HTML_HEAD + json.dumps(rows) + _HTML_TAIL


def report_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert :func:`sorter.ml.evaluator` results to the report's row shape.

    Key order and the 0..1 confidence scale match the original exactly so the
    output is byte-identical for identical inputs.
    """
    rows = []
    for r in results:
        rows.append(
            {
                "filename": r["filename"],
                "filepath": r["filepath"],
                "thumbnail": create_thumbnail_b64(Path(r["filepath"])),
                "classification": r["predicted"],
                "confidence": float(r["confidence"]) / 100.0,
                "original_classification": r["original"],
                "raw_original_classification": r["raw_original"],
                "has_mapping": r["has_mapping"],
            }
        )
    return rows


def generate_report(results: list[dict[str, Any]], output_path: Path | str) -> Path:
    """Build the report from evaluator results and write it to ``output_path``."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(report_rows(results)), encoding="utf-8")
    return out
