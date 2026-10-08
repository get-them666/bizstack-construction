"""Contract tests for the single-record lead-to-job workspace."""

import pathlib


ROOT = pathlib.Path(__file__).resolve().parent
APP = ROOT / "construction_main.py"


def _function_source(name: str) -> str:
    source = APP.read_text()
    start = source.index(f"async def {name}(")
    end = source.find("\n@app.", start)
    return source[start:end] if end > start else source[start:]


def test_lead_workspace_loads_only_the_visible_construction_lead():
    route = _function_source("lead_workspace_page")
    assert "l.id = %s AND l.company = 'construction'" in route
    assert 'source_visibility_clause("l")' in route
    assert "FROM projects p" in route and "WHERE p.lead_id = %s" in route
    assert "FROM comms_logs WHERE lead_id = %s" in route
    assert "LOWER(sender) = LOWER(%s)" in route
    assert "FROM payments WHERE lead_id = %s" in route


def test_project_activation_and_completion_advance_the_related_lead():
    route = _function_source("project_update")
    assert 'status_val in ("active", "completed")' in route
    assert '"in_progress" if status_val == "active" else "completed"' in route
    assert "WHERE id = %s AND company = 'construction'" in route


def test_project_creation_can_be_started_from_a_specific_lead():
    route = _function_source("projects_page")
    assert "lead_id: int = 0" in route
    assert "selected_lead = next(" in route
    assert "WHERE id = %s AND company = 'construction'" in route

    template = (ROOT / "templates/construction/projects.html").read_text()
    assert 'id="create-project"' in template
    assert "{% if selected_lead and l.id == selected_lead.id %}selected{% endif %}" in template
    assert "value=\"{{ selected_lead.address if selected_lead else '' }}\"" in template
    assert "{{ selected_lead.description if selected_lead else '' }}" in template

    create_route = _function_source("project_create")
    assert "WHERE id = %s AND company = 'construction'" in create_route
    assert 'summary = summary or lead["description"] or ""' in create_route
    assert 'url=f"/projects?project_id={pid}#project-{pid}"' in create_route
    assert 'href="/leads/{{ p.lead_id }}"' in template


def test_leads_and_dashboard_open_the_same_workspace():
    lead_cards = (ROOT / "templates/construction/_leads_lane.html").read_text()
    dashboard = (ROOT / "templates/construction/dashboard.html").read_text()
    workspace = (ROOT / "templates/construction/lead_workspace.html").read_text()
    assert 'href="/leads/{{ lead.id }}"' in lead_cards
    assert 'href="/leads/{{ lead.id }}"' in dashboard
    for path in (
        "/api/leads/{{ lead.id }}/status",
        "/api/leads/{{ lead.id }}/touch",
        "/api/leads/{{ lead.id }}/notes",
        "/projects?lead_id={{ lead.id }}#create-project",
    ):
        assert path in workspace
    assert 'class="draft-btn' in workspace and 'data-lead-id="{{ lead.id }}"' in workspace
    assert "data-deposit data-lead=\"{{ lead.id }}\"" in workspace
    shared_script = (ROOT / "templates/construction/_shell_script.html").read_text()
    assert "/api/leads/' + id + '/draft-email" in shared_script
    assert "/api/leads/' + leadId + '/deposit-link" in shared_script


def test_workspace_notes_form_supports_textarea_feedback():
    script = (ROOT / "templates/construction/_shell_script.html").read_text()
    assert """const notesField = form.querySelector('[name="notes"]');""" in script
    assert """if (notesField) {""" in script
