import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHAT_HTML = PROJECT_ROOT / "channel" / "web" / "chat.html"
CONSOLE_JS = PROJECT_ROOT / "channel" / "web" / "static" / "js" / "console.js"


def _read(path):
    return path.read_text(encoding="utf-8")


def _section_between(text, start_marker, end_marker):
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def _button_for_i18n(html, i18n_key):
    for button in re.findall(r"<button\b.*?</button>", html, flags=re.S):
        if 'data-i18n="%s"' % i18n_key in button:
            return button
    raise AssertionError("button not found for %s" % i18n_key)


def _incremental_loop(script):
    return _section_between(
        script,
        "async function startVisualBuildLoopIncremental",
        "async function startVisualBuildLoopLegacy",
    )


def _progress_renderer(script):
    return _section_between(
        script,
        "function updateVisualBuildProgress",
        "function resetVisualBuildProgress",
    )


def test_visual_buttons_call_incremental_loop_not_visual_complete():
    html = _read(CHAT_HTML)

    build_button = _button_for_i18n(html, "knowledge_backend_visual_build")
    continue_button = _button_for_i18n(html, "knowledge_backend_visual_continue")

    assert "startVisualBuildLoop(getSelectedKnowledgeBackendDocumentId(), false, false)" in build_button
    assert "startVisualBuildLoop(getSelectedKnowledgeBackendDocumentId(), false, true)" in continue_button
    assert "visual/complete" not in build_button
    assert "visual/complete" not in continue_button
    assert "fetch(" not in build_button
    assert "fetch(" not in continue_button


def test_console_visual_build_request_is_incremental_limit_one():
    script = _read(CONSOLE_JS)
    loop = _incremental_loop(script)

    assert "fetch('/api/knowledge/admin/visual/build'" in loop
    assert "/api/knowledge/admin/visual/complete" not in loop

    request = re.search(r"body:\s*JSON\.stringify\(\{(?P<body>.*?)\}\)", loop, flags=re.S)
    assert request is not None
    body = request.group("body")
    expected_fields = {
        "document_id": r"document_id\s*:\s*sourceDoc\.id",
        "limit": r"limit\s*:\s*1\b",
        "run_id": r"run_id\s*:\s*runId",
        "analysis_backend": r"analysis_backend\s*:\s*backend",
        "retry_failed": r"retry_failed\s*:\s*requestRetryFailed",
    }
    for field, pattern in expected_fields.items():
        assert re.search(pattern, body), "%s missing from visual/build request body" % field


def test_console_queues_source_documents_when_no_document_is_selected():
    script = _read(CONSOLE_JS)
    loop = _incremental_loop(script)
    source_ids = _section_between(
        script,
        "function _knowledgeBackendSourceDocumentIds",
        "async function startVisualBuildLoop",
    )

    assert "function _knowledgeBackendSourceDocumentIds" in script
    assert ".filter(doc => _isKnowledgeBackendSourceDocument(doc) && doc.id)" in source_ids
    assert "const sourceDocumentIds = _knowledgeBackendSourceDocumentIds();" in loop
    assert "await startVisualBuildLoopQueue(sourceDocumentIds" in loop
    assert "retryFailed: buildOptions.retryFailed !== undefined ? !!buildOptions.retryFailed : !!retryFailed" in loop


def test_console_excludes_generated_documents_from_visual_build_sources():
    script = _read(CONSOLE_JS)
    predicate = _section_between(
        script,
        "function _isKnowledgeBackendSourceDocument",
        "function _knowledgeBackendSourceDocumentIds",
    )
    selector = _section_between(
        script,
        "function renderKnowledgeBackendDocumentSelector",
        "function getSelectedKnowledgeBackendDocumentId",
    )
    rendered_docs = _section_between(
        script,
        "function renderKnowledgeBackendDocuments",
        "function renderKnowledgeBackendSummary",
    )
    loop_entry = _section_between(
        script,
        "async function startVisualBuildLoop",
        "async function startVisualBuildLoopIncremental",
    )

    assert "doc_type" in predicate
    assert re.search(r"===\s*'document'", predicate)
    assert ".filter(_isKnowledgeBackendSourceDocument)" in selector
    assert "const visualBtns = _isKnowledgeBackendSourceDocument(doc)" in rendered_docs
    assert "selectedDoc && !_isKnowledgeBackendSourceDocument(selectedDoc)" in loop_entry
    assert "Selected document is generated and cannot be used for visual completion." in loop_entry


def test_console_visual_progress_shows_group_merge_metadata():
    script = _read(CONSOLE_JS)
    progress = _progress_renderer(script)

    assert "groupMergeStrategy" in progress
    assert "groupMergeFallbackReason" in progress
    assert "Group merge strategy: ${escapeHtml(groupMergeStrategy)}" in progress
    assert "Group merge fallback: ${escapeHtml(groupMergeFallbackReason)}" in progress
    for label in (
        "Processed:",
        "Succeeded:",
        "Low confidence:",
        "Failed:",
        "Pending:",
        "Multipage groups:",
        "Groups merged:",
        "Low-confidence groups:",
        "Failed groups:",
    ):
        assert label in progress


def test_console_visual_status_text_has_single_final_assignment_path():
    script = _read(CONSOLE_JS)
    loop = _incremental_loop(script)
    progress = _progress_renderer(script)

    visual_message_block = _section_between(
        loop,
        "updateVisualBuildProgress(data, totals, sourceDoc, data.analysis_backend || backend);",
        "const prepare = data.prepare || {};",
    )
    label_block = _section_between(
        progress,
        "const detailEl = document.getElementById('knowledge-backend-visual-progress-detail');",
        "if (percentEl) percentEl.textContent",
    )

    assert visual_message_block.count("messageEl.textContent =") == 1
    assert label_block.count("labelEl.textContent =") == 1


def test_console_visual_progress_uses_page_scan_percent_until_prepare_is_done():
    script = _read(CONSOLE_JS)
    progress = _progress_renderer(script)
    helper = _section_between(
        script,
        "function _visualBuildOverallProgress",
        "function resetVisualBuildProgress",
    )

    assert "const progressInfo = _visualBuildOverallProgress(prepare, preparePercent, analysisPercent, total);" in progress
    assert "const percent = progressInfo.percent;" in progress
    assert "阶段：${escapeHtml(progressInfo.stage)}" in progress
    assert "status !== 'done' || preparedPages < totalPages" in helper
    assert "Math.min(99, preparePercent || 0)" in helper
    assert "analysisPercent || 0" in helper


def test_console_renders_external_visual_status_without_build_endpoint():
    script = _read(CONSOLE_JS)
    panel = _section_between(
        script,
        "function loadKnowledgeBackendPanel",
        "function loadVisualAnalysisBackends",
    )
    external = _section_between(
        script,
        "function renderKnowledgeBackendExternalVisualProgress",
        "function uploadKnowledgeBackendFile",
    )

    assert "fetch('/api/knowledge/admin/visual/status')" in panel
    assert "renderKnowledgeBackendExternalVisualProgress(visualData)" in panel
    assert "updateVisualBuildProgress(visualData, null, sourceDoc, backend)" in external
    assert "latestRun.analysis_backend || visualData.analysis_backend || 'background'" in external
    assert "startKnowledgeBackendVisualStatusPoll()" in external
    assert "fetch('/api/knowledge/admin/visual/status')" in external
    assert "fetch(`/api/knowledge/admin/visual/status?document_id=${encodeURIComponent(sourceDoc.id)}`)" in external
    assert "String(visualData.document_id || '') !== String(sourceDoc.id)" in external
    assert "/api/knowledge/admin/visual/build" not in external


def test_console_stops_external_visual_status_poll_when_leaving_knowledge_view():
    script = _read(CONSOLE_JS)
    navigation_hook = _section_between(
        script,
        "const _origNavigateTo = navigateTo;",
        "// =====================================================================\n// Knowledge View",
    )
    poller = _section_between(
        script,
        "function startKnowledgeBackendVisualStatusPoll",
        "function uploadKnowledgeBackendFile",
    )

    assert "currentView === 'knowledge' && viewId !== 'knowledge'" in navigation_hook
    assert "stopKnowledgeBackendVisualStatusPoll()" in navigation_hook
    assert "currentView !== 'knowledge'" in poller
    assert "window.clearTimeout(_knowledgeBackendVisualStatusPollTimer)" in poller
